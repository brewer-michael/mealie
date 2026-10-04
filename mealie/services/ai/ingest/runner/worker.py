"""
Running one claimed task (docs/ai/PHASE2.md §3.7), in its own daemon thread and event loop:

1. Read the job in a short session; stop if the fence (the lease token, `task_state='running'`) fails.
2. Set the locale context to the job's language (the uploader's `Accept-Language`; en-US for the inbox).
3. Apply the job's AI call policy (`local_only`, `job_id`) around the handler: local-only when the job was stored so
   or when its group's "Keep recipe card photos and text on this server" is on now, so switching that on also covers
   cards still queued and later re-reads, re-extracts and retries of older ones (§10: a change never loosens a job).
4. Call the handler (`tasks.handle_extract` or `tasks.handle_reread`): it opens its own sessions, returns a result and
   writes nothing to the job row.
5. Apply the outcome with a write fenced on the lease (`finalize`), in a short session of its own; then, after a first
   extraction, `events.maybe_notify_batch` (§8).

**Cancellation** comes from the dispatcher through the task's loop (`loop.call_soon_threadsafe(task.cancel)`), with a
reason the worker reads back: the reviewer cancelled it (`cancelled`), the deadline passed (`timeout`), its lease was
cleared by a commit, discard or sweep (nothing is written), or the process is stopping (nothing is written; the
dispatcher releases the lease).

**A backup restore** (§3.9): when the handler raised `IngestPaused`, or anything while the pause marker is set, or
finished while it's set, the task writes nothing until the marker clears (polling every `PAUSED_TASK_POLL`, within its
deadline). Then it releases its lease (`queued`, `attempts - 1`, not claimed for `PAUSED_RELEASE_DELAY`) or applies its
result, fenced on its token: a restored row carries another token, so the result is then dropped.
"""

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.db.models.household.household import Household
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.lang.providers import get_locale_config, get_locale_provider, set_locale_context
from mealie.repos.repository_recipe_ingest import IngestQueue, IngestRepos, utcnow
from mealie.schema.recipe_ingest import IngestErrorCode, IngestTaskKind
from mealie.services.ai.policy import AICallPolicy, ai_call_policy

from .. import limits, storage
from . import finalize
from .classify import Disposition, classify, safe_trace
from .finalize import Applied, Finalized
from .types import ExtractResult, RereadResult, TaskContext

logger = get_logger(__name__)

Job = RecipeIngestionJob

DEFAULT_LOCALE = "en-US"


class CancelReason(StrEnum):
    """Why the dispatcher cancelled a task"""

    cancelled = "cancelled"
    """The reviewer asked (`cancel_requested`): stored as `cancelled`"""
    timeout = "timeout"
    """The task passed its deadline: stored as `timeout`"""
    vanished = "vanished"
    """The lease token is gone (commit, discard, sweep, a restore): nothing to write to"""
    shutdown = "shutdown"
    """The process is stopping: nothing is written, and the dispatcher releases the lease"""


CancelReasonGetter = Callable[[], CancelReason | None]


def _no_reason() -> CancelReason | None:
    return None


@dataclass(frozen=True)
class _ClaimedJob:
    id: UUID
    group_id: UUID
    household_id: UUID
    batch_id: UUID
    kind: IngestTaskKind
    payload: dict[str, Any] | None
    locale: str
    local_only: bool
    """The job's own `local_only`, or its group's setting as it is now"""
    owner_exists: bool


@dataclass(frozen=True)
class _Failure:
    """An outcome the worker decided itself (a cancellation, a missing household)"""

    code: IngestErrorCode


def _household_exists(session: Session, household_id: UUID) -> bool:
    """Whether the job's household still exists (SQLite enforces no foreign keys, so a job can outlive it)"""
    stmt = sa.select(Household.id).where(Household.id == household_id)
    return session.execute(stmt).scalar_one_or_none() is not None


def _load_job(job_id: UUID, token: UUID) -> _ClaimedJob | None:
    """The claimed job, or None when the lease is no longer this task's"""
    with session_context() as session:
        row = session.execute(
            sa.select(
                Job.id,
                Job.group_id,
                Job.household_id,
                Job.batch_id,
                Job.task_kind,
                Job.task_payload,
                Job.locale,
                Job.local_only,
            ).where(Job.id == job_id, *IngestQueue.fence(token))
        ).one_or_none()
        if row is None:
            return None
        owner_exists = _household_exists(session, row.household_id)
        # the group's setting as it is now: switching it on also covers cards queued before (§10)
        local_only = bool(row.local_only) or IngestRepos(session, row.group_id, None).settings.get().local_only
        session.commit()

    return _ClaimedJob(
        id=row.id,
        group_id=row.group_id,
        household_id=row.household_id,
        batch_id=row.batch_id,
        kind=IngestTaskKind(row.task_kind),
        payload=row.task_payload if isinstance(row.task_payload, dict) else None,
        locale=row.locale or DEFAULT_LOCALE,
        local_only=local_only,
        owner_exists=owner_exists,
    )


class _Progress:
    """
    The handler's `report_progress`: stores the latest progress key, fenced on the lease, at most once every
    `PROGRESS_INTERVAL`. A key reported sooner is written when the interval is up, unless a newer one replaces it.
    """

    def __init__(self, job_id: UUID, token: UUID) -> None:
        self.job_id = job_id
        self.token = token
        self._last_write = -math.inf
        self._pending: str | None = None
        self._flush: asyncio.Task[None] | None = None

    async def __call__(self, key: str) -> None:
        wait = self._last_write + limits.PROGRESS_INTERVAL - time.monotonic()
        if wait <= 0:
            self._pending = None
            self._store(key)
            return

        self._pending = key
        if self._flush is None or self._flush.done():
            self._flush = asyncio.get_running_loop().create_task(self._flush_later(wait))

    async def _flush_later(self, wait: float) -> None:
        await asyncio.sleep(wait)
        key, self._pending = self._pending, None
        if key is not None:
            self._store(key)

    def _store(self, key: str) -> None:
        self._last_write = time.monotonic()
        if storage.is_paused():
            return  # a restore is replacing the row (§3.9): the write would fail, or be replaced with it
        try:
            with session_context() as session:
                IngestQueue(session).set_progress(self.job_id, self.token, key)
        except Exception:
            # progress is cosmetic: a failed write never fails the task
            logger.warning(f"Recipe card job {self.job_id}: couldn't store its progress", exc_info=True)

    def close(self) -> None:
        if self._flush is not None:
            self._flush.cancel()


def _uncancel() -> None:
    task = asyncio.current_task()
    if task is not None:
        while task.cancelling():
            task.uncancel()


async def _wait_for_pause_end(job_id: UUID, deadline: float, cancel_reason: CancelReasonGetter) -> bool:
    """
    Waits for a backup restore's pause to end, within the task's deadline. False when the task has to give up
    instead (the deadline passed, or it was cancelled): it then writes nothing, and its lease expires.
    """
    logged = False
    try:
        while storage.is_paused():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or cancel_reason() is not None:
                logger.warning(f"Recipe card job {job_id}: gave up waiting for the backup restore to finish")
                return False
            if not logged:
                logger.info(f"Recipe card job {job_id}: waiting for the backup restore to finish")
                logged = True
            await asyncio.sleep(min(limits.PAUSED_TASK_POLL, remaining))
    except asyncio.CancelledError:
        _uncancel()
        return False
    return True


def _write(job_id: UUID, apply: Callable[[Session], Finalized]) -> Finalized:
    try:
        with session_context() as session:
            return apply(session)
    except Exception as e:
        # the lease then expires and the sweep queues the task again
        logger.error(f"Recipe card job {job_id}: couldn't store its task's outcome:\n{safe_trace(e)}")
        return finalize.DROPPED


async def _apply(
    job: _ClaimedJob,
    token: UUID,
    outcome: ExtractResult | RereadResult | _Failure | Exception,
    deadline: float,
    cancel_reason: CancelReasonGetter,
) -> Finalized:
    paused = storage.is_paused()

    if isinstance(outcome, Exception):
        job_dir = storage.job_dir(job.group_id, job.id)
        classified = classify(outcome, job_id=job.id, paused=paused, job_dir=job_dir)
        if classified.disposition == Disposition.paused:
            if not await _wait_for_pause_end(job.id, deadline, cancel_reason):
                return finalize.DROPPED
            not_before = utcnow() + timedelta(seconds=limits.PAUSED_RELEASE_DELAY)
            return _write(job.id, lambda s: finalize.release_lease(s, job.id, token, not_before=not_before))
        if classified.disposition == Disposition.rate_limited:
            return _write(job.id, lambda s: finalize.requeue_rate_limited(s, job.id, token, utcnow()))
        code = classified.code or IngestErrorCode.internal_error
        return _write(job.id, lambda s: finalize.finalize_failure(s, job.id, token, code, classified.params))

    if paused:
        # a result (or the worker's own failure) is held back until the restore is over (§3.9)
        if not await _wait_for_pause_end(job.id, deadline, cancel_reason):
            return finalize.DROPPED
        if isinstance(outcome, _Failure):
            not_before = utcnow() + timedelta(seconds=limits.PAUSED_RELEASE_DELAY)
            return _write(job.id, lambda s: finalize.release_lease(s, job.id, token, not_before=not_before))

    if isinstance(outcome, _Failure):
        failure = outcome
        return _write(job.id, lambda s: finalize.finalize_failure(s, job.id, token, failure.code))
    if isinstance(outcome, ExtractResult):
        extracted = outcome
        return _write(job.id, lambda s: finalize.finalize_extract(s, job.id, token, extracted))
    reread = outcome
    return _write(job.id, lambda s: finalize.finalize_reread(s, job.id, token, reread))


async def _call_handler(ctx: TaskContext) -> ExtractResult | RereadResult:
    # imported here, like every stage-B module the runner calls: some of them import the dispatcher (`wake`)
    from .. import tasks

    with ai_call_policy(AICallPolicy(local_only=ctx.local_only, job_id=ctx.job_id)):
        if ctx.kind == IngestTaskKind.reread:
            return await tasks.handle_reread(ctx)
        return await tasks.handle_extract(ctx)


def _notify(batch_id: UUID) -> None:
    from .. import events

    try:
        events.maybe_notify_batch(batch_id)
    except Exception as e:
        logger.error(f"Recipe card batch {batch_id}: its ready notification failed:\n{safe_trace(e)}")


async def run_task(
    job_id: UUID,
    token: UUID,
    *,
    deadline: float | None = None,
    cancel_reason: CancelReasonGetter = _no_reason,
) -> Applied:
    """
    Runs the task claimed with `token` on job `job_id` to its end, and applies the outcome (fenced). `deadline` is the
    `time.monotonic()` by which it must finish (default `TASK_DEADLINE` from now); `cancel_reason` tells why the
    dispatcher cancelled it. Returns what was applied.
    """
    deadline = deadline if deadline is not None else time.monotonic() + limits.TASK_DEADLINE

    try:
        job = _load_job(job_id, token)
    except Exception as e:
        logger.error(f"Recipe card job {job_id}: couldn't read its claimed task:\n{safe_trace(e)}")
        return Applied.dropped
    if job is None:
        logger.info(f"Recipe card job {job_id}: its task was taken back before it started")
        return Applied.dropped

    outcome: ExtractResult | RereadResult | _Failure | Exception
    if not job.owner_exists:
        outcome = _Failure(IngestErrorCode.owner_missing)
    else:
        set_locale_context(get_locale_provider(job.locale), get_locale_config(job.locale))
        progress = _Progress(job.id, token)
        ctx = TaskContext(
            job_id=job.id,
            group_id=job.group_id,
            household_id=job.household_id,
            kind=job.kind,
            payload=job.payload,
            token=token,
            locale=job.locale,
            local_only=job.local_only,
            report_progress=progress,
        )
        try:
            outcome = await _call_handler(ctx)
        except asyncio.CancelledError:
            _uncancel()
            reason = cancel_reason() or CancelReason.cancelled
            if reason in (CancelReason.vanished, CancelReason.shutdown):
                logger.info(f"Recipe card job {job.id}: its task was stopped ({reason.value})")
                return Applied.dropped
            outcome = _Failure(IngestErrorCode(reason.value))
        except Exception as e:
            outcome = e
        finally:
            progress.close()

    finalized = await _apply(job, token, outcome, deadline, cancel_reason)
    if finalized.applied == Applied.dropped:
        logger.info(f"Recipe card job {job.id}: its task's outcome was dropped (the lease is no longer its own)")
    else:
        logger.debug(f"Recipe card job {job.id}: task finished ({finalized.applied.value})")

    if finalized.left_processing and finalized.batch_id is not None:
        _notify(finalized.batch_id)
    return finalized.applied
