"""
Batches (docs/ai/PHASE2.md §1.4): which batch an upload joins, sealing (by the app and when idle), and why a seal can't
race an insert. Runs on SQLite and PostgreSQL; the race tests use two sessions in two threads.
"""

import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core.exceptions import NoEntryFound
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.recipe_ingest import IngestSource, IngestStatus
from mealie.services.ai.ingest import batches, limits
from mealie.services.ai.ingest.batches import seal_idle_batches, select_batch
from tests.utils.fixture_schemas import TestUser

Batch = RecipeIngestionBatch


@pytest.fixture()
def db() -> Iterator[Session]:
    with session_context() as session:
        yield session


def _repos(session: Session, user: TestUser) -> IngestRepos:
    return IngestRepos(session, UUID(user.group_id), UUID(user.household_id))


def _batch(session: Session, batch_id: UUID) -> Batch:
    batch = session.execute(
        sa.select(Batch).where(Batch.id == batch_id).execution_options(populate_existing=True)
    ).scalar_one()
    session.commit()
    return batch


def _select(repos: IngestRepos, user: TestUser, batch_id=None, **kwargs) -> UUID:  # noqa: ANN001
    params = {
        "batch_id": batch_id,
        "source": IngestSource.api,
        "created_by": user.user_id,
        "source_key": None,
        "locale": "en-US",
        "now": utcnow(),
        **kwargs,
    }
    selected = select_batch(repos, **params)
    repos.session.commit()
    return selected


def _naive(value: datetime | None) -> datetime | None:
    """Timestamps come back as aware UTC; the queries bind naive UTC"""
    return value.replace(tzinfo=None) if value else None


def _set(session: Session, batch_id: UUID, **values) -> None:  # noqa: ANN003
    session.execute(sa.update(Batch).where(Batch.id == batch_id).values(**values))
    session.commit()


def _add_job(repos: IngestRepos, batch_id: UUID, *, commit: bool = True) -> UUID:
    return repos.jobs.create(
        {
            "batch_id": batch_id,
            "position": repos.jobs.next_position(batch_id),
            "source": IngestSource.api.value,
            "status": IngestStatus.processing.value,
            "pages": [],
            "source_sha256": uuid4().hex * 2,
        },
        commit=commit,
    )


def _jobs_in(session: Session, batch_id: UUID) -> int:
    count = session.execute(
        sa.select(sa.func.count()).select_from(RecipeIngestionJob).where(RecipeIngestionJob.batch_id == batch_id)
    ).scalar_one()
    session.commit()
    return count


# ==========================================
# Choosing a batch


def test_new_always_starts_a_batch_in_the_callers_transaction(db: Session, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    repos = _repos(db, user)
    first = _select(repos, user, "new")
    second = _select(repos, user, "new")
    assert first != second
    batch = _batch(db, first)
    assert (batch.source, batch.created_by, batch.locale, batch.sealed_at) == ("api", user.user_id, "en-US", None)

    # nothing is committed by the choice itself
    rolled_back = select_batch(
        repos, batch_id="new", source=IngestSource.api, created_by=None, source_key=None, locale=None, now=utcnow()
    )
    db.rollback()
    assert repos.batches.get(rolled_back) is None


def test_api_uploads_join_the_newest_open_batch_of_two_minutes(db: Session, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    repos = _repos(db, user)
    first = _select(repos, user)
    assert _select(repos, user) == first

    # another uploader, source or source key gets its own
    assert _select(repos, user, created_by=None) != first
    assert _select(repos, user, source=IngestSource.inbox, created_by=None, source_key="g/h") not in (first,)
    inbox = _select(repos, user, source=IngestSource.inbox, created_by=None, source_key="g/h")
    assert _select(repos, user, source=IngestSource.inbox, created_by=None, source_key="g/other") != inbox

    # idle for two minutes: a new one
    _set(db, first, last_upload_at=utcnow() - timedelta(seconds=limits.AUTO_BATCH_IDLE + 1))
    second = _select(repos, user)
    assert second != first

    # sealed: never joined
    _set(db, second, sealed_at=utcnow())
    assert _select(repos, user) not in (first, second)


def test_an_explicit_batch_is_used_while_open_and_replaced_once_sealed(db: Session, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    repos = _repos(db, user)
    app_batch = repos.batches.create(source=IngestSource.app, created_by=user.user_id)
    assert _select(repos, user, app_batch) == app_batch

    _set(db, app_batch, sealed_at=utcnow())
    replacement = _select(repos, user, app_batch)
    assert replacement != app_batch
    assert _batch(db, replacement).source == "app"  # the sealed batch's source, not the request's
    assert _batch(db, app_batch).sealed_at is not None  # never reopened

    # the next card for the sealed batch follows into the same replacement
    assert _select(repos, user, app_batch) == replacement


def test_an_unknown_or_foreign_batch_is_not_found(db: Session, unique_user: TestUser, h2_user: TestUser):
    theirs = _repos(db, h2_user).batches.create(source=IngestSource.app, created_by=h2_user.user_id)
    for batch_id in (theirs, uuid4()):
        with pytest.raises(NoEntryFound):
            _select(_repos(db, unique_user), unique_user, batch_id)


def test_positions_come_from_the_app_or_follow_the_last_card(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    batch_id = repos.batches.create(source=IngestSource.app, created_by=None)
    assert batches.next_position(repos, batch_id, None) == 0
    _add_job(repos, batch_id)
    assert batches.next_position(repos, batch_id, None) == 1
    assert batches.next_position(repos, batch_id, 7) == 7


# ==========================================
# Sealing


def test_a_batch_is_sealed_once(db: Session, unique_user: TestUser, h2_user: TestUser):
    repos = _repos(db, unique_user)
    batch_id = repos.batches.create(source=IngestSource.app, created_by=None)
    assert batches.seal(repos, batch_id, utcnow())
    sealed_at = _batch(db, batch_id).sealed_at
    assert not batches.seal(repos, batch_id, utcnow() + timedelta(seconds=5))
    assert _batch(db, batch_id).sealed_at == sealed_at

    # another household's seal matches nothing
    theirs = _repos(db, h2_user).batches.create(source=IngestSource.app, created_by=None)
    assert not batches.seal(repos, theirs, utcnow())
    assert _batch(db, theirs).sealed_at is None


def test_idle_batches_seal_after_their_kinds_idle_time(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    now = utcnow()

    def batch(source: IngestSource, idle: int) -> UUID:
        return repos.batches.create(source=source, created_by=None, now=now - timedelta(seconds=idle))

    app_busy = batch(IngestSource.app, limits.APP_BATCH_IDLE - 60)
    app_idle = batch(IngestSource.app, limits.APP_BATCH_IDLE + 60)
    api_busy = batch(IngestSource.api, limits.AUTO_BATCH_IDLE - 30)
    api_idle = batch(IngestSource.api, limits.AUTO_BATCH_IDLE + 30)
    inbox_idle = batch(IngestSource.inbox, limits.AUTO_BATCH_IDLE + 30)
    already = batch(IngestSource.api, limits.AUTO_BATCH_IDLE + 30)
    _set(db, already, sealed_at=now - timedelta(seconds=10))

    sealed = set(seal_idle_batches(db, now))
    mine = {app_busy, app_idle, api_busy, api_idle, inbox_idle, already}
    assert sealed & mine == {app_idle, api_idle, inbox_idle}
    assert _naive(_batch(db, app_idle).sealed_at) == now
    assert _naive(_batch(db, already).sealed_at) == now - timedelta(seconds=10)
    assert _batch(db, app_busy).sealed_at is None
    assert not set(seal_idle_batches(db, now)) & mine


# ==========================================
# Sealing can't race an insert (two sessions)


def _in_thread(target, *args) -> tuple[threading.Thread, dict]:  # noqa: ANN001
    result: dict = {}

    def run() -> None:
        try:
            result["value"] = target(*args)
        except BaseException as e:  # reported by the test
            result["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def test_a_seal_waits_for_an_insert_in_progress(unique_user: TestUser):
    with session_context() as setup:
        batch_id = _repos(setup, unique_user).batches.create(source=IngestSource.app, created_by=None)

    def seal_then_count() -> tuple[bool, int]:
        with session_context() as session:
            repos = _repos(session, unique_user)
            sealed = batches.seal(repos, batch_id, utcnow())
            return sealed, _jobs_in(session, batch_id)

    with session_context() as session:
        repos = _repos(session, unique_user)
        assert batches.touch(repos, batch_id, utcnow())  # the insert's transaction has begun
        thread, result = _in_thread(seal_then_count)
        time.sleep(0.5)
        assert thread.is_alive(), "the seal must wait for the insert's transaction"
        _add_job(repos, batch_id, commit=False)
        session.commit()

    thread.join(10)
    assert "error" not in result, result
    sealed, jobs_when_sealed = result["value"]
    assert sealed
    with session_context() as session:
        assert jobs_when_sealed == _jobs_in(session, batch_id) == 1


def test_an_insert_after_a_seal_finds_the_batch_closed(unique_user: TestUser):
    with session_context() as setup:
        batch_id = _repos(setup, unique_user).batches.create(source=IngestSource.app, created_by=None)

    with session_context() as other:
        assert batches.seal(_repos(other, unique_user), batch_id, utcnow())

    with session_context() as session:
        assert not batches.touch(_repos(session, unique_user), batch_id, utcnow())
        session.rollback()


def test_an_idle_seal_rechecks_after_an_upload_touches_the_batch(unique_user: TestUser):
    with session_context() as setup:
        repos = _repos(setup, unique_user)
        old = utcnow() - timedelta(seconds=limits.AUTO_BATCH_IDLE + 60)
        batch_id = repos.batches.create(source=IngestSource.api, created_by=None, now=old)

    def seal_idle() -> list[UUID]:
        with session_context() as session:
            return seal_idle_batches(session, utcnow())

    with session_context() as session:
        repos = _repos(session, unique_user)
        assert batches.touch(repos, batch_id, utcnow())
        thread, result = _in_thread(seal_idle)
        time.sleep(0.5)
        _add_job(repos, batch_id, commit=False)
        session.commit()

    thread.join(10)
    assert "error" not in result, result
    assert batch_id not in result["value"]
    with session_context() as session:
        assert _batch(session, batch_id).sealed_at is None
