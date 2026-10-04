"""
Leases (docs/ai/PHASE2.md §3.2, §3.5, §3.6, §3.8): heartbeats, the sweep and its poison guard, cancellation, the
deadline, rate-limit backoff and shutdown.
"""

import asyncio
import math
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from ingest_runner_testing import FakeHandlers, Jobs, blocking, raising, run, settle, wait_for
from sqlalchemy.orm import Session

from mealie.core import exceptions
from mealie.repos.repository_recipe_ingest import TASK_CLEARED, CancelOutcome, IngestQueue, cancel_task, utcnow
from mealie.schema.recipe_ingest import IngestErrorCode, IngestStatus, IngestTaskKind, IngestTaskState
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.runner import dispatcher as dispatcher_module
from mealie.services.ai.ingest.runner.classify import rate_limit_delay
from mealie.services.ai.ingest.runner.dispatcher import Claim, IngestDispatcher, _RunningTask, claim_tasks
from mealie.services.ai.ingest.runner.sweep import sweep_expired
from mealie.services.ai.ingest.runner.types import TaskContext
from mealie.services.ai.ingest.runner.worker import CancelReason


def _seconds_from_now(value: datetime) -> float:
    return (value - datetime.now(UTC)).total_seconds()


def _claim(session: Session, job_id: UUID) -> UUID:
    token = uuid4()
    assert IngestQueue(session).claim(job_id, token=token, owner="elsewhere", now=utcnow())
    return token


def _expire(jobs: Jobs, job_id: UUID) -> None:
    jobs.update(job_id, lease_expires_at=utcnow() - timedelta(seconds=1))


# ==========================================
# Heartbeats and the sweep


def test_the_heartbeat_renews_the_lease(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    gate = threading.Event()
    handlers.default = blocking(gate)
    job_id = jobs.create()

    async def scenario() -> float:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        jobs.update(job_id, lease_expires_at=utcnow() + timedelta(seconds=5))
        await dispatcher.run_once()
        remaining = _seconds_from_now(jobs.row(job_id)["lease_expires_at"])
        gate.set()
        await settle(dispatcher)
        return remaining

    assert run(scenario()) > limits.LEASE - 10
    assert jobs.row(job_id)["status"] == IngestStatus.ready


def test_an_expired_lease_is_queued_again(db: Session, jobs: Jobs):
    job_id = jobs.create()
    _claim(db, job_id)
    _expire(jobs, job_id)

    assert sweep_expired(db, utcnow()).requeued == [job_id]
    row = jobs.row(job_id)
    assert (row["task_state"], row["lease_token"], row["lease_expires_at"]) == (IngestTaskState.queued, None, None)
    assert row["attempts"] == 1  # the lost lease counts
    assert row["status"] == IngestStatus.processing


def test_a_live_lease_is_left_alone(db: Session, jobs: Jobs):
    job_id = jobs.create()
    token = _claim(db, job_id)

    assert sweep_expired(db, utcnow()).requeued == []
    assert jobs.row(job_id)["lease_token"] == token


def test_three_lost_leases_fail_a_processing_job(db: Session, jobs: Jobs):
    job_id = jobs.create()
    for attempt in range(1, limits.MAX_ATTEMPTS + 1):
        _claim(db, job_id)
        _expire(jobs, job_id)
        result = sweep_expired(db, utcnow())
        if attempt < limits.MAX_ATTEMPTS:
            assert result.requeued == [job_id]

    assert (result.failed, result.interrupted) == ([job_id], [])
    row = jobs.row(job_id)
    assert row["status"] == IngestStatus.failed
    assert row["error_code"] == IngestErrorCode.interrupted
    assert all(row[column] == value for column, value in TASK_CLEARED.items())


def test_three_lost_leases_leave_a_ready_job_ready_with_a_banner(db: Session, jobs: Jobs):
    job_id = jobs.ready(kind=IngestTaskKind.reread, state=IngestTaskState.queued)
    draft = jobs.row(job_id)["draft"]
    for _ in range(limits.MAX_ATTEMPTS):
        _claim(db, job_id)
        _expire(jobs, job_id)
        result = sweep_expired(db, utcnow())

    assert result.interrupted == [job_id]
    row = jobs.row(job_id)
    assert row["status"] == IngestStatus.ready
    assert row["error_code"] == IngestErrorCode.interrupted
    assert row["task_state"] is None and row["lease_token"] is None
    assert row["draft"] == draft
    assert row["row_version"] > 0  # a banner is a versioned write


def test_the_sweep_leaves_the_tasks_this_process_holds(db: Session, jobs: Jobs):
    job_id = jobs.create()
    token = _claim(db, job_id)
    _expire(jobs, job_id)

    assert sweep_expired(db, utcnow(), held={token}).requeued == []
    assert jobs.row(job_id)["lease_token"] == token


def test_a_stale_dispatcher_sweep_doesnt_touch_a_reclaimed_task(db: Session, jobs: Jobs):
    """The sweep is fenced on the expired token: a task claimed again in the meantime keeps its new lease"""
    job_id = jobs.create()
    old = _claim(db, job_id)
    _expire(jobs, job_id)
    leases = IngestQueue(db).expired(utcnow())
    assert [lease.token for lease in leases] == [old]

    assert IngestQueue(db).requeue_expired(job_id, old, utcnow())
    new = _claim(db, job_id)
    assert not IngestQueue(db).requeue_expired(job_id, old, utcnow())
    assert jobs.row(job_id)["lease_token"] == new


# ==========================================
# Cancellation and the deadline


def test_cancelling_a_queued_task_fails_its_job_before_it_runs(
    dispatcher: IngestDispatcher, db: Session, jobs: Jobs, handlers: FakeHandlers
):
    job_id = jobs.create()
    assert cancel_task(db, job_id) == CancelOutcome.cancelled

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert handlers.calls == []
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"]) == (
        IngestStatus.failed,
        IngestErrorCode.cancelled,
        None,
    )


def test_cancelling_a_running_task_stops_it_within_a_heartbeat(
    dispatcher: IngestDispatcher, db: Session, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    cancelled = threading.Event()

    async def provider_call(ctx: TaskContext) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    job_id = jobs.create()
    handlers.default = provider_call

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        assert cancel_task(db, job_id) == CancelOutcome.requested
        await dispatcher.run_once()
        await settle(dispatcher, timeout=5)

    run(scenario())
    assert cancelled.is_set()
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"], row["lease_token"]) == (
        IngestStatus.failed,
        IngestErrorCode.cancelled,
        None,
        None,
    )


def test_cancelling_a_running_reread_leaves_the_draft_without_a_banner(
    dispatcher: IngestDispatcher, db: Session, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    job_id = jobs.ready(kind=IngestTaskKind.reread, state=IngestTaskState.queued)
    handlers.default = blocking(threading.Event())

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        cancel_task(db, job_id)
        await dispatcher.run_once()
        await settle(dispatcher, timeout=5)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"], row["proposals"]) == (
        IngestStatus.ready,
        None,
        None,
        None,
    )


def test_a_cleared_token_cancels_its_task_and_nothing_is_written(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    """A commit or discard clears the task columns: the next heartbeat stops the task, whose result is dropped"""
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    job_id = jobs.ready(kind=IngestTaskKind.extract, state=IngestTaskState.queued)
    handlers.default = blocking(threading.Event())

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        jobs.update(job_id, status=IngestStatus.committing.value, **TASK_CLEARED)
        before = jobs.row(job_id)
        await dispatcher.run_once()
        await settle(dispatcher, timeout=5)
        assert jobs.row(job_id) == before

    run(scenario())


def test_a_cancel_asked_while_the_task_ran_survives_its_requeue(
    dispatcher: IngestDispatcher, db: Session, jobs: Jobs, handlers: FakeHandlers
):
    """
    The reviewer cancels a running card and every provider answers 429 before the next heartbeat: the task goes back
    to the queue still asked to stop, so it's ended as cancelled instead of being read (and paid for) again
    """
    gate = threading.Event()

    async def rate_limited(ctx: TaskContext) -> None:
        while not gate.is_set():
            await asyncio.sleep(0.01)
        raise exceptions.RateLimitError("every provider answered 429")

    job_id = jobs.create()
    handlers.default = rate_limited

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        assert cancel_task(db, job_id) == CancelOutcome.requested
        gate.set()
        await settle(dispatcher)
        assert jobs.row(job_id)["task_state"] == IngestTaskState.queued
        jobs.update(job_id, not_before=None)  # its backoff is over
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert len(handlers.calls) == 1
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"], row["cancel_requested"], row["draft"]) == (
        IngestStatus.failed,
        IngestErrorCode.cancelled,
        None,
        False,
        None,
    )


def test_a_task_given_back_after_a_cancel_was_asked_is_never_claimed_and_the_sweep_ends_it(db: Session, jobs: Jobs):
    """
    A shutdown, a pause, the sweep or a rate limit can give a task back before the heartbeat that would have stopped
    it: it's never claimed again, and the next sweep ends it the way cancelling a queued task does
    """
    processing = jobs.create()
    reread = jobs.ready(kind=IngestTaskKind.reread, state=IngestTaskState.queued)
    for job_id in (processing, reread):
        token = _claim(db, job_id)
        assert cancel_task(db, job_id) == CancelOutcome.requested
        assert IngestQueue(db).release(job_id, token)
        assert jobs.row(job_id)["cancel_requested"]

    assert claim_tasks(db, owner="test", general_slots=5, reread_slots=1).claims == []

    result = sweep_expired(db, utcnow())
    assert sorted(result.cancelled) == sorted([processing, reread])
    failed = jobs.row(processing)
    assert (failed["status"], failed["error_code"], failed["task_state"], failed["cancel_requested"]) == (
        IngestStatus.failed,
        IngestErrorCode.cancelled,
        None,
        False,
    )
    ready = jobs.row(reread)
    assert (ready["status"], ready["error_code"], ready["task_state"], ready["cancel_requested"]) == (
        IngestStatus.ready,
        None,
        None,
        False,
    )
    assert sweep_expired(db, utcnow()).cancelled == []


def test_a_task_past_its_deadline_fails_with_timeout(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "TASK_DEADLINE", 0.2)
    job_id = jobs.create()
    handlers.default = blocking(threading.Event())

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        await asyncio.sleep(0.3)
        await dispatcher.run_once()
        await settle(dispatcher, timeout=5)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"]) == (IngestStatus.failed, IngestErrorCode.timeout, None)


# ==========================================
# Rate limits


def test_the_rate_limit_backoff_doubles_up_to_15_minutes():
    assert [rate_limit_delay(n) for n in range(7)] == [60, 120, 240, 480, 900, 900, 900]


def test_a_rate_limited_task_is_queued_again_with_a_backoff(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers
):
    job_id = jobs.create(rate_limit_retries=2)
    handlers.default = raising(exceptions.RateLimitError("every provider answered 429"))

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)
        await dispatcher.run_once()  # backing off: not claimed again
        await settle(dispatcher)

    run(scenario())
    assert len(handlers.calls) == 1
    row = jobs.row(job_id)
    assert (row["status"], row["task_state"], row["lease_token"], row["error_code"]) == (
        IngestStatus.processing,
        IngestTaskState.queued,
        None,
        None,
    )
    assert row["rate_limit_retries"] == 3
    assert row["attempts"] == 0  # a rate limit isn't a lost lease
    assert 240 - 10 < _seconds_from_now(row["not_before"]) <= 240


def test_a_task_rate_limited_six_times_fails(dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers):
    job_id = jobs.ready(
        kind=IngestTaskKind.extract, state=IngestTaskState.queued, rate_limit_retries=limits.MAX_RATE_LIMIT_RETRIES
    )
    handlers.default = raising(exceptions.RateLimitError("429"))

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"]) == (
        IngestStatus.ready,
        IngestErrorCode.rate_limited,
        None,
    )


# ==========================================
# Shutdown


def test_shutdown_releases_the_leases_it_holds(dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers):
    running = [jobs.create(), jobs.create()]
    handlers.default = blocking(threading.Event())

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 2)
        await dispatcher.stop()

    run(scenario())
    assert dispatcher.running_tasks == []
    for job_id in running:
        row = jobs.row(job_id)
        assert (row["status"], row["task_state"], row["lease_token"], row["attempts"], row["error_code"]) == (
            IngestStatus.processing,
            IngestTaskState.queued,
            None,
            0,
            None,
        )


def test_shutdown_during_a_claim_gives_back_what_it_claimed(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    """
    Shutdown cancels the dispatcher's loop while a claim is in flight (its `UPDATE` committed, the claim phase not yet
    back): the claimed task is still started, stopped and released, not left `running` with an attempt used
    """
    monkeypatch.setattr(dispatcher_module, "STOP_WAIT", 0.05)
    claimed = threading.Event()
    claim = IngestQueue.claim

    def slow_claim(self: IngestQueue, job_id: UUID, **kwargs: Any) -> bool:
        won = claim(self, job_id, **kwargs)
        claimed.set()
        time.sleep(0.5)  # e.g. SQLite busy on the next statement
        return won

    monkeypatch.setattr(IngestQueue, "claim", slow_claim)
    job_id = jobs.create()
    handlers.default = blocking(threading.Event())

    async def scenario() -> None:
        await dispatcher.start()
        await wait_for(claimed.is_set)
        await dispatcher.stop()

    run(scenario())
    assert dispatcher.running_tasks == []
    row = jobs.row(job_id)
    assert (row["status"], row["task_state"], row["lease_token"], row["attempts"]) == (
        IngestStatus.processing,
        IngestTaskState.queued,
        None,
        0,
    )


def test_a_cancellation_before_the_task_thread_attached_is_delivered_when_it_does():
    handle = _RunningTask(Claim(uuid4(), uuid4(), reread_slot=False), deadline=math.inf)
    assert handle.cancel(CancelReason.shutdown)  # its thread has started, but its loop isn't running yet
    stopped: list[CancelReason | None] = []

    async def task() -> None:
        current = asyncio.current_task()
        assert current is not None
        handle.attach(asyncio.get_running_loop(), current)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            stopped.append(handle.reason())
            raise

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(task())
    assert stopped == [CancelReason.shutdown]
    assert not handle.cancel(CancelReason.shutdown)  # delivered once
