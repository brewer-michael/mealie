"""
Applying a task's outcome to its job (docs/ai/PHASE2.md §3.3, §3.6): the only writes a running task makes to the job
row, each fenced on its lease (`WHERE id=:job AND lease_token=:token AND task_state='running'`). A task reclaimed after
a pause, cancelled by a commit or discard, or swept therefore never applies a stale result: the fence fails and the
result is dropped. Every write also clears the task columns, except a requeue or release, which puts the task back in
the queue.

The JSON columns are written through `update_job_json`, which is optimistic on `row_version`: a review save landing
between the read and the write makes it read again, so neither is lost.

- A **first extraction** (the job is `processing`) writes the draft, flags, title, counts, transcription, extraction
  and pages, and makes the job `ready`.
- A **re-extract** replaces a draft nobody edited (`draft_version = extracted_version`; both become `draft_version + 1`,
  so an open editor's next save gets 409). On an edited draft it adds the new draft as a whole-card proposal instead,
  stores the new transcription and extraction, and recomputes the kept draft's flags against them.
- A **re-read** adds its proposal.
- A **failure** stores its code: a `processing` job becomes `failed`, a `ready` one stays ready with the code as a
  banner (a re-read the reviewer cancelled leaves no banner).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.orm import Session

from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import TASK_CLEARED, IngestQueue, update_job_json
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardFlag,
    CardProposal,
    CardProposalKind,
    IngestErrorCode,
    IngestStatus,
    IngestTaskState,
)

from .. import limits
from ..flag_rules import count_unresolved
from ..pipeline.flags import compute_flags
from .classify import rate_limit_delay
from .types import ExtractResult, RereadResult

Job = RecipeIngestionJob


class Applied(StrEnum):
    """What a finalize did"""

    draft = "draft"
    """A first extraction's draft, or a re-extract that replaced an unedited draft"""
    proposal = "proposal"
    """A re-read's proposal, or a re-extract of an edited draft"""
    failed = "failed"
    """A first extraction failed: the job is `failed`"""
    error = "error"
    """A task on a `ready` job ended without a result: the job keeps its draft (with the code as a banner)"""
    requeued = "requeued"
    """Rate limited: back in the queue after a backoff"""
    released = "released"
    """Given back without using up an attempt (a pause, or shutdown)"""
    dropped = "dropped"
    """Nothing written: the lease is no longer this task's"""


@dataclass(frozen=True)
class Finalized:
    applied: Applied
    batch_id: UUID | None = None
    left_processing: bool = False
    """The job's first extraction finished (`ready` or `failed`), so its batch may now be complete (§8)"""


DROPPED = Finalized(Applied.dropped)


def _rowcount(result: sa.Result) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


def _end_transaction(session: Session) -> None:
    if session.in_transaction():
        session.commit()


def _title(draft: CardDraft) -> str | None:
    return draft.name.strip()[:255] or None


def _counts(flags: list[CardFlag]) -> dict[str, int]:
    errors, warnings = count_unresolved(flags)
    return {"error_count": errors, "warning_count": warnings}


_NO_ERROR: dict[str, Any] = {"error_code": None, "error_params": None}


def finalize_extract(session: Session, job_id: UUID, token: UUID, result: ExtractResult) -> Finalized:
    """A finished first extraction, retry or re-extract (§3.3)"""
    applied: list[Applied] = []

    def mutate(row: RowMapping) -> dict[str, Any]:
        applied.clear()
        status = row["status"]
        if status not in (IngestStatus.processing, IngestStatus.ready):
            # no draft can land on a failed, committing or committed card: end the task, keeping the job as it is
            applied.append(Applied.dropped)
            return dict(TASK_CLEARED)

        common = {
            **TASK_CLEARED,
            **_NO_ERROR,
            "transcription": result.transcription,
            "extraction": result.extraction,
            "pages": result.pages,
        }

        replace = status == IngestStatus.processing or (
            row["draft"] is None or row["draft_version"] == row["extracted_version"]
        )
        if replace:
            version = row["draft_version"] + 1
            applied.append(Applied.draft)
            return {
                **common,
                **_counts(result.flags),
                "status": IngestStatus.ready.value,
                "draft": result.draft,
                "flags": result.flags,
                "title": _title(result.draft),
                "draft_version": version,
                "extracted_version": version,
            }

        # The reviewer has edited the draft: the new reading becomes a proposal. The transcription and extraction
        # are the card's latest reading (after a manual rotate, the first was of a sideways card), so the kept
        # draft's flags are recomputed against them as a save would: reading flags only where they were raised
        # before, so the reviewer's own edits don't raise them
        draft = CardDraft.model_validate(row["draft"])
        stored_flags = [CardFlag.model_validate(flag) for flag in row["flags"] or []]
        resolutions = {flag.id: flag.resolution for flag in stored_flags if flag.resolution is not None}
        flags = compute_flags(
            draft, result.extraction, resolutions, transcription=result.transcription, previous=stored_flags
        )
        proposal = CardProposal(kind=CardProposalKind.full, draft=result.draft)
        applied.append(Applied.proposal)
        return {
            **common,
            **_counts(flags),
            "flags": flags,
            "proposals": [*(row["proposals"] or []), proposal],
        }

    write = update_job_json(session, job_id, mutate, where=IngestQueue.fence(token))
    if write is None or applied[0] == Applied.dropped:
        return DROPPED
    first = write.before["status"] == IngestStatus.processing
    return Finalized(applied[0], batch_id=write.before["batch_id"], left_processing=first)


def finalize_reread(session: Session, job_id: UUID, token: UUID, result: RereadResult) -> Finalized:
    """A finished region re-read: its proposal joins the job's proposals"""

    def mutate(row: RowMapping) -> dict[str, Any]:
        return {**TASK_CLEARED, **_NO_ERROR, "proposals": [*(row["proposals"] or []), result.proposal]}

    write = update_job_json(session, job_id, mutate, where=IngestQueue.fence(token))
    if write is None:
        return DROPPED
    return Finalized(Applied.proposal, batch_id=write.before["batch_id"])


def finalize_failure(
    session: Session, job_id: UUID, token: UUID, code: IngestErrorCode, params: dict[str, Any] | None = None
) -> Finalized:
    """
    A task that ended without a result (§3.6): a `processing` job becomes `failed` with the code; a `ready` job keeps
    its draft and shows the code as a banner, except for a cancellation the reviewer asked for, which leaves no
    banner (as cancelling a queued task doesn't).
    """
    outcome: list[Applied] = []

    def mutate(row: RowMapping) -> dict[str, Any]:
        outcome.clear()
        error = {"error_code": code.value, "error_params": params or None}
        if row["status"] == IngestStatus.processing:
            outcome.append(Applied.failed)
            return {**TASK_CLEARED, **error, "status": IngestStatus.failed.value}
        outcome.append(Applied.error)
        if code == IngestErrorCode.cancelled:
            return dict(TASK_CLEARED)
        return {**TASK_CLEARED, **error}

    write = update_job_json(session, job_id, mutate, where=IngestQueue.fence(token))
    if write is None:
        return DROPPED
    return Finalized(outcome[0], batch_id=write.before["batch_id"], left_processing=outcome[0] == Applied.failed)


def requeue_rate_limited(session: Session, job_id: UUID, token: UUID, now: datetime) -> Finalized:
    """
    Every provider answered 429: the task goes back in the queue, not before `now + min(60·2ⁿ, 900)` seconds for its
    nth rate-limit retry, without using up an attempt (attempts count lost leases). After `MAX_RATE_LIMIT_RETRIES` it
    fails `rate_limited` instead.
    """
    fence = [Job.id == job_id, *IngestQueue.fence(token)]
    row = session.execute(sa.select(Job.rate_limit_retries).where(*fence)).one_or_none()
    _end_transaction(session)
    if row is None:
        return DROPPED

    retries = row.rate_limit_retries
    if retries >= limits.MAX_RATE_LIMIT_RETRIES:
        return finalize_failure(session, job_id, token, IngestErrorCode.rate_limited)

    stmt = (
        sa.update(Job)
        .where(*fence, Job.rate_limit_retries == retries)
        .values(
            task_state=IngestTaskState.queued.value,
            lease_token=None,
            lease_owner=None,
            lease_expires_at=None,
            progress_key=None,
            not_before=now + timedelta(seconds=rate_limit_delay(retries)),
            rate_limit_retries=retries + 1,
            attempts=sa.case((Job.attempts > 0, Job.attempts - 1), else_=0),
        )
    )
    try:
        requeued = _rowcount(session.execute(stmt, execution_options={"synchronize_session": False})) == 1
    finally:
        _end_transaction(session)
    # only this task changes its own counter, so a miss means the lease was taken from it
    return Finalized(Applied.requeued) if requeued else DROPPED


def release_lease(session: Session, job_id: UUID, token: UUID, *, not_before: datetime | None = None) -> Finalized:
    """Gives the task back without using up an attempt (§3.8, §3.9): queued, lease cleared, `attempts - 1`"""
    released = IngestQueue(session).release(job_id, token, not_before=not_before)
    return Finalized(Applied.released) if released else DROPPED
