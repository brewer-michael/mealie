"""
The sweep (docs/ai/PHASE2.md §3.2): recovering tasks whose process stopped heartbeating (a crash, a container stop
past its grace, a dev reload, a long process pause). An expired lease goes back to the queue while the task has
attempts left. After `MAX_ATTEMPTS` lost leases the poison guard ends the task by the job's status: a `processing`
job fails with `interrupted`; a `ready` job keeps its draft, with its task cleared and `interrupted` as a banner.

It also ends the queued tasks the reviewer asked to cancel while they ran (`cancel_requested` survives a requeue that
came before the heartbeat that would have stopped them), as cancelling a queued task does; the claim never takes them.

Every write is its own transaction, fenced on the expired token and on the lease still being expired, so a task that
was renewed, finished or reclaimed in the meantime is left alone. The dispatcher runs this every tick, in every
process; it's idempotent.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from mealie.core.root_logger import get_logger
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import TASK_CLEARED, ExpiredLease, IngestQueue
from mealie.schema.recipe_ingest import IngestErrorCode, IngestStatus, IngestTaskState

from .. import limits

logger = get_logger(__name__)

Job = RecipeIngestionJob


@dataclass
class SweepResult:
    requeued: list[UUID] = field(default_factory=list)
    """Jobs whose task went back to the queue"""
    failed: list[UUID] = field(default_factory=list)
    """`processing` jobs that failed with `interrupted`"""
    interrupted: list[UUID] = field(default_factory=list)
    """`ready` jobs whose task was given up, with an `interrupted` banner"""
    cancelled: list[UUID] = field(default_factory=list)
    """Jobs whose queued task was ended because the reviewer had asked to cancel it while it ran"""


def _poison(session: Session, lease: ExpiredLease, now: datetime) -> IngestStatus | None:
    """Ends a task that lost its lease too often; the job's status it was ended for, or None if the fence failed"""
    still_expired = [
        Job.id == lease.job_id,
        Job.lease_token == lease.token,
        Job.task_state == IngestTaskState.running.value,
        Job.lease_expires_at < now,
        Job.attempts >= limits.MAX_ATTEMPTS,
    ]
    error = {"error_code": IngestErrorCode.interrupted.value, "error_params": None}
    bump = {"row_version": Job.row_version + 1}
    statements = [
        (
            IngestStatus.failed,
            sa.update(Job)
            .where(*still_expired, Job.status == IngestStatus.processing.value)
            .values(**TASK_CLEARED, **error, **bump, status=IngestStatus.failed.value),
        ),
        (
            IngestStatus.ready,
            sa.update(Job)
            .where(*still_expired, Job.status != IngestStatus.processing.value)
            .values(**TASK_CLEARED, **error, **bump),
        ),
    ]
    for outcome, stmt in statements:
        try:
            result = session.execute(stmt, execution_options={"synchronize_session": False})
            ended = isinstance(result, CursorResult) and result.rowcount == 1
        finally:
            if session.in_transaction():
                session.commit()
        if ended:
            return outcome
    return None


def sweep_expired(session: Session, now: datetime, *, held: Collection[UUID] = ()) -> SweepResult:
    """
    Requeues or ends every running task whose lease expired before `now` (naive UTC, `utcnow()`), then ends the
    queued tasks whose cancel was asked while they ran. Tokens in `held` belong to tasks this process is still running
    and heartbeating; they're left alone.
    """
    queue = IngestQueue(session)
    result = SweepResult()
    for lease in queue.expired(now):
        if lease.token in held:
            continue

        if lease.attempts < limits.MAX_ATTEMPTS:
            if queue.requeue_expired(lease.job_id, lease.token, now):
                logger.info(f"Recipe card job {lease.job_id}: its task lost its lease and was queued again")
                result.requeued.append(lease.job_id)
            continue

        ended = _poison(session, lease, now)
        if ended is None:
            continue
        logger.warning(
            f"Recipe card job {lease.job_id}: its task lost its lease {lease.attempts} times and was given up"
        )
        if ended == IngestStatus.failed:
            result.failed.append(lease.job_id)
        else:
            result.interrupted.append(lease.job_id)

    result.cancelled = queue.cancel_requeued()
    for job_id in result.cancelled:
        logger.info(f"Recipe card job {job_id}: its task was cancelled as asked after it went back to the queue")
    return result
