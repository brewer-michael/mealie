"""
Claims (docs/ai/PHASE2.md §3.2, §3.4): concurrent claims are unique, the queue order, backoffs, the re-read slot, no
database work on the dispatcher's event loop, and times bound from Python whatever the database's time zone.
"""

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from ingest_runner_testing import FakeHandlers, Jobs, PhaseCalls, blocking, reread_result, run, settle, wait_for
from sqlalchemy import event
from sqlalchemy.orm import Session

from mealie.db import db_setup
from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import IngestQueue, utcnow
from mealie.schema.recipe_ingest import IngestStatus, IngestTaskKind, IngestTaskState
from mealie.services.ai.ingest import events, limits
from mealie.services.ai.ingest.runner.dispatcher import Claim, IngestDispatcher, claim_tasks
from mealie.services.ai.ingest.runner.sweep import sweep_expired


def _claim(session: Session, *, general: int, reread: int, owner: str = "test") -> list[Claim]:
    batch = claim_tasks(session, owner=owner, general_slots=general, reread_slots=reread)
    assert batch.error is None
    return batch.claims


def test_concurrent_claims_from_threads_are_unique(jobs: Jobs):
    queued = {jobs.create() for _ in range(12)}
    start = threading.Barrier(4)
    claimed: dict[str, list[Claim]] = {}

    def claimer(owner: str) -> None:
        with session_context() as session:
            start.wait(5)
            claimed[owner] = _claim(session, general=6, reread=0, owner=owner)

    threads = [threading.Thread(target=claimer, args=(f"process-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    every = [claim for claims in claimed.values() for claim in claims]
    assert sorted(claim.job_id for claim in every) == sorted(queued)  # each job once, none left behind
    assert len({claim.token for claim in every}) == len(every)
    for owner, claims in claimed.items():
        for claim in claims:
            row = jobs.row(claim.job_id)
            assert row["task_state"] == IngestTaskState.running
            assert row["lease_token"] == claim.token
            assert row["lease_owner"] == owner
            assert row["attempts"] == 1


def test_rereads_take_the_reread_slot_then_tasks_go_by_priority_and_age(db: Session, jobs: Jobs):
    oldest = jobs.create()
    newer = jobs.create()
    reread = jobs.ready(kind=IngestTaskKind.reread, state=IngestTaskState.queued)

    claims = _claim(db, general=1, reread=1)
    assert [(claim.job_id, claim.reread_slot) for claim in claims] == [(reread, True), (oldest, False)]
    assert [claim.job_id for claim in _claim(db, general=5, reread=1)] == [newer]


def test_extractions_never_take_the_reread_slot(db: Session, jobs: Jobs):
    jobs.create()
    assert _claim(db, general=0, reread=1) == []


def test_a_task_backing_off_waits_for_its_time(db: Session, jobs: Jobs):
    later = jobs.create(not_before=utcnow() + timedelta(minutes=5))
    due = jobs.create(not_before=utcnow() - timedelta(seconds=1))

    assert [claim.job_id for claim in _claim(db, general=2, reread=0)] == [due]
    assert jobs.row(later)["task_state"] == IngestTaskState.queued


def test_the_reread_slot_runs_a_reread_while_both_extraction_slots_are_busy(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers
):
    gate, open_gate = threading.Event(), threading.Event()
    open_gate.set()
    extractions = [jobs.create() for _ in range(3)]
    handlers.default = blocking(gate)

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 2)
        assert sorted(dispatcher.running_tasks) == sorted(extractions[:2])

        reread = jobs.ready(kind=IngestTaskKind.reread, state=IngestTaskState.queued)
        handlers.behaviour[reread] = blocking(open_gate, reread_result)  # answers at once
        await dispatcher.run_once()
        await wait_for(lambda: jobs.row(reread)["task_state"] is None)
        assert len(jobs.row(reread)["proposals"]) == 1
        assert jobs.row(extractions[2])["task_state"] == IngestTaskState.queued  # no slot for it yet

        gate.set()
        await settle(dispatcher)
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert [jobs.row(job_id)["status"] for job_id in extractions] == [IngestStatus.ready] * 3


def test_no_database_call_runs_on_the_event_loop(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    phases: PhaseCalls,
    monkeypatch: pytest.MonkeyPatch,
):
    """Every session the dispatcher opens (claims, heartbeats, sweeps, releases) is opened off its event loop"""
    sessions: list[int] = []
    other_work: list[int] = []
    factory = db_setup.SessionLocal

    def tracked_factory() -> Session:
        sessions.append(threading.get_ident())
        return factory()

    def housekeeping(now: object) -> None:
        other_work.append(threading.get_ident())

    monkeypatch.setattr(db_setup, "SessionLocal", tracked_factory)
    monkeypatch.setattr(events, "housekeeping", housekeeping)
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    gate = threading.Event()
    job_id = jobs.create()
    expired = jobs.create(state=IngestTaskState.running, lease_token=uuid4(), lease_expires_at=utcnow(), attempts=1)
    handlers.behaviour[job_id] = blocking(gate)

    async def scenario() -> int:
        loop_thread = threading.get_ident()
        await dispatcher.run_once()  # sweeps (the expired lease), claims, housekeeping
        await wait_for(lambda: job_id in [ctx.job_id for ctx in handlers.calls])
        await dispatcher.run_once()  # heartbeats the running task
        gate.set()
        await settle(dispatcher)
        await dispatcher.stop()
        return loop_thread

    loop_thread = run(scenario())
    assert sessions, "the scenario opened no sessions"
    assert loop_thread not in sessions
    assert other_work and loop_thread not in other_work
    assert jobs.row(job_id)["status"] == IngestStatus.ready
    assert jobs.row(expired)["status"] == IngestStatus.ready  # swept, claimed again and run


@pytest.fixture()
def new_york_sessions() -> Iterator[bool]:
    """Every new connection's session time zone is America/New_York (PostgreSQL); whether it applies"""
    engine = db_setup.engine
    postgres = engine.dialect.name == "postgresql"

    def set_time_zone(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TIME ZONE 'America/New_York'")
        cursor.close()

    if postgres:
        event.listen(engine, "connect", set_time_zone)
        engine.dispose()
    try:
        yield postgres
    finally:
        if postgres:
            event.remove(engine, "connect", set_time_zone)
            engine.dispose()


def test_time_comparisons_ignore_the_database_time_zone(new_york_sessions: bool, jobs: Jobs):
    """`now` is bound from Python as naive UTC, never the database's clock, which a non-UTC zone would shift"""
    not_yet = jobs.create(not_before=utcnow() + timedelta(minutes=30))
    due = jobs.create(not_before=utcnow() - timedelta(minutes=1))
    live_token, dead_token = uuid4(), uuid4()
    live = jobs.create(
        state=IngestTaskState.running, lease_token=live_token, lease_expires_at=utcnow() + timedelta(seconds=60)
    )
    dead = jobs.create(
        state=IngestTaskState.running, lease_token=dead_token, lease_expires_at=utcnow() - timedelta(seconds=60)
    )

    with session_context() as session:
        if new_york_sessions:
            assert session.execute(sa.text("SHOW TIME ZONE")).scalar_one() == "America/New_York"
        claims = _claim(session, general=5, reread=0)
        swept = sweep_expired(session, utcnow())

    assert [claim.job_id for claim in claims] == [due]
    assert jobs.row(not_yet)["task_state"] == IngestTaskState.queued
    assert swept.requeued == [dead]
    assert jobs.row(live)["lease_token"] == live_token
    assert jobs.row(dead)["task_state"] == IngestTaskState.queued


def test_a_claim_records_its_lease(db: Session, jobs: Jobs):
    job_id = jobs.create()
    before = datetime.now(UTC)  # stored naive UTC, read back as UTC
    [claim] = _claim(db, general=1, reread=0, owner="host:1:abc")

    row = jobs.row(job_id)
    assert row["lease_token"] == claim.token
    assert isinstance(row["lease_token"], UUID)
    assert before <= row["task_started_at"] <= datetime.now(UTC)
    expires = row["lease_expires_at"] - row["task_started_at"]
    assert expires == timedelta(seconds=limits.LEASE)


def test_claims_stop_at_a_failure_and_keep_what_they_took(db: Session, jobs: Jobs, monkeypatch: pytest.MonkeyPatch):
    first, second = jobs.create(), jobs.create()
    claim = IngestQueue.claim
    calls = []

    def failing_claim(self: IngestQueue, job_id: UUID, **kwargs) -> bool:
        calls.append(job_id)
        if len(calls) == 2:
            raise RuntimeError("database went away")
        return claim(self, job_id, **kwargs)

    monkeypatch.setattr(IngestQueue, "claim", failing_claim)
    batch = claim_tasks(db, owner="test", general_slots=2, reread_slots=0)

    assert [c.job_id for c in batch.claims] == [first]
    assert isinstance(batch.error, RuntimeError)
    assert jobs.row(second)["task_state"] == IngestTaskState.queued


def test_a_dispatcher_waits_for_slots(dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers):
    gate = threading.Event()
    handlers.default = blocking(gate)
    queued = [jobs.create() for _ in range(4)]

    async def scenario() -> None:
        await dispatcher.run_once()
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 2)
        await asyncio.sleep(0.05)
        assert len(handlers.calls) == 2
        gate.set()
        for _ in range(3):
            await settle(dispatcher)
            await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert [jobs.row(job_id)["status"] for job_id in queued] == [IngestStatus.ready] * 4
