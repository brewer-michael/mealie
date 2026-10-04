"""
The recipe card task handlers (docs/ai/PHASE2.md §3.7): the runner calls one per claimed task, in the task's thread
and event loop, with the locale context and the job's AI call policy already set. A handler opens its own sessions,
returns a result and writes nothing to the job row: the runner applies the result with a write fenced on the lease.
A failure it understands is raised as `TaskFailed`.

The one write is a page's turn: a page Tesseract decided to turn (`_orient_page`), or one the image reader said was
sideways (`_turn_as_read`, when Tesseract didn't settle it), has new files on disk, so its new metadata is written
straight away, fenced on the task's lease like every write by a running task (§3.3). A later failure (a rate limit,
say) would otherwise leave the row describing the page as it was before it turned. A turn is staged (`_turn_page`):
the turned files are written beside the current ones, the metadata naming them is stored, and only then are they
swapped in, all in one write section, in one thread, awaited to its end even when the task is cancelled meanwhile.
If the fence fails the staged files are removed, and the page is as stored. A crash between the steps leaves staged
files that the next task settles against the stored metadata before it reads the pages (`_recover_pages`): swapped
in when the metadata naming them was stored, removed when it wasn't. So a shutdown, a cancel, a lost lease, a later
page's failure or a killed process never leaves a page's files and its stored metadata disagreeing.

Each handler's AI session (`JobOpenAIService` on `get_repositories(session, group_id=…, household_id=…)`) is only used
for reading the job, routing reads, the usage log and read-only lookups, and no transaction stays open across an
await (F11). Its service records every provider answer in the task's `answers`, and replays an answer an earlier task
on the card got (one a backup restore cut off) for a request with the same inputs rather than send it again
(`_KeptAnswersService`).
"""

import asyncio
from collections.abc import Callable, Sequence
from functools import cached_property
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.db.models.household.household import Household
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.lang.providers import get_locale_provider
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_factory import AllRepositories
from mealie.repos.repository_recipe_ingest import IngestQueue, IngestRepos
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardProposalOrigin,
    ExtractionMeta,
    ExtractionUsage,
    IngestErrorCode,
    IngestTaskMode,
    PageMeta,
    PageOCR,
    PageRotationSource,
    ProposalTarget,
)
from mealie.schema.recipe_ingest.ingest_requests import MAX_PARSE_LINES, MAX_TRANSCRIPTION
from mealie.services.openai.openai import OpenAIAttachment
from mealie.services.openai.openai import T as Answer

from . import images, storage
from .images import Region
from .matching import IngestMatcher
from .pipeline import (
    CardPage,
    decide_orientation,
    extract_card,
    options_for_group,
    orientation_available,
    oriented_meta,
    parse_lines,
    rebuild_from_transcription,
    reread_region,
)
from .pipeline.context import PROGRESS_ORIENTING
from .pipeline.flags import DRAFT_TEXT_FIELDS, FIELD_INGREDIENTS, FIELD_NOTES, FIELD_STEPS, ingredient_line
from .pipeline.ingredients import IngredientLine, normalize_lines
from .pipeline.reread import field_name
from .pipeline.service import JobAIRuntime, JobOpenAIService, end_transaction
from .runner.answers import KeptAnswers
from .runner.types import ExtractResult, ParseLinesResult, RereadResult, TaskContext, TaskFailed

logger = get_logger(__name__)

TURNS = (90, 180, 270)
"""How far a page can be turned clockwise"""
TURN_LOCK_WAIT = 60.0
"""How long a task waits for a page's turn lock (a manual rotate holds it for moments)"""


class _ReplayingRuntime(JobAIRuntime):
    """`JobAIRuntime` that also tallies a replayed answer, as the task that got it did (`_KeptAnswersService`)"""

    def tallies(self, feature: str) -> list[ExtractionUsage]:
        """The usage tallied so far for one feature (a response schema's name)"""
        return [tally.model_copy() for (name, _, _), tally in self._usage.items() if name == feature]

    def replayed(self, feature: str, answered: tuple[str, str] | None, usage: list[ExtractionUsage]) -> None:
        for tally in usage:
            key = (feature, tally.slot, tally.provider or "")
            current = self._usage.get(key)
            if current is None:
                self._usage[key] = tally.model_copy()
                continue
            current.model = tally.model or current.model
            current.requests += tally.requests
            current.failures += tally.failures
            current.prompt_tokens += tally.prompt_tokens
            current.completion_tokens += tally.completion_tokens
            current.latency_ms += tally.latency_ms
        if answered is not None:
            self._answered[feature] = answered


def _usage_since(before: list[ExtractionUsage], after: list[ExtractionUsage]) -> list[ExtractionUsage]:
    """What one request added to a feature's tallies"""
    earlier = {(tally.slot, tally.provider): tally for tally in before}
    added: list[ExtractionUsage] = []
    for tally in after:
        old = earlier.get((tally.slot, tally.provider))
        if old is None:
            added.append(tally)
            continue
        delta = tally.model_copy(
            update={
                "requests": tally.requests - old.requests,
                "failures": tally.failures - old.failures,
                "prompt_tokens": tally.prompt_tokens - old.prompt_tokens,
                "completion_tokens": tally.completion_tokens - old.completion_tokens,
                "latency_ms": tally.latency_ms - old.latency_ms,
            }
        )
        if delta.requests:
            added.append(delta)
    return added


class _KeptAnswersService(JobOpenAIService):
    """
    The task's AI service: every answer a provider gives is recorded in the task's `answers` (with the usage it cost
    and who answered), and a request an earlier task on the card already got an answer for, with the same inputs, is
    answered from those instead of sent again. That earlier task was cut off by a backup restore (`runner/results.py`).
    """

    def __init__(self, repos: AllRepositories, answers: KeptAnswers) -> None:
        super().__init__(repos)
        self.answers = answers

    @cached_property
    def runtime(self) -> _ReplayingRuntime:
        return _ReplayingRuntime(self)

    async def get_response(
        self,
        prompt: str,
        message: str,
        *,
        response_schema: type[Answer],
        attachments: list[OpenAIAttachment] | None = None,
        provider: AIProviderOut | None = None,
        slot: AIProviderSlot | None = None,
    ) -> Answer | None:
        if provider is not None:
            # one provider's attempt, which the runtime makes for the routed request recorded around it
            return await super().get_response(
                prompt, message, response_schema=response_schema, attachments=attachments, provider=provider, slot=slot
            )
        key = KeptAnswers.request_key(prompt, message, response_schema, attachments, provider, slot)
        feature = response_schema.__name__
        entry = self.answers.get(key)
        if entry is not None:
            try:
                answer = response_schema.model_validate(entry["answer"])
                usage = [ExtractionUsage.model_validate(tally) for tally in entry.get("usage") or []]
                answered = (str(entry["answered"][0]), str(entry["answered"][1])) if entry.get("answered") else None
            except KeyError, IndexError, TypeError, ValueError, ValidationError:
                pass  # not what this request answers now: ask a provider
            else:
                self.runtime.replayed(feature, answered, usage)
                self.answers.replayed += 1
                return answer

        before = self.runtime.tallies(feature)
        answer = await super().get_response(
            prompt, message, response_schema=response_schema, attachments=attachments, provider=provider, slot=slot
        )
        if isinstance(answer, BaseModel):
            answered = self.runtime.answered_by(feature)
            self.answers.put(
                key,
                {
                    "answer": answer.model_dump(mode="json"),
                    "answered": list(answered) if answered else None,
                    "usage": [
                        tally.model_dump(mode="json") for tally in _usage_since(before, self.runtime.tallies(feature))
                    ],
                },
            )
        return answer


def _ai_service(repos: AllRepositories, ctx: TaskContext) -> _KeptAnswersService:
    """The handler's AI service, recording and replaying provider answers in the task's `answers`"""
    return _KeptAnswersService(repos, ctx.answers)


def _load_job(session: Session, ctx: TaskContext) -> RecipeIngestionJob:
    """The task's job, or `TaskFailed`: `owner_missing` when its household is gone, else `interrupted`"""
    job = IngestRepos(session, ctx.group_id, ctx.household_id).jobs.get(ctx.job_id)
    if job is None:
        household = session.execute(sa.select(Household.id).where(Household.id == ctx.household_id)).first()
        end_transaction(session)
        raise TaskFailed(IngestErrorCode.interrupted if household else IngestErrorCode.owner_missing)
    return job


def _recover_pages(pages: list[CardPage]) -> None:
    """
    Settles what a crash left between staging a page's turn and swapping it in (`images.recover_staged`), against
    the stored metadata the task just read: the staged files are swapped in when the metadata naming them was stored,
    removed when it wasn't. Each page under its turn lock (`review.page_turn_lock`), which a manual rotate holds from
    staging to swapping: a rotate that checked for a task just before this one was claimed finishes (refused, as the
    job has a task now) before its files are looked at. `IngestPaused` while a backup restore pauses ingestion.
    """
    from .review import page_turn_lock  # here: the review service imports the runner

    staged = [page for page in pages if images.has_staged(page.dir)]
    if not staged:
        return
    with storage.ingest_write():
        for page in staged:
            with page_turn_lock(page.dir, wait=TURN_LOCK_WAIT):
                outcome = images.recover_staged(page.dir, page.meta)
            if outcome != "none":
                logger.info(f"Recipe card page {page.dir}: the staged files of a turn cut short were {outcome}")


def _card_pages(ctx: TaskContext, job: RecipeIngestionJob) -> list[CardPage]:
    """
    The job's pages, a turn cut short by a crash settled first (`_recover_pages`); `FileNotFoundError` when a page's
    files are gone
    """
    pages = [
        CardPage(dir=storage.page_dir(ctx.group_id, ctx.job_id, meta.index), meta=meta)
        for meta in (PageMeta.model_validate(page) for page in job.pages or [])
    ]
    if not pages:
        raise FileNotFoundError(storage.job_dir(ctx.group_id, ctx.job_id))
    _recover_pages([page for page in pages if page.dir.is_dir()])
    for page in pages:
        for path in (page.page_path, page.view_path):
            if not path.is_file():
                raise FileNotFoundError(path)
    return pages


def _holds_lease(ctx: TaskContext) -> bool:
    with session_context() as session:
        return IngestQueue(session).holds(ctx.job_id, ctx.token)


def _store_page(ctx: TaskContext, meta: PageMeta, *, was: str) -> bool:
    """
    Stores one page's new metadata, fenced on the task's lease and on the page's stored hash still being `was` (the
    page the task read); whether it was stored
    """

    def mutate(row: RowMapping) -> dict[str, Any] | None:
        stored = [dict(entry) for entry in row["pages"] or []]
        for position, entry in enumerate(stored):
            if entry.get("index") == meta.index:
                if entry.get("page_sha256") != was:
                    return None  # the page changed since the task read it
                stored[position] = meta.model_dump(mode="json")
                return {"pages": stored}
        return None

    with session_context() as session:
        written = IngestQueue(session).update_job_json(ctx.job_id, mutate, where=IngestQueue.fence(ctx.token))
    return written is not None


def _settle_staged(ctx: TaskContext, page: CardPage) -> None:
    """
    After an error while a turn's metadata was being stored, when whether it was stored isn't known: the staged files
    are settled against what the row says now (`images.recover_staged`), or removed when the job is gone. If even
    that fails, they stay for the next task's `_recover_pages`.
    """
    try:
        with session_context() as session:
            job = IngestQueue(session).get(ctx.job_id)
            stored = next(
                (
                    PageMeta.model_validate(entry)
                    for entry in (job.pages if job else None) or []
                    if entry.get("index") == page.meta.index
                ),
                None,
            )
            session.commit()
        if stored is None:
            images.discard_staged(page.dir)
        else:
            images.recover_staged(page.dir, stored)
    except Exception as e:
        logger.warning(
            f"Recipe card job {ctx.job_id}: couldn't settle page {page.meta.index}'s staged turn now "
            f"({type(e).__name__}); the next task does"
        )


def _turn_page(
    ctx: TaskContext, page: CardPage, degrees: int, source: PageRotationSource, ocr_text: PageOCR | None
) -> PageMeta:
    """
    Turns one page clockwise by `degrees` and stores its new metadata, crash-safe, inside one write section (§3.9) and
    under the page's turn lock (`review.page_turn_lock`, which a manual rotate holds too, so their staged files never
    mix): the turned files are staged beside the current ones, their metadata (with `ocr_text`, read at the new
    rotation) is stored fenced on the task's lease, then they're swapped in. If the fence fails (the task was
    cancelled, swept or discarded meanwhile) the staged files are removed and the page stays as stored:
    `TaskFailed(interrupted)`. A crash at any step is settled by the next task (`_recover_pages`). Returns the page's
    new metadata. Blocking.
    """
    from .review import page_turn_lock  # here: the review service imports the runner

    with storage.ingest_write(), page_turn_lock(page.dir, wait=TURN_LOCK_WAIT):
        if not _holds_lease(ctx):
            raise TaskFailed(IngestErrorCode.interrupted)

        staged = images.stage_rotation(page.dir, page.meta, degrees, source).model_copy(update={"ocr": ocr_text})
        try:
            stored = _store_page(ctx, staged, was=page.meta.page_sha256)
        except Exception:
            _settle_staged(ctx, page)
            raise
        if not stored:
            # cancelled, swept or discarded meanwhile: the runner's fenced finalize drops the result too
            images.discard_staged(page.dir)
            raise TaskFailed(IngestErrorCode.interrupted)
        images.apply_staged(page.dir)
    return staged


def _orient_page(ctx: TaskContext, page: CardPage) -> PageMeta:
    """
    Orients one page with Tesseract (`decide_orientation`, outside the write section: it takes seconds, and a backup
    restore waits for write sections) and stores the outcome, fenced on the task's lease: a turn through `_turn_page`,
    else the page marked oriented with the text read. `TaskFailed(interrupted)` when the lease is gone, before any
    reading. Returns the page's metadata as stored. Blocking (Tesseract).
    """
    if not _holds_lease(ctx):
        raise TaskFailed(IngestErrorCode.interrupted)

    decision = decide_orientation(page)
    if not decision.settled:
        return page.meta
    if decision.rotation in TURNS:
        return _turn_page(ctx, page, decision.rotation, PageRotationSource.ocr, decision.ocr)

    meta = oriented_meta(page.meta, decision)
    if meta == page.meta:
        return meta
    with storage.ingest_write():
        if not _store_page(ctx, meta, was=page.meta.page_sha256):
            raise TaskFailed(IngestErrorCode.interrupted)
    return meta


async def _to_thread_to_the_end[T](func: Callable[..., T], *args: Any) -> T:
    """
    `func(*args)` in a thread, awaited to its end even when the task is cancelled meanwhile; the cancellation is then
    raised (whatever `func` did). For a step whose files and stored metadata mustn't be split: the thread would carry
    on after the task stopped waiting, and its write would race the runner's finalize or the shutdown's release.
    """
    step = asyncio.ensure_future(asyncio.to_thread(func, *args))
    cancelled = False
    while not step.done():
        try:
            await asyncio.wait({step})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not step.cancelled():
            step.exception()  # the cancellation wins; retrieved so it isn't logged as never retrieved
        raise asyncio.CancelledError()
    return step.result()


async def _orient(ctx: TaskContext, pages: list[CardPage]) -> None:
    """Turns the pages not yet oriented upright (Tesseract, in a thread), saving each one's metadata as it goes"""
    waiting = [page for page in pages if not page.meta.oriented]
    if not waiting or not orientation_available():
        return

    await ctx.report_progress(PROGRESS_ORIENTING)
    for page in waiting:
        page.meta = await _to_thread_to_the_end(_orient_page, ctx, page)


async def _turn_as_read(ctx: TaskContext, pages: list[CardPage], rotations: dict[int, int]) -> None:
    """
    Turns the pages the image reader said were sideways (`CardExtraction.rotations`), when Tesseract didn't orient
    them (a page already oriented, by Tesseract or by hand, keeps its turn): `rotation_source` model, `oriented` set.
    Fenced and crash-safe like orientation (`_turn_page`).
    """
    for page in pages:
        degrees = rotations.get(page.meta.index, 0) % 360
        if page.meta.oriented or degrees not in TURNS:
            continue
        page.meta = await _to_thread_to_the_end(_turn_page, ctx, page, degrees, PageRotationSource.model, None)


def _mode(payload: dict[str, Any] | None) -> IngestTaskMode:
    """What an extract task does (`task_payload.mode`): read the card again unless it says otherwise"""
    mode = payload.get("mode") if isinstance(payload, dict) else None
    if mode is None:
        return IngestTaskMode.reextract
    try:
        return IngestTaskMode(mode)
    except ValueError:
        raise TaskFailed(IngestErrorCode.internal_error) from None


async def handle_extract(ctx: TaskContext) -> ExtractResult | ParseLinesResult:
    """
    An extract task, by its mode (`task_payload.mode`): a first extraction, retry or re-extract (`reextract`, the
    default) orients pages not yet oriented, then `pipeline.extract_card` with a `JobOpenAIService` on a dedicated
    session, then turns the pages the image reader said were still sideways; `rebuild` builds the recipe again from the
    reviewer's edited transcription (`_rebuild`); `parse_lines` parses chosen ingredient lines with the AI parser
    (`_parse_chosen_lines`).
    """
    mode = _mode(ctx.payload)
    if mode == IngestTaskMode.rebuild:
        return await _rebuild(ctx)
    if mode == IngestTaskMode.parse_lines:
        return await _parse_chosen_lines(ctx)

    with session_context() as session:
        job = _load_job(session, ctx)
        pages = _card_pages(ctx, job)
        end_transaction(session)

        await _orient(ctx, pages)

        repos = get_repositories(session, group_id=ctx.group_id, household_id=ctx.household_id)
        ai = _ai_service(repos, ctx)
        options = options_for_group(session, ctx.group_id)
        end_transaction(session)

        extraction = await extract_card(
            pages,
            ai=ai,
            repos=repos,
            translator=get_locale_provider(ctx.locale),
            options=options,
            on_progress=ctx.report_progress,
        )
        await _turn_as_read(ctx, pages, extraction.rotations)

    return ExtractResult(
        draft=extraction.draft,
        flags=extraction.flags,
        transcription=extraction.transcription,
        extraction=extraction.extraction,
        pages=[page.meta for page in pages],
    )


# ==========================================
# Rebuild from the edited transcription, and Parse with AI


def rebuild_payload(transcription: str) -> dict[str, Any]:
    """The `task_payload` of an extract task that builds the recipe again from `transcription` (no image read)"""
    return {"mode": IngestTaskMode.rebuild.value, "transcription": transcription}


def parse_lines_payload(draft: CardDraft, refs: Sequence[UUID]) -> dict[str, Any]:
    """
    The `task_payload` of an extract task that parses the draft's lines `refs` with the AI ingredient parser, each
    with its text as it reads now (`ingredient_line`): a line the reviewer changes meanwhile keeps their change.
    Raises `KeyError` for a ref the draft hasn't.
    """
    lines = {str(ingredient.reference_id): ingredient for ingredient in draft.ingredients}
    chosen = [lines[str(ref)] for ref in dict.fromkeys(refs)]
    return {
        "mode": IngestTaskMode.parse_lines.value,
        "lines": [{"ref": str(line.reference_id), "text": ingredient_line(line)} for line in chosen],
    }


def _transcription(payload: dict[str, Any] | None) -> str:
    text = payload.get("transcription") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TRANSCRIPTION:
        raise TaskFailed(IngestErrorCode.internal_error)
    return text


async def _rebuild(ctx: TaskContext) -> ExtractResult:
    """
    The recipe built again from the reviewer's edited transcription (`pipeline.rebuild_from_transcription`: the build
    steps, ingredients and flags; no image read, no page turned). Finalized as a re-extract is: it replaces a draft
    nobody edited, else becomes a whole-card proposal marked as a rebuild.
    """
    transcription = _transcription(ctx.payload)
    with session_context() as session:
        job = _load_job(session, ctx)
        pages = _card_pages(ctx, job)
        previous = ExtractionMeta.model_validate(job.extraction) if job.extraction else None
        end_transaction(session)

        repos = get_repositories(session, group_id=ctx.group_id, household_id=ctx.household_id)
        ai = _ai_service(repos, ctx)
        options = options_for_group(session, ctx.group_id)
        end_transaction(session)

        rebuilt = await rebuild_from_transcription(
            pages,
            transcription,
            ai=ai,
            repos=repos,
            translator=get_locale_provider(ctx.locale),
            options=options,
            previous=previous,
            on_progress=ctx.report_progress,
        )

    return ExtractResult(
        draft=rebuilt.draft,
        flags=rebuilt.flags,
        transcription=rebuilt.transcription,
        extraction=rebuilt.extraction,
        pages=[page.meta for page in pages],
        origin=CardProposalOrigin.rebuild,
    )


def _lines_to_parse(payload: dict[str, Any] | None) -> dict[str, str]:
    """A `parse_lines` payload's lines: each ref with its text as sent, in order"""
    lines = payload.get("lines") if isinstance(payload, dict) else None
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_PARSE_LINES:
        raise TaskFailed(IngestErrorCode.internal_error)
    sent: dict[str, str] = {}
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("text"), str) or _uuid(line.get("ref")) is None:
            raise TaskFailed(IngestErrorCode.internal_error)
        sent[str(_uuid(line["ref"]))] = line["text"]
    return sent


async def _parse_chosen_lines(ctx: TaskContext) -> ParseLinesResult:
    """
    Chosen ingredient lines parsed by the AI ingredient parser in any language (`pipeline.parse_lines`, the review
    page's "Parse with AI"), through the job's own service under its policy. The runner writes the parsed fields into
    the lines still as they were sent (`finalize.finalize_parse_lines`). The provider's error fails the task (a banner).
    """
    sent = _lines_to_parse(ctx.payload)
    with session_context() as session:
        job = _load_job(session, ctx)
        draft = CardDraft.model_validate(job.draft) if job.draft else None
        extraction = ExtractionMeta.model_validate(job.extraction) if job.extraction else None
        end_transaction(session)

        current = {str(line.reference_id): line for line in draft.ingredients} if draft else {}
        lines = [
            IngredientLine(text=text, title=current[ref].title if ref in current else None, reference_id=UUID(ref))
            for ref, text in sent.items()
        ]
        repos = get_repositories(session, group_id=ctx.group_id, household_id=ctx.household_id)
        matcher = IngestMatcher(repos)
        ingredients = await parse_lines(
            lines,
            ai=_ai_service(repos, ctx),
            repos=repos,
            translator=get_locale_provider(ctx.locale),
            matcher=matcher,
            language=extraction.language if extraction else None,
        )
        # what the parsed lines' parse flags are judged with, as extraction's are (`finalize_parse_lines`)
        units = matcher.unit_names()
        linked = matcher.linked_names([*(draft.ingredients if draft else []), *ingredients])
        end_transaction(session)
    return ParseLinesResult(ingredients=ingredients, sent=sent, units=units, linked=linked)


# ==========================================
# Re-reads


def _reread_request(payload: dict[str, Any] | None) -> tuple[int, Region, ProposalTarget]:
    """
    The page, region and target of a re-read's `task_payload`: a `RereadRequest` as JSON (`page`, `x`, `y`, `width`,
    `height`, `target`), the region optionally nested as `region`.
    """
    if not payload:
        raise TaskFailed(IngestErrorCode.internal_error)
    try:
        box = payload.get("region") or payload
        region = Region(float(box["x"]), float(box["y"]), float(box["width"]), float(box["height"]))
        return int(payload["page"]), region, ProposalTarget.model_validate(payload["target"])
    except KeyError, TypeError, ValueError, ValidationError:
        raise TaskFailed(IngestErrorCode.internal_error) from None


def _previous_text(draft: CardDraft | None, target: ProposalTarget) -> str | None:
    """What the draft says now in the re-read's target, for the model to compare with"""
    if draft is None:
        return None

    field = field_name(target.field)
    if field in DRAFT_TEXT_FIELDS:
        return getattr(draft, DRAFT_TEXT_FIELDS[field]) or None
    if field == FIELD_INGREDIENTS:
        return next((ingredient_line(i) for i in draft.ingredients if str(i.reference_id) == target.ref), None)
    if field == FIELD_STEPS:
        return next((step.text for step in draft.steps if str(step.id) == target.ref), None)
    if field == FIELD_NOTES and target.ref:
        # a note by its id, as its flags name it; a client from before note ids sends the note's position
        note = next((note for note in draft.notes if str(note.id) == target.ref), None)
        if note is None and target.ref.isdigit() and int(target.ref) < len(draft.notes):
            note = draft.notes[int(target.ref)]
        return note.text if note else None
    return None


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


async def handle_reread(ctx: TaskContext) -> RereadResult:
    """
    A region re-read (`ctx.payload` holds the page, region and target): `pipeline.reread_region`. A reading for an
    ingredient line is parsed and linked as extraction does (a card in another language by the AI parser, on the same
    service), and comes back as the proposal's one-line draft.
    """
    index, region, target = _reread_request(ctx.payload)

    with session_context() as session:
        job = _load_job(session, ctx)
        pages = _card_pages(ctx, job)
        draft = CardDraft.model_validate(job.draft) if job.draft else None
        extraction = ExtractionMeta.model_validate(job.extraction) if job.extraction else None
        end_transaction(session)

        page = next((page for page in pages if page.meta.index == index), None)
        if page is None:
            raise TaskFailed(IngestErrorCode.internal_error)

        repos = get_repositories(session, group_id=ctx.group_id, household_id=ctx.household_id)
        ai = _ai_service(repos, ctx)
        proposal = await reread_region(page, region, target, _previous_text(draft, target), ai=ai)

        if field_name(target.field) == FIELD_INGREDIENTS and proposal.readable and proposal.text:
            current = next((i for i in draft.ingredients if str(i.reference_id) == target.ref), None) if draft else None
            line = IngredientLine(
                text=proposal.text,
                title=current.title if current else None,
                reference_id=_uuid(target.ref),
            )
            ingredients = await normalize_lines(
                [line],
                repos=repos,
                translator=get_locale_provider(ctx.locale),
                matcher=IngestMatcher(repos),
                language=extraction.language if extraction else None,
                ai=ai,
            )
            if ingredients:
                proposal.draft = CardDraft(ingredients=ingredients)

    return RereadResult(proposal=proposal)
