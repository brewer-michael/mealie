"""
Batches (docs/ai/PHASE2.md §1.4): choosing the batch an upload joins, and sealing idle ones. A sealed batch never
gains a card: the job insert touches its batch with an `UPDATE` conditional on `sealed_at IS NULL`, in the insert's
transaction, and picks another batch when that matches nothing.

- **App batches** are explicit (`POST /ingest/batches`), sealed by the app's Done or after `APP_BATCH_IDLE`.
- **API and inbox uploads auto-join** the newest unsealed batch of the same household, uploader, source and source key
  that saw an upload in the last `AUTO_BATCH_IDLE`; otherwise they start one. `batch_id="new"` always starts one.
- **A sealed batch is never reopened.** A card sent to one goes where an upload without a batch would go, keeping the
  sealed batch's source (an app card joins the uploader's recent app batch or starts one), and the 202 says which.
- **Sealing can't race an insert.** Sealing is `UPDATE ... SET sealed_at=:now WHERE id=:b AND sealed_at IS NULL` (plus
  `AND last_upload_at < :cutoff` when idle). The insert's touch is `UPDATE ... SET last_upload_at=:now WHERE id=:b AND
  sealed_at IS NULL`, held until the insert commits: SQLite's write lock or PostgreSQL's row lock makes a waiting seal
  re-check its `WHERE` after the insert. Every time is `utcnow()`, bound from Python.
"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from mealie.core.exceptions import NoEntryFound
from mealie.db.models.recipe_ingest import RecipeIngestionBatch
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import IngestSource

from . import limits

Batch = RecipeIngestionBatch


def select_batch(
    repos: IngestRepos,
    *,
    batch_id: UUID | Literal["new"] | None,
    source: IngestSource,
    created_by: UUID | None,
    source_key: str | None,
    locale: str | None,
    now: datetime,
) -> UUID:
    """
    The batch an upload goes into: the app's explicit batch while it's unsealed; otherwise (no `batch_id`) the newest
    unsealed batch of the same uploader, source and source key that saw an upload in the last 2 minutes; otherwise,
    or with `batch_id="new"`, a new batch. An unknown or foreign `batch_id` raises `NoEntryFound`.

    A sealed explicit batch is replaced by the batch an upload without one would get, for the sealed batch's own
    source and source key. Runs in the caller's transaction: a batch it creates is committed (or rolled back) with
    the job, so a rejected card never leaves an empty batch behind. The caller then touches the batch (`touch`),
    which is what guarantees it is still open.
    """
    if batch_id == "new":
        return repos.batches.create(
            source=source, created_by=created_by, source_key=source_key, locale=locale, now=now, commit=False
        )

    if batch_id is not None:
        batch = repos.batches.get(batch_id)
        if batch is None:
            raise NoEntryFound(f"Recipe card batch {batch_id} not found")
        if batch.sealed_at is None:
            return batch_id
        # never reopened: the card goes where an upload without a batch would, with the sealed batch's source
        source = IngestSource(batch.source)
        source_key = batch.source_key

    open_batch = repos.batches.find_open(
        source=source,
        created_by=created_by,
        source_key=source_key,
        active_since=now - timedelta(seconds=limits.AUTO_BATCH_IDLE),
    )
    if open_batch is not None:
        return open_batch
    return repos.batches.create(
        source=source, created_by=created_by, source_key=source_key, locale=locale, now=now, commit=False
    )


def touch(repos: IngestRepos, batch_id: UUID, now: datetime) -> bool:
    """
    Records an upload into the batch, in the caller's (the job insert's) transaction, only while the batch is unsealed:
    whether it was. Until that transaction ends, a seal of the batch waits and then re-checks its `WHERE`.
    """
    return repos.batches.touch(batch_id, now, commit=False)


def batch_source(session: Session, batch_id: UUID) -> IngestSource:
    """A batch's source, which its jobs share"""
    return IngestSource(session.execute(sa.select(Batch.source).where(Batch.id == batch_id)).scalar_one())


def next_position(repos: IngestRepos, batch_id: UUID, requested: int | None) -> int:
    """A card's place in its batch: the app's capture index when it sent one, else after the batch's last card"""
    if requested is not None:
        return requested
    return repos.jobs.next_position(batch_id)


def seal(repos: IngestRepos, batch_id: UUID, now: datetime) -> bool:
    """
    Seals one of the household's batches (the app's Done): `UPDATE ... SET sealed_at WHERE id AND sealed_at IS NULL`.
    Whether this call sealed it; False when it already was (or isn't the household's). Commits.
    """
    return repos.batches.seal(batch_id, now)


def _idle(now: datetime) -> sa.ColumnElement[bool]:
    """Unsealed batches whose last upload is older than their kind's idle time (app 10 minutes, API and inbox 2)"""
    last_upload = sa.func.coalesce(Batch.last_upload_at, Batch.created_at)
    app_cutoff = now - timedelta(seconds=limits.APP_BATCH_IDLE)
    auto_cutoff = now - timedelta(seconds=limits.AUTO_BATCH_IDLE)
    return sa.and_(
        Batch.sealed_at.is_(None),
        sa.or_(
            sa.and_(Batch.source == IngestSource.app.value, last_upload < app_cutoff),
            sa.and_(Batch.source != IngestSource.app.value, last_upload < auto_cutoff),
        ),
    )


def seal_idle_batches(session: Session, now: datetime) -> list[UUID]:
    """Seals every household's batches idle for too long (app 10 minutes, API and inbox 2); returns their ids"""
    candidates = list(session.execute(sa.select(Batch.id).where(_idle(now)).order_by(Batch.created_at)).scalars())
    if session.in_transaction():
        session.commit()

    sealed: list[UUID] = []
    for batch_id in candidates:
        # the idle condition is checked again by the UPDATE itself: an upload touching the batch meanwhile wins
        stmt = sa.update(Batch).where(Batch.id == batch_id, _idle(now)).values(sealed_at=now)
        try:
            result = session.execute(stmt, execution_options={"synchronize_session": False})
            if isinstance(result, CursorResult) and result.rowcount == 1:
                sealed.append(batch_id)
            session.commit()
        except BaseException:
            session.rollback()
            raise
    return sealed
