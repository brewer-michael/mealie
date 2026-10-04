"""
Finalizing (docs/ai/PHASE2.md §3.3, §3.6, §3.7): what each outcome writes, the fence that drops stale results, the
replace-or-propose choice of a re-extract, the error table, and the worker around a handler (locale, policy, progress,
the batch notification).
"""

import asyncio
import logging
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import openai
import pytest
from ingest_runner_testing import (
    FakeHandlers,
    Jobs,
    PhaseCalls,
    blocking,
    extract_result,
    reread_result,
    run,
    settle,
    wait_for,
)
from sqlalchemy.orm import Session

from mealie.core import exceptions
from mealie.lang.providers import get_locale_context
from mealie.repos.repository_recipe_ingest import TASK_CLEARED, IngestQueue, utcnow
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardFlag,
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    CardProposalKind,
    ExtractionMeta,
    FlagResolution,
    IngestErrorCode,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
)
from mealie.services.ai.errors import (
    AIProviderLimitReachedError,
    AIProviderLocalOnlyError,
    AIProviderRefusedError,
    IngestPaused,
)
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.runner import finalize, worker
from mealie.services.ai.ingest.runner.classify import Disposition, classify
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.finalize import Applied
from mealie.services.ai.ingest.runner.types import TaskContext, TaskFailed
from mealie.services.ai.policy import current_policy
from mealie.services.openai.openai import OpenAINotEnabledException
from mealie.services.recipe.import_workflow.exceptions import NoRecipeDataError


def _flag(kind: CardFlagKind, severity: CardFlagSeverity, *, resolution: FlagResolution | None = None) -> CardFlag:
    return CardFlag(
        id=f"{kind.value}:name:",
        kind=kind,
        severity=severity,
        source=CardFlagSource.validator,
        field="name",
        resolution=resolution,
    )


def _claim(session: Session, job_id: UUID) -> UUID:
    token = uuid4()
    assert IngestQueue(session).claim(job_id, token=token, owner="test", now=utcnow())
    return token


# ==========================================
# What each outcome writes


def test_a_first_extraction_writes_the_draft_and_makes_the_job_ready(db: Session, jobs: Jobs):
    job_id = jobs.create(error_code=IngestErrorCode.internal_error.value)  # a retry of a failed card
    token = _claim(db, job_id)
    flags = [
        _flag(CardFlagKind.blank, CardFlagSeverity.error),
        _flag(CardFlagKind.unsure, CardFlagSeverity.warning),
        _flag(CardFlagKind.not_on_card, CardFlagSeverity.warning, resolution=FlagResolution.dismissed),
    ]
    result = extract_result("  Banana Mug Cake ", flags=flags)

    finalized = finalize.finalize_extract(db, job_id, token, result)

    assert finalized.applied == Applied.draft
    assert finalized.left_processing and finalized.batch_id == jobs.batch_id
    row = jobs.row(job_id)
    assert row["status"] == IngestStatus.ready
    assert CardDraft.model_validate(row["draft"]) == result.draft
    assert [CardFlag.model_validate(flag) for flag in row["flags"]] == flags
    assert (row["title"], row["error_count"], row["warning_count"]) == ("Banana Mug Cake", 1, 1)
    assert (row["draft_version"], row["extracted_version"]) == (1, 1)
    assert row["transcription"] == result.transcription
    assert ExtractionMeta.model_validate(row["extraction"]) == result.extraction
    assert row["pages"][0]["oriented"] is True
    assert (row["error_code"], row["error_params"]) == (None, None)
    assert all(row[column] == value for column, value in TASK_CLEARED.items())
    assert row["row_version"] == 1


def test_a_reextract_replaces_a_draft_nobody_edited(db: Session, jobs: Jobs):
    job_id = jobs.ready(kind=IngestTaskKind.extract, state=IngestTaskState.queued)
    token = _claim(db, job_id)

    finalized = finalize.finalize_extract(db, job_id, token, extract_result("Banana Bread"))

    assert finalized.applied == Applied.draft
    assert not finalized.left_processing
    row = jobs.row(job_id)
    assert (row["status"], row["title"], row["draft"]["name"]) == (IngestStatus.ready, "Banana Bread", "Banana Bread")
    assert (row["draft_version"], row["extracted_version"]) == (2, 2)  # an open editor's next save gets 409
    assert row["proposals"] is None


def test_a_reextract_of_an_edited_draft_becomes_a_proposal(db: Session, jobs: Jobs, monkeypatch: pytest.MonkeyPatch):
    kept = _flag(CardFlagKind.blank, CardFlagSeverity.error, resolution=FlagResolution.kept)
    job_id = jobs.ready(
        kind=IngestTaskKind.extract, state=IngestTaskState.queued, draft_version=3, flags=[kept], error_count=0
    )
    before = jobs.row(job_id)
    recomputed: list[dict[str, Any]] = []

    def compute_flags(
        draft: CardDraft,
        extraction: ExtractionMeta | None,
        resolutions: Mapping[str, FlagResolution],
        *,
        transcription: str | None = None,
        previous: Sequence[CardFlag] | None = None,
    ) -> list[CardFlag]:
        recomputed.append({"draft": draft, "resolutions": dict(resolutions), "transcription": transcription})
        unsure = _flag(CardFlagKind.unsure, CardFlagSeverity.warning)
        return [kept.model_copy(update={"resolution": resolutions.get(kept.id)}), unsure]

    monkeypatch.setattr(finalize, "compute_flags", compute_flags)
    token = _claim(db, job_id)
    result = extract_result("Banana Bread")

    finalized = finalize.finalize_extract(db, job_id, token, result)

    assert finalized.applied == Applied.proposal
    row = jobs.row(job_id)
    assert row["draft"] == before["draft"]  # the reviewer's draft is kept
    assert (row["draft_version"], row["extracted_version"]) == (3, 1)
    [proposal] = row["proposals"]
    assert proposal["kind"] == CardProposalKind.full
    assert proposal["draft"]["name"] == "Banana Bread"
    assert row["transcription"] == result.transcription
    assert recomputed == [
        {
            "draft": CardDraft.model_validate(before["draft"]),
            "resolutions": {kept.id: FlagResolution.kept},
            "transcription": result.transcription,
        }
    ]
    assert (row["error_count"], row["warning_count"]) == (0, 1)
    assert row["task_state"] is None


def test_a_save_landing_during_a_reextract_makes_it_a_proposal(
    db: Session, jobs: Jobs, monkeypatch: pytest.MonkeyPatch
):
    """The replace is optimistic on `row_version`: a save between the read and the write is never overwritten"""
    job_id = jobs.ready(kind=IngestTaskKind.extract, state=IngestTaskState.queued)
    token = _claim(db, job_id)
    counts = finalize._counts
    saved = []

    def counts_with_a_save_landing(flags: list[CardFlag]) -> dict[str, int]:
        if not saved:
            row = jobs.row(job_id)
            edited = {**row["draft"], "name": "Grandma's Banana Mug Cake"}
            jobs.update(
                job_id, draft=edited, draft_version=row["draft_version"] + 1, row_version=row["row_version"] + 1
            )
            saved.append(edited)
        return counts(flags)

    monkeypatch.setattr(finalize, "_counts", counts_with_a_save_landing)
    finalized = finalize.finalize_extract(db, job_id, token, extract_result("Banana Bread"))

    assert finalized.applied == Applied.proposal
    row = jobs.row(job_id)
    assert row["draft"]["name"] == "Grandma's Banana Mug Cake"
    assert row["draft_version"] == 2
    assert [proposal["draft"]["name"] for proposal in row["proposals"]] == ["Banana Bread"]


def test_a_reread_adds_its_proposal(db: Session, jobs: Jobs):
    earlier = reread_result("1/2 tsp vanilla").proposal
    job_id = jobs.ready(
        kind=IngestTaskKind.reread,
        state=IngestTaskState.queued,
        proposals=[earlier],
        error_code=IngestErrorCode.provider_failed.value,
    )
    token = _claim(db, job_id)

    finalized = finalize.finalize_reread(db, job_id, token, reread_result("1/4 tsp salt"))

    assert finalized.applied == Applied.proposal and not finalized.left_processing
    row = jobs.row(job_id)
    assert [proposal["text"] for proposal in row["proposals"]] == ["1/2 tsp vanilla", "1/4 tsp salt"]
    assert row["error_code"] is None  # the last task worked, so the old banner goes
    assert row["draft_version"] == 1
    assert row["task_state"] is None


def test_a_failed_first_extraction_fails_the_job(db: Session, jobs: Jobs):
    job_id = jobs.create()
    token = _claim(db, job_id)

    detail = {"detail": "AuthenticationError (HTTP 401)"}
    finalized = finalize.finalize_failure(db, job_id, token, IngestErrorCode.provider_failed, detail)

    assert finalized.applied == Applied.failed and finalized.left_processing
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["error_params"]) == (
        IngestStatus.failed,
        IngestErrorCode.provider_failed,
        detail,
    )
    assert all(row[column] == value for column, value in TASK_CLEARED.items())


def test_a_failed_reread_leaves_the_job_ready_with_a_banner(db: Session, jobs: Jobs):
    job_id = jobs.ready(kind=IngestTaskKind.reread, state=IngestTaskState.queued)
    token = _claim(db, job_id)

    finalized = finalize.finalize_failure(db, job_id, token, IngestErrorCode.limit_reached)

    assert finalized.applied == Applied.error and not finalized.left_processing
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"]) == (
        IngestStatus.ready,
        IngestErrorCode.limit_reached,
        None,
    )


def test_an_extraction_for_a_card_that_cant_take_a_draft_just_ends(db: Session, jobs: Jobs):
    """A running extraction on a failed card (nothing enqueues one, but if it happens) ends without looping"""
    job_id = jobs.create(status=IngestStatus.failed, error_code=IngestErrorCode.no_recipe_found.value)
    token = _claim(db, job_id)

    assert finalize.finalize_extract(db, job_id, token, extract_result()) == finalize.DROPPED
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["draft"], row["task_state"]) == (
        IngestStatus.failed,
        IngestErrorCode.no_recipe_found,
        None,
        None,
    )


# ==========================================
# The fence


def test_a_stale_token_writes_nothing(db: Session, jobs: Jobs):
    job_id = jobs.create()
    _claim(db, job_id)
    before = jobs.row(job_id)
    stale = uuid4()

    assert finalize.finalize_extract(db, job_id, stale, extract_result()) == finalize.DROPPED
    assert finalize.finalize_failure(db, job_id, stale, IngestErrorCode.timeout) == finalize.DROPPED
    assert finalize.requeue_rate_limited(db, job_id, stale, utcnow()) == finalize.DROPPED
    assert finalize.release_lease(db, job_id, stale) == finalize.DROPPED
    assert jobs.row(job_id) == before


def test_a_result_after_the_lease_was_swept_and_reclaimed_is_dropped(db: Session, jobs: Jobs):
    job_id = jobs.create()
    old = _claim(db, job_id)
    assert IngestQueue(db).release(job_id, old)
    new = _claim(db, job_id)

    assert finalize.finalize_extract(db, job_id, old, extract_result()).applied == Applied.dropped
    row = jobs.row(job_id)
    assert (row["status"], row["lease_token"], row["draft"]) == (IngestStatus.processing, new, None)


def test_a_result_after_a_commit_cleared_the_task_is_dropped(db: Session, jobs: Jobs):
    job_id = jobs.ready(kind=IngestTaskKind.reread, state=IngestTaskState.queued)
    token = _claim(db, job_id)
    jobs.update(job_id, status=IngestStatus.committing.value, **TASK_CLEARED)

    assert finalize.finalize_reread(db, job_id, token, reread_result()).applied == Applied.dropped
    assert jobs.row(job_id)["proposals"] is None


# ==========================================
# The error table (§3.6)


def _openai_error(kind: type[openai.APIStatusError], status: int, body: str) -> openai.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "https://provider.example/v1"), text=body)
    return kind(body, response=response, body=body)


def _wrapped(error: Exception) -> Exception:
    """As upstream's `OpenAIService.get_response` wraps a provider's error"""
    try:
        raise Exception(f"OpenAI Request Failed. {error.__class__.__name__}: {error}") from error
    except Exception as e:
        return e


@pytest.mark.parametrize(
    ("error", "disposition", "code"),
    [
        (TaskFailed(IngestErrorCode.no_recipe_found), Disposition.fail, IngestErrorCode.no_recipe_found),
        (exceptions.RateLimitError("429"), Disposition.rate_limited, None),
        (_wrapped(_openai_error(openai.RateLimitError, 429, "slow down")), Disposition.rate_limited, None),
        (FileNotFoundError("view.jpg"), Disposition.fail, IngestErrorCode.files_missing),
        (AIProviderLocalOnlyError(), Disposition.fail, IngestErrorCode.local_only_unavailable),
        (AIProviderLimitReachedError("over"), Disposition.fail, IngestErrorCode.limit_reached),
        (OpenAINotEnabledException(), Disposition.fail, IngestErrorCode.ai_not_enabled),
        (NoRecipeDataError(), Disposition.fail, IngestErrorCode.no_recipe_found),
        (_wrapped(AIProviderRefusedError("The model declined")), Disposition.fail, IngestErrorCode.provider_failed),
        (IngestPaused(), Disposition.paused, None),
        (ValueError("bug"), Disposition.fail, IngestErrorCode.internal_error),
    ],
)
def test_errors_map_to_their_outcome(error: Exception, disposition: Disposition, code: IngestErrorCode | None):
    classified = classify(error, job_id=uuid4(), paused=False)
    assert (classified.disposition, classified.code) == (disposition, code)


def test_any_error_while_paused_waits_for_the_restore():
    for error in (ValueError("table dropped"), FileNotFoundError("page.jpg"), TaskFailed(IngestErrorCode.timeout)):
        assert classify(error, job_id=uuid4(), paused=True).disposition == Disposition.paused


def test_only_the_jobs_own_missing_files_are_files_missing(tmp_path: Path):
    job_dir = tmp_path / "ai-ingest" / "job"
    page = FileNotFoundError(2, "No such file or directory", str(job_dir / "pages" / "0" / "view.jpg"))
    prompt = FileNotFoundError(2, "No such file or directory", str(tmp_path / "prompts" / "card-compile-rules.txt"))

    assert classify(page, job_id=uuid4(), paused=False, job_dir=job_dir).code == IngestErrorCode.files_missing
    assert classify(prompt, job_id=uuid4(), paused=False, job_dir=job_dir).code == IngestErrorCode.internal_error


def test_a_provider_error_is_stored_without_the_providers_response(caplog: pytest.LogCaptureFixture):
    job_id = uuid4()
    error = _wrapped(_openai_error(openai.AuthenticationError, 401, "secret internal host reply"))

    classified = classify(error, job_id=job_id, paused=False)

    assert classified.params == {"detail": "AuthenticationError (HTTP 401)"}
    assert "secret" not in caplog.text
    assert str(job_id) in caplog.text


def test_an_internal_error_is_logged_with_the_job_id_but_not_its_message(caplog: pytest.LogCaptureFixture):
    job_id = uuid4()
    with caplog.at_level(logging.ERROR):
        classified = classify(KeyError("1 T. coconut oil"), job_id=job_id, paused=False)

    assert classified.code == IngestErrorCode.internal_error and classified.params == {}
    [record] = [record for record in caplog.records if str(job_id) in record.getMessage()]
    assert record.levelno == logging.ERROR
    assert "KeyError" in record.getMessage()
    assert "coconut" not in record.getMessage()


# ==========================================
# The worker around a handler


def test_the_worker_sets_the_locale_and_policy_and_notifies_after_a_first_extraction(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, phases: PhaseCalls
):
    seen: dict[str, Any] = {}

    async def handler(ctx: TaskContext) -> Any:
        context = get_locale_context()
        assert context is not None
        seen["locale"] = (ctx.locale, context[1].key)
        seen["policy"] = current_policy()
        seen["kind"] = ctx.kind
        return extract_result()

    job_id = jobs.create(locale="fr-FR", local_only=True)
    handlers.default = handler

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert seen["locale"] == ("fr-FR", "fr-FR")
    assert (seen["policy"].local_only, seen["policy"].job_id) == (True, job_id)
    assert seen["kind"] == IngestTaskKind.extract
    assert current_policy().job_id is None  # nothing leaks out of the task
    assert phases.notified == [jobs.batch_id]
    assert jobs.row(job_id)["status"] == IngestStatus.ready


def test_rereads_and_reextracts_dont_notify(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, phases: PhaseCalls
):
    jobs.ready(kind=IngestTaskKind.reread, state=IngestTaskState.queued)
    jobs.ready(kind=IngestTaskKind.extract, state=IngestTaskState.queued)

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert len(handlers.calls) == 2
    assert phases.notified == []


def test_a_failed_first_extraction_notifies_too(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, phases: PhaseCalls
):
    """A failed card no longer holds its batch back: a failed-only batch notifies as well (§8)"""
    job_id = jobs.create()

    async def no_recipe(ctx: TaskContext) -> Any:
        raise TaskFailed(IngestErrorCode.no_recipe_found)

    handlers.default = no_recipe

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert jobs.row(job_id)["error_code"] == IngestErrorCode.no_recipe_found
    assert phases.notified == [jobs.batch_id]


def test_a_job_whose_household_is_gone_fails_owner_missing(
    db: Session, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    job_id = jobs.create()
    if db.get_bind().dialect.name == "sqlite":
        jobs.update(job_id, household_id=uuid4())  # SQLite enforces no foreign keys: the job outlives its household
    else:
        monkeypatch.setattr(worker, "_household_exists", lambda session, household_id: False)
    token = _claim(db, job_id)

    assert run(worker.run_task(job_id, token)) == Applied.failed
    assert handlers.calls == []
    assert jobs.row(job_id)["error_code"] == IngestErrorCode.owner_missing


def test_progress_is_stored_at_most_once_an_interval(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "PROGRESS_INTERVAL", 0.3)
    gate = threading.Event()
    stored: list[str | None] = []
    set_progress = IngestQueue.set_progress

    def recording(self: IngestQueue, job_id: UUID, token: UUID, progress_key: str | None) -> bool:
        stored.append(progress_key)
        return set_progress(self, job_id, token, progress_key)

    monkeypatch.setattr(IngestQueue, "set_progress", recording)

    async def handler(ctx: TaskContext) -> Any:
        await ctx.report_progress("recipe-ingest.progress.orienting")
        await ctx.report_progress("recipe-ingest.progress.reading-card")
        await ctx.report_progress("recipe-ingest.progress.structuring")
        return await blocking(gate)(ctx)

    job_id = jobs.create()
    handlers.default = handler

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: jobs.row(job_id)["progress_key"] == "recipe-ingest.progress.structuring")
        await asyncio.sleep(0.4)
        gate.set()
        await settle(dispatcher)

    run(scenario())
    assert stored == ["recipe-ingest.progress.orienting", "recipe-ingest.progress.structuring"]
    assert jobs.row(job_id)["progress_key"] is None  # cleared with the task
