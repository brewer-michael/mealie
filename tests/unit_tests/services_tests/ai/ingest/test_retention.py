"""
The retention purge (docs/ai/PHASE2.md §16): committed cards past retention lose their files and card text but keep
their row, failed ones go entirely, empty batches and orphan job folders are removed, and ready or committing cards,
eval cases and `recipes/` are never touched. Idempotent, and stopped by a backup restore.
"""

import fcntl
import os
import threading
import time
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from mealie.core.config import get_app_dirs
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardFlag,
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    ExtractionMeta,
    IngestSource,
    IngestStatus,
    PageMeta,
)
from mealie.services.ai.ingest import limits, retention, storage
from mealie.services.ai.ingest.settings import get_ingest_settings
from tests.utils.fixture_schemas import TestUser

DAY = timedelta(days=1)


def _page() -> PageMeta:
    return PageMeta(
        index=0,
        width=480,
        height=640,
        view_width=480,
        view_height=640,
        raw_sha256="a" * 64,
        page_sha256="b" * 64,
        format="jpeg",
        raw_bytes=1000,
        ocr={"text": "Banana Mug Cake", "confidence": 61.0},
    )


class Seeder:
    def __init__(self, user: TestUser) -> None:
        self.group_id, self.household_id = UUID(user.group_id), UUID(user.household_id)
        self.user = user
        with session_context() as session:
            self.batch_id = IngestRepos(session, self.group_id, self.household_id).batches.create(
                source=IngestSource.app, created_by=user.user_id
            )

    def batch(self, *, last_upload_ago: timedelta) -> UUID:
        with session_context() as session:
            batch_id = IngestRepos(session, self.group_id, self.household_id).batches.create(
                source=IngestSource.api, created_by=self.user.user_id, now=utcnow() - last_upload_ago
            )
        return batch_id

    def job(self, status: IngestStatus, *, age: timedelta = timedelta(0), **columns: Any) -> UUID:
        """A job with a page on disk, whose last change (and commit, if committed) was `age` ago"""
        job_id = uuid4()
        when = utcnow() - age
        with storage.ingest_write():
            page_dir = storage.create_job_dir(self.group_id, job_id, 1) / "pages" / "0"
            storage.atomic_write_bytes(page_dir / "page.jpg", b"jpeg")

        flag = CardFlag(
            id="blank:steps:x",
            kind=CardFlagKind.blank,
            severity=CardFlagSeverity.error,
            source=CardFlagSource.marker,
            field="steps",
        )
        values: dict[str, Any] = {
            "id": job_id,
            "batch_id": columns.pop("batch_id", self.batch_id),
            "source": "app",
            "status": status.value,
            "title": "Banana Mug Cake",
            "pages": [_page()],
            "source_sha256": "c" * 64,
            "transcription": "Banana Mug Cake",
            "draft": CardDraft(name="Banana Mug Cake"),
            "flags": [flag],
            "proposals": [],
            "extraction": ExtractionMeta(read_path="image"),
            "recipe_id": uuid4() if status == IngestStatus.committed else None,
            "committed_at": when if status == IngestStatus.committed else None,
            "created_at": when,
            **columns,
        }
        with session_context() as session:
            IngestRepos(session, self.group_id, self.household_id).jobs.create(values)
            # set last, so the column's own `onupdate` doesn't move it
            session.execute(sa.update(RecipeIngestionJob).where(RecipeIngestionJob.id == job_id).values(update_at=when))
            session.commit()
        return job_id

    def dir(self, job_id: UUID) -> Any:
        return storage.job_dir(self.group_id, job_id)


def _row(job_id: UUID) -> dict[str, Any] | None:
    with session_context() as session:
        row = (
            session.execute(sa.select(*RecipeIngestionJob.__table__.columns).where(RecipeIngestionJob.id == job_id))
            .mappings()
            .one_or_none()
        )
        return dict(row) if row else None


def _batch_exists(batch_id: UUID) -> bool:
    with session_context() as session:
        stmt = sa.select(RecipeIngestionBatch.id).where(RecipeIngestionBatch.id == batch_id)
        return session.execute(stmt).first() is not None


def _orphan(group_id: UUID, *, age_seconds: float, name: str | None = None) -> Any:
    path = storage.ingest_root(group_id) / (name or str(uuid4()))
    (path / "pages" / "0").mkdir(parents=True)
    (path / "pages" / "0" / "page.jpg").write_bytes(b"jpeg")
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    return path


@pytest.fixture
def seeder(unique_user_fn_scoped: TestUser) -> Seeder:
    return Seeder(unique_user_fn_scoped)


def test_the_purge(seeder: Seeder):
    retention_days = get_ingest_settings().RETENTION_DAYS
    old, recent = timedelta(days=retention_days + 1), timedelta(days=retention_days - 1)

    committed_old = seeder.job(IngestStatus.committed, age=old)
    committed_recent = seeder.job(IngestStatus.committed, age=recent)
    failed_old = seeder.job(IngestStatus.failed, age=old, error_code="no_recipe_found")
    failed_recent = seeder.job(IngestStatus.failed, age=recent)
    ready_old = seeder.job(IngestStatus.ready, age=old * 3)
    committing_old = seeder.job(IngestStatus.committing, age=old, commit_recipe_id=uuid4())
    processing_old = seeder.job(IngestStatus.processing, age=old, task_state="queued", task_kind="extract")

    empty_old = seeder.batch(last_upload_ago=old)
    empty_recent = seeder.batch(last_upload_ago=timedelta(hours=2))
    busy_old = seeder.batch(last_upload_ago=old)
    seeder.job(IngestStatus.ready, batch_id=busy_old)

    orphan_old = _orphan(seeder.group_id, age_seconds=limits.ORPHAN_DIR_AGE + 60)
    orphan_new = _orphan(seeder.group_id, age_seconds=60)
    not_a_job = _orphan(seeder.group_id, age_seconds=limits.ORPHAN_DIR_AGE * 10, name="notes")
    eval_case = storage.eval_cards_dir(seeder.group_id) / "banana.json"
    eval_case.parent.mkdir(parents=True, exist_ok=True)
    eval_case.write_text("{}")
    recipe_dir = get_app_dirs().RECIPE_DATA_DIR / str(uuid4())
    recipe_dir.mkdir(parents=True)
    os.utime(recipe_dir, (0, 0))

    retention.purge_once(utcnow())

    # committed: files gone, card text cleared, the row kept with its recipe link and duplicate hash
    row = _row(committed_old)
    assert row is not None
    assert not seeder.dir(committed_old).exists()
    for column in retention.SLIMMED_COLUMNS:
        assert row[column] is None, column
    assert row["recipe_id"] is not None
    assert row["source_sha256"] == "c" * 64
    assert row["pages"][0]["ocr"] is None
    assert row["pages"][0]["raw_sha256"] == "a" * 64
    assert _row(committed_recent)["draft"] is not None
    assert seeder.dir(committed_recent).is_dir()

    # failed: row and files
    assert _row(failed_old) is None
    assert not seeder.dir(failed_old).exists()
    assert _row(failed_recent) is not None
    assert seeder.dir(failed_recent).is_dir()

    # never ready, committing or processing cards, however old
    for job_id in (ready_old, committing_old, processing_old):
        assert _row(job_id)["draft"] is not None
        assert seeder.dir(job_id).is_dir()

    assert not _batch_exists(empty_old)
    assert _batch_exists(empty_recent)
    assert _batch_exists(busy_old)

    assert not orphan_old.exists()
    assert orphan_new.is_dir()
    assert not_a_job.is_dir()
    assert eval_case.is_file()
    assert recipe_dir.is_dir()

    # idempotent
    snapshot = {job_id: _row(job_id) for job_id in (committed_old, committed_recent, ready_old)}
    retention.purge_once(utcnow())
    assert {job_id: _row(job_id) for job_id in snapshot} == snapshot


def test_empty_batches_go_a_day_after_their_last_upload_sealed_or_not(seeder: Seeder):
    """
    A duplicate-only upload or one the app abandoned (a logout) leaves an empty batch nobody seals: it goes a day after
    its last upload, as a sealed one does, not after the card retention. A batch with cards stays.
    """
    two_days, two_hours = timedelta(days=2), timedelta(hours=2)
    open_old = seeder.batch(last_upload_ago=two_days)
    open_recent = seeder.batch(last_upload_ago=two_hours)
    sealed_old = seeder.batch(last_upload_ago=two_days)
    with_cards = seeder.batch(last_upload_ago=two_days)
    seeder.job(IngestStatus.ready, batch_id=with_cards)
    with session_context() as session:
        repos = IngestRepos(session, seeder.group_id, seeder.household_id)
        assert repos.batches.seal(sealed_old, utcnow() - two_days)
        assert repos.batches.seal(with_cards, utcnow() - two_days)
        # no upload since it was made (an app batch whose first card was refused): its age counts from its creation
        never_used = repos.batches.create(source=IngestSource.app, created_by=None, now=utcnow() - two_days)
        session.execute(
            sa.update(RecipeIngestionBatch).where(RecipeIngestionBatch.id == never_used).values(last_upload_at=None)
        )
        session.commit()

    retention.purge_once(utcnow())

    assert not _batch_exists(open_old)
    assert not _batch_exists(sealed_old)
    assert not _batch_exists(never_used)
    assert _batch_exists(open_recent)
    assert _batch_exists(with_cards)


def test_nothing_is_purged_while_a_restore_pauses_ingestion(seeder: Seeder):
    old = timedelta(days=get_ingest_settings().RETENTION_DAYS + 1)
    failed = seeder.job(IngestStatus.failed, age=old)
    committed = seeder.job(IngestStatus.committed, age=old)

    marker = storage.pause_marker_path()
    marker.write_text(f"{time.time():.3f}")
    try:
        retention.purge_once(utcnow())
    finally:
        marker.unlink(missing_ok=True)
    assert _row(failed) is not None
    assert _row(committed)["draft"] is not None
    assert seeder.dir(committed).is_dir()

    # a restore holding the write lock, before its marker is up
    fd = os.open(storage.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retention.purge_once(utcnow())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert _row(failed) is not None
    assert seeder.dir(committed).is_dir()

    retention.purge_once(utcnow())
    assert _row(failed) is None
    assert not seeder.dir(committed).exists()


def test_a_job_retried_meanwhile_is_left_alone(seeder: Seeder, monkeypatch: pytest.MonkeyPatch):
    """The delete is conditional on the state that made the job purgeable"""
    old = timedelta(days=get_ingest_settings().RETENTION_DAYS + 1)
    failed = seeder.job(IngestStatus.failed, age=old)
    real_execute = retention._execute

    def retried_first(session: Any, stmt: Any) -> int:
        if isinstance(stmt, sa.Delete) and stmt.table.name == RecipeIngestionJob.__tablename__:
            session.execute(
                sa.update(RecipeIngestionJob)
                .where(RecipeIngestionJob.id == failed)
                .values(status=IngestStatus.processing.value, task_state="queued", update_at=utcnow())
            )
            session.commit()
        return real_execute(session, stmt)

    monkeypatch.setattr(retention, "_execute", retried_first)
    retention.purge_once(utcnow())
    assert _row(failed)["status"] == "processing"
    assert seeder.dir(failed).is_dir()


def test_a_failed_card_waiting_for_the_monthly_limit_is_kept_until_after_its_retry(seeder: Seeder):
    """
    A card that failed `limit_reached` on the 5th waits for the reset on the 1st: its retention counts from then, so it
    isn't deleted before it's read again, and goes `RETENTION_DAYS` after the retry if it's still failed then
    """
    retention_days = get_ingest_settings().RETENTION_DAYS
    old = timedelta(days=retention_days + 10)
    waiting = seeder.job(
        IngestStatus.failed, age=old, error_code="limit_reached", auto_retry_at=utcnow() + timedelta(days=5)
    )
    retried_long_ago = seeder.job(
        IngestStatus.failed,
        age=old,
        error_code="limit_reached",
        auto_retry_at=utcnow() - timedelta(days=retention_days + 1),
    )
    retried_lately = seeder.job(
        IngestStatus.failed, age=old, error_code="limit_reached", auto_retry_at=utcnow() - timedelta(days=1)
    )

    retention.purge_once(utcnow())

    assert _row(waiting) is not None and seeder.dir(waiting).is_dir()
    assert _row(retried_lately) is not None
    assert _row(retried_long_ago) is None and not seeder.dir(retried_long_ago).exists()

    # when the card says it goes: its retry plus the retention
    with session_context() as session:
        job = session.get(RecipeIngestionJob, waiting)
        assert job is not None
        expires = retention.failed_card_expires_at(job)
        assert expires is not None
        assert abs(expires.replace(tzinfo=None) - (utcnow() + timedelta(days=5 + retention_days))) < timedelta(
            minutes=1
        )
        job_ready = session.get(RecipeIngestionJob, seeder.job(IngestStatus.ready))
        assert job_ready is not None and retention.failed_card_expires_at(job_ready) is None


def test_the_purge_removes_readings_a_restore_cut_off_that_no_task_used(seeder: Seeder):
    from mealie.services.ai.ingest.runner import results

    job_id = uuid4()
    key = results.TaskKey.of(job_id, "extract", None, ("b" * 64,))
    reading = results.ExtractResult(
        draft=CardDraft(name="Banana Mug Cake"),
        flags=[],
        transcription="",
        extraction=ExtractionMeta(),
        pages=[_page()],
    )
    assert results.keep(key, result=reading)
    kept = storage.results_dir() / f"{job_id}.extract.json"
    day_ago = time.time() - limits.KEPT_RESULT_TTL - 60
    os.utime(kept, (day_ago, day_ago))

    retention.purge_once(utcnow())
    assert not kept.exists()


def test_an_undo_racing_the_purge_keeps_the_cards_photos(seeder: Seeder, monkeypatch: pytest.MonkeyPatch):
    """
    The purge picks a committed card, then an undo takes it back to review before the purge gets to it: the slimming
    update no longer matches, and the folder (removed only once the row says so) stays with its photos
    """
    old = timedelta(days=get_ingest_settings().RETENTION_DAYS + 1)
    committed = seeder.job(IngestStatus.committed, age=old)
    real_execute = retention._execute

    def undone_first(session: Any, stmt: Any) -> int:
        if isinstance(stmt, sa.Update) and stmt.table.name == RecipeIngestionJob.__tablename__:
            session.execute(  # what the undo's guarded update does
                sa.update(RecipeIngestionJob)
                .where(RecipeIngestionJob.id == committed)
                .values(
                    status=IngestStatus.ready.value,
                    recipe_id=None,
                    committed_at=None,
                    row_version=RecipeIngestionJob.row_version + 1,
                )
            )
            session.commit()
        return real_execute(session, stmt)

    monkeypatch.setattr(retention, "_execute", undone_first)
    retention.purge_once(utcnow())

    row = _row(committed)
    assert row["status"] == "ready"
    assert row["draft"] is not None
    assert (seeder.dir(committed) / "pages" / "0" / "page.jpg").is_file()


def test_files_a_purge_couldnt_remove_go_with_the_orphan_folders(seeder: Seeder, monkeypatch: pytest.MonkeyPatch):
    retention_days = get_ingest_settings().RETENTION_DAYS
    committed = seeder.job(IngestStatus.committed, age=timedelta(days=retention_days + 1))
    recent = seeder.job(IngestStatus.committed, age=timedelta(days=retention_days - 1))
    real_remove = storage.remove_job_dir

    def remove_fails(group_id: UUID, job_id: UUID) -> bool:
        raise PermissionError("not this time")

    monkeypatch.setattr(storage, "remove_job_dir", remove_fails)
    retention.purge_once(utcnow())
    assert _row(committed)["draft"] is None  # slimmed, its folder still there
    assert seeder.dir(committed).is_dir()

    monkeypatch.setattr(storage, "remove_job_dir", real_remove)
    stale = time.time() - limits.ORPHAN_DIR_AGE - 60
    for job_id in (committed, recent):
        os.utime(seeder.dir(job_id), (stale, stale))
    retention.purge_once(utcnow())
    assert not seeder.dir(committed).exists()
    assert _row(committed) is not None  # the row stays, as for any slimmed card
    assert seeder.dir(recent).is_dir()  # a card within retention keeps its files however old its folder


def test_a_failed_card_being_merged_into_isnt_purged_under_the_merge(seeder: Seeder):
    """
    The purge takes the household's merge lock: a merge into a failed card past retention moves its page and changes
    the card before the purge looks again, so the card and the moved page stay
    """
    from mealie.services.ai.ingest import review

    failed = seeder.job(IngestStatus.failed, age=timedelta(days=get_ingest_settings().RETENTION_DAYS + 1))
    entered, release = threading.Event(), threading.Event()

    def merge() -> None:
        with session_context() as session, review.household_merge_lock(session, seeder.household_id):
            entered.set()
            release.wait(10)
            moved = seeder.dir(failed) / "pages" / "1"
            moved.mkdir(parents=True)
            (moved / "page.jpg").write_bytes(b"the source's page")
            session.execute(
                sa.update(RecipeIngestionJob)
                .where(RecipeIngestionJob.id == failed)
                .values(update_at=utcnow(), row_version=RecipeIngestionJob.row_version + 1)
            )

    merging = threading.Thread(target=merge)
    merging.start()
    assert entered.wait(10)
    purging = threading.Thread(target=retention.purge_once, args=(utcnow(),))
    purging.start()
    time.sleep(0.5)  # the purge waits for the merge
    release.set()
    merging.join(30)
    purging.join(30)

    assert _row(failed) is not None
    assert (seeder.dir(failed) / "pages" / "1" / "page.jpg").is_file()
