"""
Pausing for a backup restore (docs/ai/PHASE2.md §3.9, §3.6): while the marker is set the dispatcher claims, heartbeats,
sweeps and scans nothing; a task that finishes or fails meanwhile writes nothing until the marker clears, then
applies its result or gives its task back without using up an attempt, fenced on its token.
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from ingest_runner_testing import (
    FakeHandlers,
    Jobs,
    PhaseCalls,
    blocking,
    extract_result,
    raising,
    run,
    settle,
    wait_for,
)

from mealie.repos.repository_recipe_ingest import utcnow
from mealie.schema.recipe_ingest import IngestErrorCode, IngestStatus, IngestTaskState
from mealie.services.ai.errors import IngestPaused
from mealie.services.ai.ingest import limits, storage
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.types import TaskContext


def _pause() -> None:
    storage.pause_marker_path().write_text(f"{time.time():.3f}")


def _resume() -> None:
    storage.pause_marker_path().unlink(missing_ok=True)


def _seconds_from_now(value: datetime) -> float:
    return (value - datetime.now(UTC)).total_seconds()


def test_nothing_is_claimed_heartbeated_swept_or_scanned_while_paused(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    phases: PhaseCalls,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    monkeypatch.setattr(limits, "HOUSEKEEPING_INTERVAL", 0)
    monkeypatch.setattr(limits, "PURGE_FIRST_DELAY", 0)
    gate = threading.Event()
    handlers.default = blocking(gate)
    running = jobs.create()

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        before = (len(phases.housekeeping), len(phases.commits), phases.inbox, len(phases.purge))

        _pause()
        queued = jobs.create()
        expired = jobs.create(
            state=IngestTaskState.running, lease_token=uuid4(), lease_expires_at=utcnow() - timedelta(seconds=1)
        )
        lease = utcnow() + timedelta(seconds=5)
        jobs.update(running, lease_expires_at=lease)
        await dispatcher.run_once()

        assert len(handlers.calls) == 1
        assert jobs.row(queued)["task_state"] == IngestTaskState.queued
        assert jobs.row(expired)["task_state"] == IngestTaskState.running
        assert jobs.row(running)["lease_expires_at"] == lease.replace(tzinfo=UTC)
        assert (len(phases.housekeeping), len(phases.commits), phases.inbox, len(phases.purge)) == before

        _resume()
        gate.set()
        await settle(dispatcher)

    run(scenario())


def test_a_task_that_finishes_while_paused_waits_then_finalizes(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers
):
    async def restore_starts_meanwhile(ctx: TaskContext) -> Any:
        _pause()
        return extract_result()

    job_id = jobs.create()
    handlers.default = restore_starts_meanwhile

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        await settle_briefly(dispatcher)
        row = jobs.row(job_id)
        assert (row["status"], row["task_state"]) == (IngestStatus.processing, IngestTaskState.running)

        _resume()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["task_state"], row["attempts"]) == (IngestStatus.ready, None, 1)


async def settle_briefly(dispatcher: IngestDispatcher) -> None:
    """Gives a waiting task several pause polls, and checks it's still waiting"""
    assert not await dispatcher.drain(timeout=0.2)


@pytest.mark.parametrize(
    "error",
    [IngestPaused(), FileNotFoundError("pages/0/page.jpg"), RuntimeError("no such table: recipe_ingestion_jobs")],
    ids=["paused", "files-missing", "anything"],
)
def test_a_task_failing_while_paused_is_given_back_without_using_an_attempt(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, error: Exception
):
    async def restore_starts_meanwhile(ctx: TaskContext) -> Any:
        _pause()
        raise error

    job_id = jobs.create()
    handlers.default = restore_starts_meanwhile

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        await settle_briefly(dispatcher)
        assert jobs.row(job_id)["task_state"] == IngestTaskState.running

        _resume()
        await settle(dispatcher)
        await dispatcher.run_once()  # not claimed again before its delay
        await settle(dispatcher)

    run(scenario())
    assert len(handlers.calls) == 1
    row = jobs.row(job_id)
    assert (row["status"], row["task_state"], row["lease_token"], row["attempts"], row["error_code"]) == (
        IngestStatus.processing,
        IngestTaskState.queued,
        None,
        0,
        None,
    )
    assert limits.PAUSED_RELEASE_DELAY - 10 < _seconds_from_now(row["not_before"]) <= limits.PAUSED_RELEASE_DELAY


def test_ingest_paused_after_the_pause_ended_still_releases(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers
):
    job_id = jobs.create()
    handlers.default = raising(IngestPaused())

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["task_state"], row["attempts"], row["error_code"]) == (IngestTaskState.queued, 0, None)


def test_missing_files_outside_a_pause_fail_the_card(dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers):
    job_id = jobs.create()
    handlers.default = raising(FileNotFoundError("pages/0/view.jpg"))

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"]) == (
        IngestStatus.failed,
        IngestErrorCode.files_missing,
        None,
    )


def test_a_restored_row_drops_the_waiting_result(dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers):
    """A restore brings back the row with another token: the result waiting for the pause is dropped"""
    job_id = jobs.create()

    async def restore_meanwhile(ctx: TaskContext) -> Any:
        _pause()
        jobs.update(job_id, lease_token=uuid4())  # the row as the backup had it
        return extract_result()

    handlers.default = restore_meanwhile

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        _resume()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["draft"]) == (IngestStatus.processing, None)


def test_after_a_pause_this_process_renews_its_leases_before_any_sweep(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers
):
    """Heartbeats stop during a pause, so leases expire; the first ticks after it renew them, and sweeps wait"""
    gate = threading.Event()
    handlers.default = blocking(gate)
    job_id = jobs.create()

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        token = jobs.row(job_id)["lease_token"]

        _pause()
        await dispatcher.run_once()
        jobs.update(job_id, lease_expires_at=utcnow() - timedelta(minutes=1))  # a long restore
        other = jobs.create(
            state=IngestTaskState.running, lease_token=uuid4(), lease_expires_at=utcnow() - timedelta(minutes=1)
        )
        _resume()
        await dispatcher.run_once()

        row = jobs.row(job_id)
        assert row["lease_token"] == token
        assert _seconds_from_now(row["lease_expires_at"]) > limits.LEASE - 10
        assert jobs.row(other)["task_state"] == IngestTaskState.running  # swept once every process has renewed

        gate.set()
        await settle(dispatcher)

    run(scenario())
    assert jobs.row(job_id)["status"] == IngestStatus.ready


def test_a_task_waiting_for_a_pause_gives_up_at_its_deadline(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "TASK_DEADLINE", 0.3)

    async def restore_starts_meanwhile(ctx: TaskContext) -> Any:
        _pause()
        return extract_result()

    job_id = jobs.create()
    handlers.default = restore_starts_meanwhile

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher, timeout=5)

    run(scenario())
    row = jobs.row(job_id)
    # nothing written: its lease expires and a sweep after the restore queues it again
    assert (row["status"], row["task_state"], row["draft"]) == (IngestStatus.processing, IngestTaskState.running, None)
    assert row["error_code"] is None
