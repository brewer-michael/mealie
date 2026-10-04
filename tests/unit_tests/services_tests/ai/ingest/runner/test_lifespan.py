"""
The dispatcher's life (docs/ai/PHASE2.md §3.2, §3.8): started by the ingest router's lifespan inside Mealie's app (no
`app.py` change) unless the worker is off, woken from any thread, its phases on their schedules, a failing phase
logged once without stopping the loop, and shutdown through the lifespan.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from uuid import UUID, uuid4

import pytest
from ingest_runner_testing import FakeHandlers, Jobs, PhaseCalls, blocking, run, settle, wait_for

import mealie.app as mealie_app
from mealie.app import app
from mealie.core.config import get_app_settings
from mealie.db import init_db
from mealie.repos.repository_recipe_ingest import IngestQueue
from mealie.schema.recipe_ingest import IngestStatus, IngestTaskState
from mealie.services.ai.ingest import events, limits, storage
from mealie.services.ai.ingest.runner import dispatcher as dispatcher_module
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.dispatcher import dispatcher as app_dispatcher
from mealie.services.ai.ingest.settings import get_ingest_settings


@pytest.fixture()
def bare_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mealie's lifespan without the database initialization and the scheduler (the Phase 3 precedent)"""

    async def no_scheduler() -> None:
        return None

    monkeypatch.setattr(init_db, "main", lambda: None)
    monkeypatch.setattr(mealie_app, "start_scheduler", no_scheduler)


@pytest.fixture()
def worker_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AI_INGEST_WORKER` is off under `TESTING`; these tests turn it on"""
    monkeypatch.setattr(get_ingest_settings(), "WORKER", True)


@pytest.fixture()
def lock_checks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    checks: list[int] = []

    def flock_supported() -> bool:
        checks.append(threading.get_ident())
        return True

    monkeypatch.setattr(storage, "flock_supported", flock_supported)
    return checks


def test_the_app_lifespan_runs_the_dispatcher(
    bare_app: None, worker_on: None, lock_checks: list[int], jobs: Jobs, handlers: FakeHandlers
):
    job_id = jobs.create()

    async def scenario() -> int:
        async with app.router.lifespan_context(app):
            assert app_dispatcher.running
            app_dispatcher.wake()  # as intake does after inserting a job
            await wait_for(lambda: jobs.row(job_id)["status"] == IngestStatus.ready)
        assert not app_dispatcher.running
        return threading.get_ident()

    loop_thread = run(scenario())
    assert len(lock_checks) == 1 and lock_checks[0] != loop_thread  # checked once at start, off the event loop


def test_the_lifespan_starts_nothing_while_the_worker_is_off(
    bare_app: None, lock_checks: list[int], jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    job_id = jobs.create()

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            assert not app_dispatcher.running
            await asyncio.sleep(0.1)

    run(scenario())  # TESTING: tests drive the dispatcher with run_once()
    monkeypatch.setattr(get_ingest_settings(), "WORKER", True)
    monkeypatch.setattr(get_ingest_settings(), "ENABLED", False)
    run(scenario())

    assert handlers.calls == [] and lock_checks == []
    assert jobs.row(job_id)["task_state"] == IngestTaskState.queued


def test_leaving_the_lifespan_releases_running_tasks(
    bare_app: None, worker_on: None, lock_checks: list[int], jobs: Jobs, handlers: FakeHandlers
):
    handlers.default = blocking(threading.Event())
    job_id = jobs.create()

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            app_dispatcher.wake()
            await wait_for(lambda: len(handlers.calls) == 1)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["task_state"], row["lease_token"], row["attempts"]) == (
        IngestStatus.processing,
        IngestTaskState.queued,
        None,
        0,
    )


def test_a_wake_from_another_thread_starts_work_at_once(
    dispatcher: IngestDispatcher,
    lock_checks: list[int],
    jobs: Jobs,
    handlers: FakeHandlers,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(limits, "POLL_INTERVAL", 60)

    async def scenario() -> None:
        await dispatcher.start()
        await asyncio.sleep(0.1)  # the first tick found nothing; the next poll is a minute away
        job_id = jobs.create()
        threading.Thread(target=dispatcher.wake).start()  # intake runs in a worker thread
        await wait_for(lambda: jobs.row(job_id)["status"] == IngestStatus.ready, timeout=5)
        await dispatcher.stop()

    run(scenario())


def test_a_failing_phase_is_logged_once_and_doesnt_stop_the_loop(
    dispatcher: IngestDispatcher,
    lock_checks: list[int],
    jobs: Jobs,
    handlers: FakeHandlers,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    monkeypatch.setattr(limits, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(limits, "HOUSEKEEPING_INTERVAL", 0)
    monkeypatch.setattr(limits, "PHASE_BACKOFF_MAX", 0.1)
    monkeypatch.setattr(dispatcher_module, "FIRST_BACKOFF", 0.02)
    attempts: list[int] = []
    broken = threading.Event()
    broken.set()
    message = f"card text {uuid4()}"

    def housekeeping(now: object) -> None:
        attempts.append(1)
        if broken.is_set():
            raise RuntimeError(message)

    monkeypatch.setattr(events, "housekeeping", housekeeping)

    async def scenario() -> None:
        await dispatcher.start()
        job_id = jobs.create()
        dispatcher.wake()
        await wait_for(lambda: jobs.row(job_id)["status"] == IngestStatus.ready)
        await wait_for(lambda: len(attempts) >= 4)
        broken.clear()
        count = len(attempts)
        await wait_for(lambda: len(attempts) > count + 1)
        assert dispatcher.running
        await dispatcher.stop()

    with caplog.at_level(logging.INFO):
        run(scenario())

    failures = [record for record in caplog.records if "the housekeeping failed" in record.getMessage()]
    assert len(failures) == 1
    assert "RuntimeError" in failures[0].getMessage()
    assert message not in caplog.text  # the stack and the type, never the message
    assert any("the housekeeping works again" in record.getMessage() for record in caplog.records)


def test_a_failing_sweep_doesnt_stop_claims(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    calls: list[int] = []

    def broken_expired(self: IngestQueue, now: object) -> list:
        calls.append(1)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(IngestQueue, "expired", broken_expired)
    job_id = jobs.create()

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)
        await dispatcher.run_once()  # the sweep is backing off

    run(scenario())
    assert jobs.row(job_id)["status"] == IngestStatus.ready
    assert calls == [1]


def test_the_phases_run_on_their_schedules(
    jobs: Jobs, handlers: FakeHandlers, phases: PhaseCalls, monkeypatch: pytest.MonkeyPatch
):
    """Housekeeping, stale commits and the inbox at once and then every interval; the purge 10 minutes after start"""
    instance = IngestDispatcher(concurrency=1)

    async def two_ticks() -> None:
        await instance.run_once()
        await instance.run_once()

    run(two_ticks())
    assert (len(phases.housekeeping), len(phases.commits), phases.inbox, len(phases.purge)) == (1, 1, 1, 0)

    monkeypatch.setattr(limits, "PURGE_FIRST_DELAY", 0)
    instance = IngestDispatcher(concurrency=1)
    run(two_ticks())
    assert len(phases.purge) == 1  # then daily


def test_a_lock_check_that_fails_doesnt_stop_the_start(
    dispatcher: IngestDispatcher, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def flock_supported() -> bool:
        raise PermissionError("read-only data directory")

    monkeypatch.setattr(storage, "flock_supported", flock_supported)

    async def scenario() -> bool:
        await dispatcher.start()
        running = dispatcher.running
        await dispatcher.stop()
        return running

    assert run(scenario())
    assert any("Couldn't check file locking" in record.getMessage() for record in caplog.records)
    assert not dispatcher.running


def test_one_dispatcher_runs_per_lifespan(
    bare_app: None, worker_on: None, lock_checks: list[int], jobs: Jobs, handlers: FakeHandlers
):
    """Entering the lifespan while the dispatcher already runs (a second app in one process) starts nothing more"""

    async def scenario() -> list[UUID]:
        async with app.router.lifespan_context(app):
            runner = app_dispatcher._runner
            async with app_dispatcher.lifespan(app):
                assert app_dispatcher._runner is runner
            assert app_dispatcher.running
        return app_dispatcher.running_tasks

    assert run(scenario()) == []
    assert len(lock_checks) == 1


# ==========================================
# Telling whether a card reader runs


def test_a_process_whose_worker_is_off_warns_that_it_reads_no_cards(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(get_app_settings(), "TESTING", False)  # under TESTING the worker is off by default, quietly
    monkeypatch.setattr(get_ingest_settings(), "WORKER", False)
    instance = IngestDispatcher(concurrency=1)

    async def scenario() -> None:
        async with instance.lifespan(app):
            assert not instance.running

    with caplog.at_level(logging.WARNING):
        run(scenario())
    warnings = [record for record in caplog.records if "reads no cards" in record.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "run the worker in another process" in warnings[0].getMessage()

    caplog.clear()
    monkeypatch.setattr(get_ingest_settings(), "ENABLED", False)  # ingestion off: nothing to warn about
    run(scenario())
    assert not any("reads no cards" in record.getMessage() for record in caplog.records)


def test_a_running_dispatcher_marks_itself_seen_at_most_once_an_interval(
    jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    storage.dispatcher_seen_path().unlink(missing_ok=True)
    assert storage.dispatcher_seen_at() is None
    instance = IngestDispatcher(concurrency=1)

    before = time.time()
    run(instance.run_once())
    seen = storage.dispatcher_seen_at()
    assert seen is not None and before - 1 <= seen <= time.time() + 1

    old = time.time() - 30
    os.utime(storage.dispatcher_seen_path(), (old, old))
    run(instance.run_once())  # within the interval: not written again
    assert storage.dispatcher_seen_at() == pytest.approx(old)

    monkeypatch.setattr(limits, "DISPATCHER_SEEN_INTERVAL", 0)
    instance = IngestDispatcher(concurrency=1)
    run(instance.run_once())
    seen = storage.dispatcher_seen_at()
    assert seen is not None and seen > old + 20


def test_a_paused_dispatcher_still_marks_itself_seen(jobs: Jobs, handlers: FakeHandlers):
    storage.dispatcher_seen_path().unlink(missing_ok=True)
    storage.pause_marker_path().write_text(f"{time.time():.3f}")
    job_id = jobs.create()

    run(IngestDispatcher(concurrency=1).run_once())
    assert storage.dispatcher_seen_at() is not None
    assert handlers.calls == [] and jobs.row(job_id)["task_state"] == IngestTaskState.queued


def test_the_dispatcher_clears_a_crashed_restores_marker_when_it_starts(
    dispatcher: IngestDispatcher, lock_checks: list[int], caplog: pytest.LogCaptureFixture
):
    """A container stopped mid-restore left its marker: the restarted process carries on at once"""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(30)
    marker = storage.pause_marker_path()
    marker.write_text(
        json.dumps(
            {
                "time": time.time(),
                "pid": gone.pid,
                "host": storage._host_identity(),
                "started": None,
                "lock": str(storage.restore_lock_path()),  # nobody holds it now
            }
        )
    )
    cleared: list[bool] = []
    clear_stale_pause = storage.clear_stale_pause

    def recorded() -> bool:
        cleared.append(clear_stale_pause())
        return cleared[-1]

    async def scenario() -> bool:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(storage, "clear_stale_pause", recorded)
            await dispatcher.start()
        exists = marker.exists()
        await dispatcher.stop()
        return exists

    with caplog.at_level(logging.INFO):
        assert run(scenario()) is False
    assert cleared == [True]
    assert any("no longer running" in record.getMessage() for record in caplog.records)
