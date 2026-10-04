"""
Fixtures for the recipe card runner's tests (docs/ai/PHASE2.md §3): jobs in a test household, fake task handlers, the
dispatcher's other phases replaced by recorders, and a queue that only sees the test's own jobs (the dispatcher claims
across households, and other tests in the same process may leave queued jobs behind).
"""

import asyncio
from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

import pytest
from ingest_runner_testing import FakeHandlers, Jobs, PhaseCalls
from sqlalchemy.orm import Session

from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import ExpiredLease, IngestQueue
from mealie.services.ai.ingest import commit, events, inbox, limits, retention, storage, tasks
from mealie.services.ai.ingest.runner import retries
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from tests.utils.fixture_schemas import TestUser


@pytest.fixture()
def db() -> Iterator[Session]:
    with session_context() as session:
        yield session


@pytest.fixture()
def jobs(db: Session, unique_user: TestUser) -> Jobs:
    return Jobs(db, unique_user)


@pytest.fixture(autouse=True)
def only_this_tests_jobs(monkeypatch: pytest.MonkeyPatch, jobs: Jobs) -> None:
    """The dispatcher claims and sweeps across households: keep it to the jobs this test created"""
    queued_ids = IngestQueue.queued_ids
    expired = IngestQueue.expired

    def own_queued_ids(self: IngestQueue, now: datetime, limit: int, *, max_priority: int | None = None) -> list[UUID]:
        if limit <= 0:
            return []
        ids = queued_ids(self, now, 100_000, max_priority=max_priority)
        return [job_id for job_id in ids if job_id in jobs.ids][:limit]

    def own_expired(self: IngestQueue, now: datetime) -> list[ExpiredLease]:
        return [lease for lease in expired(self, now) if lease.job_id in jobs.ids]

    monkeypatch.setattr(IngestQueue, "queued_ids", own_queued_ids)
    monkeypatch.setattr(IngestQueue, "expired", own_expired)


@pytest.fixture()
def handlers(monkeypatch: pytest.MonkeyPatch) -> FakeHandlers:
    fake = FakeHandlers()
    monkeypatch.setattr(tasks, "handle_extract", fake.extract)
    monkeypatch.setattr(tasks, "handle_reread", fake.reread)
    return fake


@pytest.fixture(autouse=True)
def phases(monkeypatch: pytest.MonkeyPatch) -> PhaseCalls:
    """The dispatcher's other work items (B2-B4) replaced by recorders, so these tests exercise only the runner"""
    calls = PhaseCalls()

    def scan_once() -> int:
        calls.inbox += 1
        return 0

    def maybe_notify_batch(batch_id: UUID) -> bool:
        calls.notified.append(batch_id)
        return False

    def resume_stale_commits(now: datetime) -> int:
        calls.commits.append(now)
        return 0

    monkeypatch.setattr(events, "housekeeping", calls.housekeeping.append)
    monkeypatch.setattr(events, "maybe_notify_batch", maybe_notify_batch)
    monkeypatch.setattr(commit, "resume_stale_commits", resume_stale_commits)
    monkeypatch.setattr(inbox, "scan_once", scan_once)

    def retry_waiting(now: datetime) -> int:
        calls.retries.append(now)
        return 0

    monkeypatch.setattr(retention, "purge_once", calls.purge.append)
    monkeypatch.setattr(retries, "retry_waiting", retry_waiting)
    return calls


@pytest.fixture(autouse=True)
def quick_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "PAUSED_TASK_POLL", 0.02)
    monkeypatch.setattr(limits, "SHUTDOWN_GRACE", 2)


@pytest.fixture(autouse=True)
def no_pause_left_behind() -> Iterator[None]:
    yield
    storage.pause_marker_path().unlink(missing_ok=True)


@pytest.fixture()
def dispatcher() -> Iterator[IngestDispatcher]:
    """A dispatcher of the test's own (two task threads plus the re-read slot), stopped afterwards"""
    instance = IngestDispatcher(concurrency=2, instance="test")
    yield instance
    asyncio.run(instance.stop())
