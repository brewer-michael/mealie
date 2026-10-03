"""
Fork-owned data access for recipe card ingestion (docs/ai/PHASE2.md §3, §13). Outside `AllRepositories`, so upstream's
repository factory stays untouched.

**Rules every write here follows** (§3.3, F12):
- A state change is a Core `sa.update(Model).where(Model.id == id, <conditions>)`, and its rowcount says whether it
  happened. Every check lives in that `WHERE`, never in an earlier `SELECT`: SQLite's driver doesn't begin a
  transaction before a `SELECT`, so `SELECT ... FOR UPDATE` then `UPDATE` isn't atomic there.
- Never raw SQL with UUIDs (dashed UUIDs match nothing in SQLite's GUID columns).
- Each `UPDATE` sets only the columns it owns, and any write to a column in `VERSIONED_COLUMNS` bumps `row_version`.
- Read-modify-writes of the JSON columns go through `update_job_json`, which is optimistic on `row_version`.
- Every time compared or stored is `utcnow()`, bound from Python: never `func.now()` or `CURRENT_TIMESTAMP`, which
  PostgreSQL gives in the server's own time zone.
- Writes commit their own transaction unless they take `commit=False` and the caller commits. Reads load fresh rows
  (`populate_existing`), since Core updates don't refresh objects already in the session.

**Public interface**
- `utcnow()`: the naive-UTC "now" that every ingest query binds.
- `JobConflict`: `update_job_json` lost the race for `row_version` four times in a row.
- `JobWrite(before, values)`: what `update_job_json` read and what it wrote.
- `update_job_json(session, job_id, mutate, *, where=(), scope=())`: reads the job (matching `where` and `scope`),
  calls `mutate(row)` for the columns to set (None writes nothing), and writes them with
  `WHERE id AND row_version=:rv AND <where>`, bumping `row_version`. Zero rows means re-read and retry, 3 times, then
  `JobConflict`. Returns None when no row matches.
- `enqueue_task(session, job_id, household_id, kind, payload, priority, *, where=(), values=None, commit=True)`: gives
  an idle job (`task_state IS NULL`) a new queued task with clean counters (`attempts=0`, `rate_limit_retries=0`,
  `not_before=NULL`, `cancel_requested=false`, lease cleared); `values` sets other columns in the same `UPDATE` (e.g.
  `status` for a retry). Whether it happened.
- `cancel_task(session, job_id, *, household_id=None) -> CancelOutcome`: clears a queued task (a `processing` job then
  fails with `cancelled`; a `ready` one stays ready) or asks a running one to stop (`cancel_requested`).
- `IngestRepos(session, group_id, household_id)`, scoped to the group and household:
  - `.jobs`: `get`, `page`, `counts`, `find_duplicate`, `next_position`, `create`, `update_job_json`,
    `enqueue_task`, `cancel_task`, `delete`, and `scope` (the household conditions, for custom queries);
  - `.batches`: `get`, `create`, `touch` (conditional on not sealed), `seal` (optionally only when idle),
    `find_open` (the batch an upload auto-joins), `jobs`, `counts`;
  - `.settings` (group-scoped): `get` (defaults when there's no row), `upsert`;
  - `.notifier_options`: `get`, `set` (for the household's notifiers), `enabled_notifier_ids`;
  - `processing_jobs_in_group()`, for the per-group quota.
- `IngestQueue(session)`, across households, for the runner: `get`, `queued_ids`, `claim`, `heartbeat`,
  `set_progress`, `expired`, `requeue_expired`, `release`, `update_job_json`.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mealie.db.models.household.events import GroupEventNotifierModel
from mealie.db.models.recipe_ingest import (
    AIEventNotifierOptions,
    RecipeIngestionBatch,
    RecipeIngestionJob,
    RecipeIngestionSettings,
)
from mealie.schema.recipe_ingest import (
    AINotifierEventsOut,
    IngestErrorCode,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    RecipeIngestionJobCounts,
    RecipeIngestionSettingsUpdate,
)
from mealie.services.ai.ingest import limits

Job = RecipeIngestionJob
Batch = RecipeIngestionBatch

UPDATE_RETRIES = 3
"""Re-reads after a lost `row_version` race before `JobConflict`"""

VERSIONED_COLUMNS = frozenset(
    {
        "status",
        "title",
        "draft",
        "draft_version",
        "extracted_version",
        "flags",
        "proposals",
        "pages",
        "extraction",
        "transcription",
        "error_code",
        "error_params",
        "error_count",
        "warning_count",
    }
)
"""Columns whose every write bumps `row_version`, so an optimistic read-modify-write never overwrites it unseen"""

_NO_SYNC = {"synchronize_session": False}
_FRESH = {"populate_existing": True}

JobMutation = Callable[[RowMapping], Mapping[str, Any] | None]
"""Given the job as read (column name to value, JSON already parsed), the columns to set; None to write nothing"""


def utcnow() -> datetime:
    """Now in UTC, naive: what every ingest query binds and stores (the columns are naive-UTC `NaiveDateTime`s)"""
    return datetime.now(UTC).replace(tzinfo=None)


class JobConflict(Exception):
    """A job's `row_version` kept changing between the read and the write; nothing was written"""


class CancelOutcome(StrEnum):
    cancelled = "cancelled"
    """A queued task was cleared"""
    requested = "requested"
    """A running task was asked to stop; it stops within a heartbeat"""
    idle = "idle"
    """The job had no task"""


@dataclass(frozen=True)
class JobWrite:
    before: RowMapping
    """The job as `mutate` saw it"""
    values: dict[str, Any]
    """The columns written, the new `row_version` included"""


@dataclass(frozen=True)
class ExpiredLease:
    job_id: UUID
    token: UUID
    attempts: int
    status: IngestStatus


def _rowcount(result: sa.Result) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


def _end_transaction(session: Session) -> None:
    if session.in_transaction():
        session.commit()


def _versioned(values: Mapping[str, Any]) -> dict[str, Any]:
    """`values`, plus the `row_version` bump when it writes a versioned column"""
    written = dict(values)
    written.pop("row_version", None)
    if VERSIONED_COLUMNS.intersection(written):
        written["row_version"] = Job.row_version + 1
    return written


def _execute_update(session: Session, stmt: sa.Update) -> int:
    return _rowcount(session.execute(stmt, execution_options=_NO_SYNC))


def update_job_json(
    session: Session,
    job_id: UUID,
    mutate: JobMutation,
    *,
    where: Sequence[sa.ColumnElement[bool]] = (),
    scope: Sequence[sa.ColumnElement[bool]] = (),
) -> JobWrite | None:
    """
    An optimistic read-modify-write of one job (§3.3). Reads the job where `scope` and `where` hold, calls `mutate`
    with it, and writes the columns it returns with `WHERE id=:id AND row_version=:rv AND <scope> AND <where>`,
    bumping `row_version`. When that matches nothing the job is read again (and `mutate` called again), up to
    `UPDATE_RETRIES` times, then `JobConflict` is raised.

    Returns None, writing nothing, when no job matches or `mutate` returns None. Commits its own transaction.
    """
    columns = Job.__table__.columns
    for _ in range(UPDATE_RETRIES + 1):
        row = session.execute(sa.select(*columns).where(Job.id == job_id, *scope, *where)).mappings().one_or_none()
        if row is None:
            _end_transaction(session)
            return None

        try:
            values = mutate(row)
        except BaseException:
            _end_transaction(session)  # nothing was written; don't leave the read's transaction open
            raise
        if values is None:
            _end_transaction(session)
            return None

        written = dict(values)
        written.pop("row_version", None)
        written["row_version"] = row["row_version"] + 1
        stmt = (
            sa.update(Job)
            .where(Job.id == job_id, Job.row_version == row["row_version"], *scope, *where)
            .values(**written)
        )
        try:
            updated = _execute_update(session, stmt)
        except BaseException:
            session.rollback()
            raise
        if updated == 1:
            session.commit()
            return JobWrite(before=row, values=written)

        _end_transaction(session)

    raise JobConflict(f"Recipe card job {job_id} kept changing; nothing was written")


def _task_reset(kind: IngestTaskKind, payload: Any, priority: int) -> dict[str, Any]:
    return {
        "task_kind": kind.value,
        "task_state": IngestTaskState.queued.value,
        "task_priority": priority,
        "task_payload": payload,
        "attempts": 0,
        "rate_limit_retries": 0,
        "not_before": None,
        "cancel_requested": False,
        "lease_token": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "task_started_at": None,
        "progress_key": None,
    }


TASK_CLEARED: dict[str, Any] = {
    "task_kind": None,
    "task_state": None,
    "task_payload": None,
    "not_before": None,
    "cancel_requested": False,
    "lease_token": None,
    "lease_owner": None,
    "lease_expires_at": None,
    "task_started_at": None,
    "progress_key": None,
}
"""The values that leave a job with no task"""


def enqueue_task(
    session: Session,
    job_id: UUID,
    household_id: UUID,
    kind: IngestTaskKind,
    payload: Any,
    priority: int,
    *,
    where: Sequence[sa.ColumnElement[bool]] = (),
    values: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> bool:
    """
    Gives an idle job of the household a new queued task, with clean counters: the counters belong to one task, not
    the job's history (§3.1). Conditional on `task_state IS NULL` and `where`; `values` sets other columns in the same
    `UPDATE`. Whether it happened. Wake the dispatcher afterwards.
    """
    stmt = (
        sa.update(Job)
        .where(Job.id == job_id, Job.household_id == household_id, Job.task_state.is_(None), *where)
        .values(**_versioned({**(values or {}), **_task_reset(kind, payload, priority)}))
    )
    queued = _execute_update(session, stmt) == 1
    if commit:
        _end_transaction(session)
    return queued


def cancel_task(session: Session, job_id: UUID, *, household_id: UUID | None = None) -> CancelOutcome:
    """
    Cancels a job's task (§3.5). A queued task is cleared at once: a `processing` job becomes `failed` with
    `cancelled`, a `ready` job stays ready. A running task gets `cancel_requested`, and the dispatcher stops it within
    a heartbeat. Commits.
    """
    scope = [Job.id == job_id]
    if household_id is not None:
        scope.append(Job.household_id == household_id)
    queued = Job.task_state == IngestTaskState.queued.value

    try:
        failed = sa.update(Job).where(*scope, queued, Job.status == IngestStatus.processing.value)
        failed = failed.values(
            **_versioned(
                {
                    **TASK_CLEARED,
                    "status": IngestStatus.failed.value,
                    "error_code": IngestErrorCode.cancelled.value,
                    "error_params": None,
                }
            )
        )
        if _execute_update(session, failed) == 1:
            return CancelOutcome.cancelled

        cleared = sa.update(Job).where(*scope, queued, Job.status != IngestStatus.processing.value)
        if _execute_update(session, cleared.values(**TASK_CLEARED)) == 1:
            return CancelOutcome.cancelled

        running = sa.update(Job).where(*scope, Job.task_state == IngestTaskState.running.value)
        if _execute_update(session, running.values(cancel_requested=True)) == 1:
            return CancelOutcome.requested

        return CancelOutcome.idle
    finally:
        _end_transaction(session)


# ==================================================================================================================
# Household-scoped access


class IngestJobsRepo:
    """The household's jobs"""

    def __init__(self, session: Session, group_id: UUID, household_id: UUID) -> None:
        self.session = session
        self.group_id = group_id
        self.household_id = household_id

    @property
    def scope(self) -> list[sa.ColumnElement[bool]]:
        """The conditions that keep a query to the household's jobs"""
        return [Job.group_id == self.group_id, Job.household_id == self.household_id]

    def get(self, job_id: UUID) -> RecipeIngestionJob | None:
        stmt = sa.select(Job).where(Job.id == job_id, *self.scope).execution_options(**_FRESH)
        return self.session.execute(stmt).scalars().one_or_none()

    def page(
        self,
        *,
        statuses: Iterable[IngestStatus] | None = None,
        batch_id: UUID | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[RecipeIngestionJob], int]:
        """A page of the household's jobs, newest first, and how many match in all. `per_page=-1` returns all."""
        conditions = list(self.scope)
        if statuses is not None:
            conditions.append(Job.status.in_([status.value for status in statuses]))
        if batch_id is not None:
            conditions.append(Job.batch_id == batch_id)

        total = self.session.execute(sa.select(sa.func.count()).select_from(Job).where(*conditions)).scalar_one()
        stmt = (
            sa.select(Job)
            .where(*conditions)
            .order_by(Job.created_at.desc(), Job.position.desc(), Job.id)
            .execution_options(**_FRESH)
        )
        if per_page > 0:
            stmt = stmt.offset(max(page - 1, 0) * per_page).limit(per_page)
        return list(self.session.execute(stmt).scalars()), total

    def counts(self, *, batch_id: UUID | None = None) -> RecipeIngestionJobCounts:
        """Processing, ready, ready with something to check, and failed jobs"""
        return _counts(self.session, [*self.scope, *([Job.batch_id == batch_id] if batch_id else [])])

    def find_duplicate(self, source_sha256: str) -> UUID | None:
        """The household's oldest job holding the same card (committed ones included)"""
        stmt = (
            sa.select(Job.id)
            .where(*self.scope, Job.source_sha256 == source_sha256)
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def next_position(self, batch_id: UUID) -> int:
        """One past the batch's highest position: arrival order for cards that don't send one"""
        stmt = sa.select(sa.func.max(Job.position)).where(*self.scope, Job.batch_id == batch_id)
        highest = self.session.execute(stmt).scalar_one_or_none()
        return 0 if highest is None else highest + 1

    def create(self, values: Mapping[str, Any], *, commit: bool = True) -> UUID:
        """
        Inserts a job for the household. `values` are column values (`id` defaults to a new one); `group_id` and
        `household_id` always come from the repository.
        """
        unknown = set(values) - set(Job.__table__.columns.keys())
        if unknown:
            raise ValueError(f"Not columns of recipe_ingestion_jobs: {sorted(unknown)}")

        job_id = values.get("id") or uuid4()
        now = utcnow()
        row = {
            "created_at": now,
            "update_at": now,
            **values,
            "id": job_id,
            "group_id": self.group_id,
            "household_id": self.household_id,
        }
        self.session.execute(sa.insert(Job).values(**row))
        if commit:
            self.session.commit()
        return job_id

    def update_job_json(
        self, job_id: UUID, mutate: JobMutation, *, where: Sequence[sa.ColumnElement[bool]] = ()
    ) -> JobWrite | None:
        """`update_job_json` for one of the household's jobs"""
        return update_job_json(self.session, job_id, mutate, where=where, scope=self.scope)

    def enqueue_task(
        self,
        job_id: UUID,
        kind: IngestTaskKind,
        payload: Any,
        priority: int,
        *,
        where: Sequence[sa.ColumnElement[bool]] = (),
        values: Mapping[str, Any] | None = None,
    ) -> bool:
        return enqueue_task(
            self.session,
            job_id,
            self.household_id,
            kind,
            payload,
            priority,
            where=[Job.group_id == self.group_id, *where],
            values=values,
        )

    def cancel_task(self, job_id: UUID) -> CancelOutcome:
        return cancel_task(self.session, job_id, household_id=self.household_id)

    def delete(self, job_id: UUID, *, where: Sequence[sa.ColumnElement[bool]] = ()) -> bool:
        """Deletes the job's row where `where` holds; whether it did. The caller removes its files."""
        result = self.session.execute(
            sa.delete(Job).where(Job.id == job_id, *self.scope, *where), execution_options=_NO_SYNC
        )
        self.session.commit()
        return _rowcount(result) == 1


def _counts(session: Session, conditions: Sequence[sa.ColumnElement[bool]]) -> RecipeIngestionJobCounts:
    def count_where(*criteria: sa.ColumnElement[bool]) -> sa.ColumnElement[int]:
        return sa.func.coalesce(sa.func.sum(sa.case((sa.and_(*criteria), 1), else_=0)), 0)

    ready = Job.status == IngestStatus.ready.value
    stmt = sa.select(
        count_where(Job.status == IngestStatus.processing.value),
        count_where(ready),
        count_where(ready, sa.or_(Job.error_count > 0, Job.warning_count > 0)),
        count_where(Job.status == IngestStatus.failed.value),
    ).where(*conditions)
    processing, ready_count, needs_attention, failed = session.execute(stmt).one()
    return RecipeIngestionJobCounts(
        processing=processing, ready=ready_count, needs_attention=needs_attention, failed=failed
    )


class IngestBatchesRepo:
    """The household's batches (§1.4)"""

    def __init__(self, session: Session, group_id: UUID, household_id: UUID) -> None:
        self.session = session
        self.group_id = group_id
        self.household_id = household_id

    @property
    def scope(self) -> list[sa.ColumnElement[bool]]:
        return [Batch.group_id == self.group_id, Batch.household_id == self.household_id]

    def get(self, batch_id: UUID) -> RecipeIngestionBatch | None:
        stmt = sa.select(Batch).where(Batch.id == batch_id, *self.scope).execution_options(**_FRESH)
        return self.session.execute(stmt).scalars().one_or_none()

    def create(
        self,
        *,
        source: IngestSource,
        created_by: UUID | None,
        source_key: str | None = None,
        locale: str | None = None,
        now: datetime | None = None,
        commit: bool = True,
    ) -> UUID:
        """A new, unsealed batch; its idle time counts from now"""
        now = now or utcnow()
        batch_id = uuid4()
        self.session.execute(
            sa.insert(Batch).values(
                id=batch_id,
                group_id=self.group_id,
                household_id=self.household_id,
                created_by=created_by,
                source=source.value,
                source_key=source_key,
                locale=locale,
                last_upload_at=now,
                created_at=now,
                update_at=now,
            )
        )
        if commit:
            self.session.commit()
        return batch_id

    def touch(self, batch_id: UUID, now: datetime, *, commit: bool = True) -> bool:
        """
        Records an upload into the batch, only while it isn't sealed: whether it is still open. Run it in the job
        insert's transaction, so a seal can't slip in between (§1.4).
        """
        stmt = (
            sa.update(Batch)
            .where(Batch.id == batch_id, *self.scope, Batch.sealed_at.is_(None))
            .values(last_upload_at=now)
        )
        touched = _execute_update(self.session, stmt) == 1
        if commit:
            _end_transaction(self.session)
        return touched

    def seal(self, batch_id: UUID, now: datetime, *, idle_before: datetime | None = None) -> bool:
        """Seals the batch unless it already is (and, with `idle_before`, only if its last upload was earlier)"""
        conditions = [Batch.id == batch_id, *self.scope, Batch.sealed_at.is_(None)]
        if idle_before is not None:
            conditions.append(Batch.last_upload_at < idle_before)
        sealed = _execute_update(self.session, sa.update(Batch).where(*conditions).values(sealed_at=now)) == 1
        _end_transaction(self.session)
        return sealed

    def find_open(
        self, *, source: IngestSource, created_by: UUID | None, source_key: str | None, active_since: datetime
    ) -> UUID | None:
        """The newest unsealed batch of the same uploader, source and source key with an upload since `active_since`"""
        conditions = [
            *self.scope,
            Batch.source == source.value,
            Batch.sealed_at.is_(None),
            Batch.last_upload_at >= active_since,
            Batch.created_by.is_(None) if created_by is None else Batch.created_by == created_by,
            Batch.source_key.is_(None) if source_key is None else Batch.source_key == source_key,
        ]
        stmt = sa.select(Batch.id).where(*conditions).order_by(Batch.last_upload_at.desc(), Batch.id).limit(1)
        return self.session.execute(stmt).scalar_one_or_none()

    def jobs(self, batch_id: UUID) -> list[RecipeIngestionJob]:
        """The batch's jobs in review order: `position`, then arrival"""
        stmt = (
            sa.select(Job)
            .where(Job.batch_id == batch_id, Job.group_id == self.group_id, Job.household_id == self.household_id)
            .order_by(Job.position, Job.created_at, Job.id)
            .execution_options(**_FRESH)
        )
        return list(self.session.execute(stmt).scalars())

    def counts(self, batch_id: UUID) -> RecipeIngestionJobCounts:
        return _counts(
            self.session,
            [Job.batch_id == batch_id, Job.group_id == self.group_id, Job.household_id == self.household_id],
        )


class IngestSettingsRepo:
    """The group's recipe card settings; no row means the defaults"""

    def __init__(self, session: Session, group_id: UUID) -> None:
        self.session = session
        self.group_id = group_id

    def get(self) -> RecipeIngestionSettingsUpdate:
        stmt = sa.select(RecipeIngestionSettings.local_only, RecipeIngestionSettings.cross_read).where(
            RecipeIngestionSettings.group_id == self.group_id
        )
        row = self.session.execute(stmt).one_or_none()
        if row is None:
            return RecipeIngestionSettingsUpdate()
        return RecipeIngestionSettingsUpdate(local_only=row.local_only, cross_read=row.cross_read)

    def upsert(self, settings: RecipeIngestionSettingsUpdate) -> RecipeIngestionSettingsUpdate:
        values = {"local_only": settings.local_only, "cross_read": settings.cross_read, "update_at": utcnow()}
        for _ in range(2):
            stmt = (
                sa.update(RecipeIngestionSettings)
                .where(RecipeIngestionSettings.group_id == self.group_id)
                .values(**values)
            )
            if _execute_update(self.session, stmt) == 1:
                self.session.commit()
                return settings

            try:
                self.session.execute(
                    sa.insert(RecipeIngestionSettings).values(
                        id=uuid4(), group_id=self.group_id, created_at=values["update_at"], **values
                    )
                )
                self.session.commit()
                return settings
            except IntegrityError:
                # another request inserted the group's row first: update that one
                self.session.rollback()

        raise JobConflict("The group's recipe card settings kept changing")


class AINotifierOptionsRepo:
    """Which AI events the household's notifiers send"""

    def __init__(self, session: Session, group_id: UUID, household_id: UUID) -> None:
        self.session = session
        self.group_id = group_id
        self.household_id = household_id

    def _owns(self, notifier_id: UUID) -> bool:
        stmt = sa.select(GroupEventNotifierModel.id).where(
            GroupEventNotifierModel.id == notifier_id,
            GroupEventNotifierModel.group_id == self.group_id,
            GroupEventNotifierModel.household_id == self.household_id,
        )
        return self.session.execute(stmt).scalar_one_or_none() is not None

    def get(self, notifier_id: UUID) -> AINotifierEventsOut | None:
        """The notifier's toggles (off without a row); None when it isn't one of the household's notifiers"""
        if not self._owns(notifier_id):
            return None
        stmt = sa.select(AIEventNotifierOptions.recipe_ingestion_ready).where(
            AIEventNotifierOptions.notifier_id == notifier_id
        )
        ready = self.session.execute(stmt).scalar_one_or_none()
        return AINotifierEventsOut(recipe_ingestion_ready=bool(ready))

    def set(self, notifier_id: UUID, *, recipe_ingestion_ready: bool) -> AINotifierEventsOut | None:
        """Saves the notifier's toggles; None when it isn't one of the household's notifiers"""
        if not self._owns(notifier_id):
            return None

        now = utcnow()
        for _ in range(2):
            stmt = (
                sa.update(AIEventNotifierOptions)
                .where(AIEventNotifierOptions.notifier_id == notifier_id)
                .values(recipe_ingestion_ready=recipe_ingestion_ready, update_at=now)
            )
            if _execute_update(self.session, stmt) == 1:
                self.session.commit()
                return AINotifierEventsOut(recipe_ingestion_ready=recipe_ingestion_ready)

            try:
                self.session.execute(
                    sa.insert(AIEventNotifierOptions).values(
                        id=uuid4(),
                        notifier_id=notifier_id,
                        recipe_ingestion_ready=recipe_ingestion_ready,
                        created_at=now,
                        update_at=now,
                    )
                )
                self.session.commit()
                return AINotifierEventsOut(recipe_ingestion_ready=recipe_ingestion_ready)
            except IntegrityError:
                self.session.rollback()

        raise JobConflict("The notifier's options kept changing")

    def enabled_notifier_ids(self) -> list[UUID]:
        """The household's enabled notifiers that send "recipe cards ready" """
        stmt = (
            sa.select(GroupEventNotifierModel.id)
            .join(AIEventNotifierOptions, AIEventNotifierOptions.notifier_id == GroupEventNotifierModel.id)
            .where(
                GroupEventNotifierModel.group_id == self.group_id,
                GroupEventNotifierModel.household_id == self.household_id,
                GroupEventNotifierModel.enabled.is_(True),
                AIEventNotifierOptions.recipe_ingestion_ready.is_(True),
            )
            .order_by(GroupEventNotifierModel.id)
        )
        return list(self.session.execute(stmt).scalars())


class IngestRepos:
    """
    Recipe card data for one group and household (§9): another household's job or batch simply isn't found. With
    `household_id=None` only the group-scoped parts (`settings`, `processing_jobs_in_group`) are available.
    """

    def __init__(self, session: Session, group_id: UUID, household_id: UUID | None) -> None:
        self.session = session
        self.group_id = group_id
        self.household_id = household_id

    def _household(self) -> UUID:
        if self.household_id is None:
            raise ValueError("This needs repositories scoped to a household")
        return self.household_id

    @property
    def jobs(self) -> IngestJobsRepo:
        return IngestJobsRepo(self.session, self.group_id, self._household())

    @property
    def batches(self) -> IngestBatchesRepo:
        return IngestBatchesRepo(self.session, self.group_id, self._household())

    @property
    def settings(self) -> IngestSettingsRepo:
        return IngestSettingsRepo(self.session, self.group_id)

    @property
    def notifier_options(self) -> AINotifierOptionsRepo:
        return AINotifierOptionsRepo(self.session, self.group_id, self._household())

    def processing_jobs_in_group(self) -> int:
        """The group's `processing` jobs, every household's, for the per-group quota (§1.2)"""
        stmt = (
            sa.select(sa.func.count())
            .select_from(Job)
            .where(Job.group_id == self.group_id, Job.status == IngestStatus.processing.value)
        )
        return self.session.execute(stmt).scalar_one()


# ==================================================================================================================
# The runner's view, across households


class IngestQueue:
    """
    The job table as a queue (§3.2): claims, heartbeats, expired leases and releases, every one a conditional
    `UPDATE` on the job's id. Runs across households; every write by a running task is fenced on its lease token.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, job_id: UUID) -> RecipeIngestionJob | None:
        stmt = sa.select(Job).where(Job.id == job_id).execution_options(**_FRESH)
        return self.session.execute(stmt).scalars().one_or_none()

    def queued_ids(self, now: datetime, limit: int, *, max_priority: int | None = None) -> list[UUID]:
        """
        Jobs whose task can run now, by priority then age. `max_priority` limits it to the more urgent tasks (e.g.
        `PRIORITY_REREAD` for the re-read slot).
        """
        if limit <= 0:
            return []
        conditions = [
            Job.task_state == IngestTaskState.queued.value,
            sa.or_(Job.not_before.is_(None), Job.not_before <= now),
        ]
        if max_priority is not None:
            conditions.append(Job.task_priority <= max_priority)
        stmt = sa.select(Job.id).where(*conditions).order_by(Job.task_priority, Job.created_at, Job.id).limit(limit)
        ids = list(self.session.execute(stmt).scalars())
        _end_transaction(self.session)
        return ids

    def claim(self, job_id: UUID, *, token: UUID, owner: str, now: datetime) -> bool:
        """Takes a queued task with a new lease token; whether this caller won it"""
        stmt = (
            sa.update(Job)
            .where(
                Job.id == job_id,
                Job.task_state == IngestTaskState.queued.value,
                sa.or_(Job.not_before.is_(None), Job.not_before <= now),
            )
            .values(
                task_state=IngestTaskState.running.value,
                lease_token=token,
                lease_owner=owner[:64],
                lease_expires_at=now + timedelta(seconds=limits.LEASE),
                task_started_at=now,
                attempts=Job.attempts + 1,
            )
        )
        claimed = _execute_update(self.session, stmt) == 1
        _end_transaction(self.session)
        return claimed

    def heartbeat(self, tokens: Iterable[UUID], now: datetime) -> dict[UUID, bool]:
        """
        Extends the leases of the tasks this process runs. Returns each token whose task is still running, with whether
        it was asked to stop; a token missing from the result was cleared (commit, discard, sweep, a restore).
        """
        held = list(tokens)
        if not held:
            return {}

        running = [Job.lease_token.in_(held), Job.task_state == IngestTaskState.running.value]
        renew = sa.update(Job).where(*running).values(lease_expires_at=now + timedelta(seconds=limits.LEASE))
        _execute_update(self.session, renew)
        self.session.commit()

        rows = self.session.execute(sa.select(Job.lease_token, Job.cancel_requested).where(*running)).all()
        _end_transaction(self.session)
        return {row.lease_token: bool(row.cancel_requested) for row in rows}

    def set_progress(self, job_id: UUID, token: UUID, progress_key: str | None) -> bool:
        """Stores a running task's progress key, fenced on its lease"""
        stmt = (
            sa.update(Job)
            .where(Job.id == job_id, Job.lease_token == token, Job.task_state == IngestTaskState.running.value)
            .values(progress_key=progress_key[:64] if progress_key else None)
        )
        stored = _execute_update(self.session, stmt) == 1
        _end_transaction(self.session)
        return stored

    def expired(self, now: datetime) -> list[ExpiredLease]:
        """Running tasks whose lease ran out (their process stopped heartbeating)"""
        stmt = sa.select(Job.id, Job.lease_token, Job.attempts, Job.status).where(
            Job.task_state == IngestTaskState.running.value, Job.lease_expires_at < now
        )
        leases = [
            ExpiredLease(job_id=row.id, token=row.lease_token, attempts=row.attempts, status=IngestStatus(row.status))
            for row in self.session.execute(stmt)
            if row.lease_token is not None
        ]
        _end_transaction(self.session)
        return leases

    def requeue_expired(self, job_id: UUID, token: UUID, now: datetime) -> bool:
        """Puts an expired task back in the queue while it has attempts left, fenced on its old token"""
        stmt = (
            sa.update(Job)
            .where(
                Job.id == job_id,
                Job.lease_token == token,
                Job.task_state == IngestTaskState.running.value,
                Job.lease_expires_at < now,
                Job.attempts < limits.MAX_ATTEMPTS,
            )
            .values(
                task_state=IngestTaskState.queued.value,
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                progress_key=None,
            )
        )
        requeued = _execute_update(self.session, stmt) == 1
        _end_transaction(self.session)
        return requeued

    def release(self, job_id: UUID, token: UUID, *, not_before: datetime | None = None) -> bool:
        """
        Gives back a running task without using up an attempt (shutdown, or a pause for a restore), fenced on its
        token: queued again, lease cleared, `attempts - 1`, not claimed before `not_before`.
        """
        stmt = (
            sa.update(Job)
            .where(Job.id == job_id, Job.lease_token == token, Job.task_state == IngestTaskState.running.value)
            .values(
                task_state=IngestTaskState.queued.value,
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                progress_key=None,
                not_before=not_before,
                attempts=sa.case((Job.attempts > 0, Job.attempts - 1), else_=0),
            )
        )
        released = _execute_update(self.session, stmt) == 1
        _end_transaction(self.session)
        return released

    def update_job_json(
        self, job_id: UUID, mutate: JobMutation, *, where: Sequence[sa.ColumnElement[bool]] = ()
    ) -> JobWrite | None:
        """`update_job_json` on any household's job; fence it with `where`"""
        return update_job_json(self.session, job_id, mutate, where=where)

    @staticmethod
    def fence(token: UUID) -> list[sa.ColumnElement[bool]]:
        """The conditions every write by a running task carries (§3.3)"""
        return [Job.lease_token == token, Job.task_state == IngestTaskState.running.value]
