"""
Retention (docs/ai/PHASE2.md §16): the daily purge of committed and failed cards' files, empty batches and orphan
job directories. Ready and committing jobs, eval cases and `recipes/` are never touched.

`purge_once` is idempotent, so every worker process running it once a day is harmless. Each job's file work runs inside
the ingest write lock (§3.9), and every row change is conditional on the state that made it purgeable, so a job retried
or committed meanwhile is left alone. While a backup restore holds ingestion the pass stops where it is.
"""

import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from mealie.core.config import get_app_dirs
from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.schema.recipe_ingest import IngestStatus
from mealie.services.ai.errors import IngestPaused
from mealie.services.ai.ingest import limits, storage
from mealie.services.ai.ingest.settings import get_ingest_settings

logger = get_logger(__name__)

Job = RecipeIngestionJob
Batch = RecipeIngestionBatch

SLIMMED_COLUMNS = ("transcription", "draft", "flags", "proposals", "extraction", "title")
"""Card text a committed job no longer needs once its retention has passed; the row keeps the recipe link and hash"""


def _rowcount(result: object) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


def _execute(session: Session, stmt: sa.Update | sa.Delete) -> int:
    try:
        count = _rowcount(session.execute(stmt, execution_options={"synchronize_session": False}))
    except BaseException:
        session.rollback()
        raise
    session.commit()
    return count


def _slim_pages(pages: object) -> list | None:
    """The pages without the OCR text read while orienting them (card text), which went with the files"""
    if not isinstance(pages, list):
        return None
    slimmed = []
    for page in pages:
        if isinstance(page, dict):
            page = {**page, "ocr": None}
        slimmed.append(page)
    return slimmed


def _purge_committed(session: Session, cutoff: datetime) -> int:
    """Committed cards past retention: their files go, their card text is cleared, the row stays"""
    not_slim = sa.or_(*(getattr(Job, column).is_not(None) for column in SLIMMED_COLUMNS))
    stmt = sa.select(Job.id, Job.group_id, Job.pages).where(
        Job.status == IngestStatus.committed.value,
        sa.func.coalesce(Job.committed_at, Job.update_at) < cutoff,
        not_slim,
    )
    rows = session.execute(stmt).all()
    session.commit()

    purged = 0
    for row in rows:
        with storage.ingest_write():
            storage.remove_job_dir(row.group_id, row.id)
            values: dict[str, object] = dict.fromkeys(SLIMMED_COLUMNS)
            values["pages"] = _slim_pages(row.pages) or []
            values["row_version"] = Job.row_version + 1
            update = sa.update(Job).where(Job.id == row.id, Job.status == IngestStatus.committed.value).values(**values)
            purged += _execute(session, update)
    return purged


def _purge_failed(session: Session, cutoff: datetime) -> int:
    """Failed cards past retention: row and files"""
    stmt = sa.select(Job.id, Job.group_id).where(
        Job.status == IngestStatus.failed.value,
        Job.task_state.is_(None),
        sa.func.coalesce(Job.update_at, Job.created_at) < cutoff,
    )
    rows = session.execute(stmt).all()
    session.commit()

    purged = 0
    for row in rows:
        with storage.ingest_write():
            delete = sa.delete(Job).where(
                Job.id == row.id,
                Job.status == IngestStatus.failed.value,
                Job.task_state.is_(None),
                sa.func.coalesce(Job.update_at, Job.created_at) < cutoff,
            )
            if _execute(session, delete) == 1:
                storage.remove_job_dir(row.group_id, row.id)
                purged += 1
    return purged


def _purge_empty_batches(session: Session, cutoff: datetime) -> int:
    """Batches with no cards left whose last upload is past retention"""
    no_jobs = ~sa.exists().where(Job.batch_id == Batch.id)
    delete = sa.delete(Batch).where(sa.func.coalesce(Batch.last_upload_at, Batch.created_at) < cutoff, no_jobs)
    return _execute(session, delete)


def _job_dirs() -> list[tuple[UUID, UUID, float]]:
    """Every `groups/<group>/ai-ingest/<job>/` directory: group id, job id, modification time"""
    found: list[tuple[UUID, UUID, float]] = []
    groups_dir = get_app_dirs().GROUPS_DIR
    if not groups_dir.is_dir():
        return found
    for group_dir in groups_dir.iterdir():
        ingest = group_dir / storage.INGEST_DIR_NAME
        try:
            group_id = UUID(group_dir.name)
        except ValueError:
            continue
        if not ingest.is_dir() or ingest.is_symlink():
            continue
        for entry in ingest.iterdir():
            try:
                job_id = UUID(entry.name)
                stat = entry.lstat()
            except ValueError, FileNotFoundError:
                continue
            if entry.is_dir() and not entry.is_symlink():
                found.append((group_id, job_id, stat.st_mtime))
    return found


def _existing_jobs(session: Session, job_ids: Sequence[UUID]) -> set[UUID]:
    existing: set[UUID] = set()
    for start in range(0, len(job_ids), 500):
        chunk = list(job_ids[start : start + 500])
        existing.update(session.execute(sa.select(Job.id).where(Job.id.in_(chunk))).scalars())
    session.commit()
    return existing


def _purge_orphan_dirs(session: Session, now: datetime) -> int:
    """
    Job directories with no row, untouched for `ORPHAN_DIR_AGE` (a crash before the insert, a discard racing a task, a
    restore mismatch). The age keeps an intake that is still inserting its row safe.
    """
    oldest = now.replace(tzinfo=UTC).timestamp() - limits.ORPHAN_DIR_AGE
    candidates = [(group_id, job_id) for group_id, job_id, mtime in _job_dirs() if mtime < oldest]
    if not candidates:
        return 0

    existing = _existing_jobs(session, [job_id for _, job_id in candidates])
    removed = 0
    for group_id, job_id in candidates:
        if job_id in existing:
            continue
        with storage.ingest_write():
            # a job inserted since the first look keeps its directory
            if session.execute(sa.select(Job.id).where(Job.id == job_id)).first() is None:
                shutil.rmtree(storage.job_dir(group_id, job_id), ignore_errors=True)
                removed += 1
            session.commit()
    return removed


def purge_once(now: datetime) -> None:
    """One idempotent pass of the purge; each job's file work runs inside the ingest write lock"""
    cutoff = now - timedelta(days=get_ingest_settings().RETENTION_DAYS)
    with session_context() as session:
        try:
            committed = _purge_committed(session, cutoff)
            failed = _purge_failed(session, cutoff)
            batches = _purge_empty_batches(session, cutoff)
            orphans = _purge_orphan_dirs(session, now)
        except IngestPaused:
            logger.info("The recipe card purge stopped: a backup restore is pausing ingestion")
            return

    if committed or failed or batches or orphans:
        logger.info(
            f"Recipe card purge: {committed} committed card(s) slimmed, {failed} failed card(s), "
            f"{batches} empty batch(es) and {orphans} orphan folder(s) removed"
        )
