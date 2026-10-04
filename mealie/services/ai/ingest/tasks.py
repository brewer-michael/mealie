"""
The recipe card task handlers (docs/ai/PHASE2.md §3.7): the runner calls one per claimed task, in the task's thread
and event loop, with the locale context and the job's AI call policy already set. A handler opens its own sessions,
returns a result and writes nothing to the job row: the runner applies the result with a write fenced on the lease.
A failure it understands is raised as `TaskFailed`.

The one write is orientation's: a page Tesseract turned has new files on disk at once, so its new metadata is written
straight away, fenced on the task's lease like every write by a running task (§3.3). A later failure (a rate limit,
say) would otherwise leave the row describing the page as it was before it turned. Turning a page and storing its
metadata are one step (`_orient_page`): one write section, in one thread, awaited to its end even when the task is
cancelled meanwhile; if the fence fails, the page's files are put back as they were. So a shutdown, a cancel, a lost
lease or a later page's failure never leaves a turned page described as it was before it turned.

Each handler's AI session (`JobOpenAIService` on `get_repositories(session, group_id=…, household_id=…)`) is only used
for reading the job, routing reads, the usage log and read-only lookups, and no transaction stays open across an
await (F11).
"""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from mealie.db.db_setup import session_context
from mealie.db.models.household.household import Household
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.lang.providers import get_locale_provider
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestQueue, IngestRepos
from mealie.schema.recipe_ingest import (
    CardDraft,
    ExtractionMeta,
    IngestErrorCode,
    PageMeta,
    ProposalTarget,
)
from mealie.services import ocr

from . import images, storage
from .images import Region
from .matching import IngestMatcher
from .pipeline import CardPage, extract_card, options_for_group, orient_page, reread_region
from .pipeline.context import PROGRESS_ORIENTING
from .pipeline.flags import DRAFT_TEXT_FIELDS, FIELD_INGREDIENTS, FIELD_NOTES, FIELD_STEPS, ingredient_line
from .pipeline.ingredients import IngredientLine, normalize_lines
from .pipeline.reread import field_name
from .pipeline.service import JobOpenAIService, end_transaction
from .runner.types import ExtractResult, RereadResult, TaskContext, TaskFailed


def _load_job(session: Session, ctx: TaskContext) -> RecipeIngestionJob:
    """The task's job, or `TaskFailed`: `owner_missing` when its household is gone, else `interrupted`"""
    job = IngestRepos(session, ctx.group_id, ctx.household_id).jobs.get(ctx.job_id)
    if job is None:
        household = session.execute(sa.select(Household.id).where(Household.id == ctx.household_id)).first()
        end_transaction(session)
        raise TaskFailed(IngestErrorCode.interrupted if household else IngestErrorCode.owner_missing)
    return job


def _card_pages(ctx: TaskContext, job: RecipeIngestionJob) -> list[CardPage]:
    """The job's pages; `FileNotFoundError` when a page's files are gone"""
    pages = [
        CardPage(dir=storage.page_dir(ctx.group_id, ctx.job_id, meta.index), meta=meta)
        for meta in (PageMeta.model_validate(page) for page in job.pages or [])
    ]
    if not pages:
        raise FileNotFoundError(storage.job_dir(ctx.group_id, ctx.job_id))
    for page in pages:
        for path in (page.page_path, page.view_path):
            if not path.is_file():
                raise FileNotFoundError(path)
    return pages


_PAGE_FILES = (images.VIEW_FILE, images.THUMB_FILE, images.PAGE_FILE)
"""A page's files, in the order they're written back: `page.jpg` last, as its hash says the page changed"""


def _put_back(page: CardPage, files: dict[str, bytes]) -> None:
    """Writes a page's files back as they were before it turned (inside the same write section)"""
    try:
        for name in _PAGE_FILES:
            storage.atomic_write_bytes(page.dir / name, files[name])
    except FileNotFoundError:
        pass  # the job was discarded meanwhile: nothing left to put back


def _orient_page(ctx: TaskContext, page: CardPage) -> PageMeta:
    """
    Orients one page and stores its new metadata, fenced on the task's lease, as one step inside one write section
    (§3.9). If the fence fails (the task was cancelled, swept or discarded meanwhile) or the write does, the page's
    files are put back byte for byte, so they never disagree with the stored metadata. `TaskFailed(interrupted)` when
    the lease is gone. Blocking (Tesseract).
    """
    with storage.ingest_write():
        with session_context() as session:
            if not IngestQueue(session).holds(ctx.job_id, ctx.token):
                raise TaskFailed(IngestErrorCode.interrupted)

        before = {name: (page.dir / name).read_bytes() for name in _PAGE_FILES}
        meta = orient_page(page)
        if meta == page.meta:
            return meta
        turned = meta.page_sha256 != page.meta.page_sha256

        def mutate(row: RowMapping) -> dict[str, Any] | None:
            stored = [dict(entry) for entry in row["pages"] or []]
            for position, entry in enumerate(stored):
                if entry.get("index") == meta.index:
                    stored[position] = meta.model_dump(mode="json")
                    return {"pages": stored}
            return None

        try:
            with session_context() as session:
                written = IngestQueue(session).update_job_json(ctx.job_id, mutate, where=IngestQueue.fence(ctx.token))
        except BaseException:
            if turned:
                _put_back(page, before)
            raise
        if written is None:
            # cancelled, swept or discarded meanwhile: the runner's fenced finalize drops the result too
            if turned:
                _put_back(page, before)
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
    if not waiting or not ocr.is_available():
        return

    await ctx.report_progress(PROGRESS_ORIENTING)
    for page in waiting:
        page.meta = await _to_thread_to_the_end(_orient_page, ctx, page)


async def handle_extract(ctx: TaskContext) -> ExtractResult:
    """
    A first extraction, retry or re-extract: orients pages not yet oriented, then `pipeline.extract_card` with a
    `JobOpenAIService` on a dedicated session.
    """
    with session_context() as session:
        job = _load_job(session, ctx)
        pages = _card_pages(ctx, job)
        end_transaction(session)

        await _orient(ctx, pages)

        repos = get_repositories(session, group_id=ctx.group_id, household_id=ctx.household_id)
        ai = JobOpenAIService(repos)
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

    return ExtractResult(
        draft=extraction.draft,
        flags=extraction.flags,
        transcription=extraction.transcription,
        extraction=extraction.extraction,
        pages=[page.meta for page in pages],
    )


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
    if field == FIELD_NOTES and target.ref and target.ref.isdigit() and int(target.ref) < len(draft.notes):
        return draft.notes[int(target.ref)].text
    return None


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


async def handle_reread(ctx: TaskContext) -> RereadResult:
    """
    A region re-read (`ctx.payload` holds the page, region and target): `pipeline.reread_region`. A reading for an
    ingredient line is parsed and linked as extraction does, and comes back as the proposal's one-line draft.
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
        ai = JobOpenAIService(repos)
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
            )
            if ingredients:
                proposal.draft = CardDraft(ingredients=ingredients)

    return RereadResult(proposal=proposal)
