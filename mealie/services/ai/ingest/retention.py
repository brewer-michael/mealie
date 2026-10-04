"""
Retention (docs/ai/PHASE2.md §16): the daily purge of committed and failed cards' files, empty batches and orphan
job directories (and of readings a restore cut off that no task used). Ready and committing jobs, eval cases and
`recipes/` are never touched. A failed card waiting for the monthly limits to reset counts its retention from its
automatic retry (`failed_card_expires_at`). Empty batches go a day after their last upload (`EMPTY_BATCH_AGE`),
sealed or not: an upload whose only card was refused as a duplicate, or one the app abandoned, leaves a batch nobody
seals.

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


def _slimmed() -> sa.ColumnElement[bool]:
    """A committed card the purge has slimmed: its files are no longer needed"""
    return sa.and_(
        Job.status == IngestStatus.committed.value, *(getattr(Job, column).is_(None) for column in SLIMMED_COLUMNS)
    )


def _purge_committed(session: Session, cutoff: datetime) -> int:
    """
    Committed cards past retention: their card text is cleared, the row stays, and then their files go. The row first:
    a card an undo took back to review since it was picked keeps its photos, as the update no longer matches it (and
    an undo after it finds the card purged). Files left by a removal that failed go with the orphan folders.
    """
    stmt = sa.select(Job.id, Job.group_id, Job.pages, Job.row_version).where(
        Job.status == IngestStatus.committed.value,
        sa.func.coalesce(Job.committed_at, Job.update_at) < cutoff,
        sa.not_(_slimmed()),
    )
    rows = session.execute(stmt).all()
    session.commit()

    purged = 0
    for row in rows:
        with storage.ingest_write():
            values: dict[str, object] = dict.fromkeys(SLIMMED_COLUMNS)
            values["pages"] = _slim_pages(row.pages) or []
            values["row_version"] = Job.row_version + 1
            update = (
                sa.update(Job)
                .where(
                    Job.id == row.id,
                    Job.row_version == row.row_version,  # unchanged since it was picked: committed, past retention
                    Job.status == IngestStatus.committed.value,
                )
                .values(**values)
            )
            if _execute(session, update) == 1:
                purged += 1
                try:
                    storage.remove_job_dir(row.group_id, row.id)
                except OSError:
                    logger.exception(
                        f"Couldn't remove the files of recipe card job {row.id}; the orphan folders' purge retries"
                    )
    return purged


def _failed_since() -> sa.ColumnElement[datetime]:
    """
    What a failed card's retention counts from: its automatic retry for one waiting for a monthly limit to reset
    (`auto_retry_at`, so it's kept until then and `RETENTION_DAYS` after), else its last change
    """
    return sa.func.coalesce(Job.auto_retry_at, Job.update_at, Job.created_at)


def failed_card_expires_at(job: RecipeIngestionJob) -> datetime | None:
    """When the purge removes a failed card (row and photos), counted as `_purge_failed` counts it; None otherwise"""
    if job.status != IngestStatus.failed.value:
        return None
    since = job.auto_retry_at or job.update_at or job.created_at
    return since + timedelta(days=get_ingest_settings().RETENTION_DAYS) if since else None


def _purge_failed(session: Session, cutoff: datetime) -> int:
    """
    Failed cards past retention: row and files. A card waiting for the monthly limits to reset is kept until
    `RETENTION_DAYS` after its automatic retry, which reads it again before then.
    """
    from .review import household_merge_lock

    stmt = sa.select(Job.id, Job.group_id, Job.household_id).where(
        Job.status == IngestStatus.failed.value,
        Job.task_state.is_(None),
        _failed_since() < cutoff,
    )
    rows = session.execute(stmt).all()
    session.commit()

    purged = 0
    for row in rows:
        # under the household's merge lock: a merge into this card moving a page into its folder meanwhile either
        # finished first (and changed the card, which the delete then no longer matches) or finds it gone
        with storage.ingest_write(), household_merge_lock(session, row.household_id):
            delete = sa.delete(Job).where(
                Job.id == row.id,
                Job.status == IngestStatus.failed.value,
                Job.task_state.is_(None),
                _failed_since() < cutoff,
            )
            if _execute(session, delete) == 1:
                storage.remove_job_dir(row.group_id, row.id)
                purged += 1
    return purged


def _purge_empty_batches(session: Session, cutoff: datetime) -> int:
    """
    Batches with no cards whose last upload (or creation, if none came) is before `cutoff`, sealed or not. An upload
    into one meanwhile touches it first (§1.4), so the `WHERE` no longer matches it; one that comes after the delete
    gets "batch not found", and the app starts another batch.
    """
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
    """Those of `job_ids` whose rows still need their folders: every one but a slimmed committed card's"""
    existing: set[UUID] = set()
    for start in range(0, len(job_ids), 500):
        chunk = list(job_ids[start : start + 500])
        existing.update(session.execute(sa.select(Job.id).where(Job.id.in_(chunk), sa.not_(_slimmed()))).scalars())
    session.commit()
    return existing


def _purge_orphan_dirs(session: Session, now: datetime) -> int:
    """
    Job directories with no row, untouched for `ORPHAN_DIR_AGE` (a crash before the insert, a discard racing a task, a
    restore mismatch), or a slimmed committed card's that `_purge_committed` couldn't remove. The age keeps an intake
    that is still inserting its row safe.
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
            if session.execute(sa.select(Job.id).where(Job.id == job_id, sa.not_(_slimmed()))).first() is None:
                shutil.rmtree(storage.job_dir(group_id, job_id), ignore_errors=True)
                removed += 1
            session.commit()
    return removed


def purge_once(now: datetime) -> None:
    """One idempotent pass of the purge; each job's file work runs inside the ingest write lock"""
    from .runner import results

    cutoff = now - timedelta(days=get_ingest_settings().RETENTION_DAYS)
    with session_context() as session:
        try:
            committed = _purge_committed(session, cutoff)
            failed = _purge_failed(session, cutoff)
            batches = _purge_empty_batches(session, now - timedelta(seconds=limits.EMPTY_BATCH_AGE))
            orphans = _purge_orphan_dirs(session, now)
        except IngestPaused:
            logger.info("The recipe card purge stopped: a backup restore is pausing ingestion")
            return
    # readings a restore cut off that no task used within a day (a runtime folder: no write lock needed)
    kept = results.purge(now.replace(tzinfo=UTC).timestamp())

    if committed or failed or batches or orphans or kept:
        logger.info(
            f"Recipe card purge: {committed} committed card(s) slimmed, {failed} failed card(s), "
            f"{batches} empty batch(es), {orphans} orphan folder(s) and {kept} kept reading(s) removed"
        )
