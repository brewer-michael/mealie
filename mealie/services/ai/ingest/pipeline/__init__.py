"""
The recipe card extraction pipeline (docs/ai/PHASE2.md §4, §5): everything between a job's normalized pages and its
draft, flags and transcription. The worker and the eval (§11) call exactly these functions, so the eval scores the
production pipeline.

- `orient_page` (step 0): Tesseract turns a sideways page upright, with a margin, and keeps its text.
- `extract_card` (steps 1-6): the card compilers, the optional cross-read on the same `ai`, the build and organizer
  steps, ingredient normalization and linking, and the flags. No database writes.
- `reread_region`: one region of a page read again, as a proposal.
- `options_for_group`: the options from the group's recipe card settings.
"""

import asyncio
from uuid import UUID

from sqlalchemy.orm import Session

from mealie.core import exceptions
from mealie.core.root_logger import get_logger
from mealie.lang.providers import Translator
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_factory import AllRepositories
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.openai.organizers import OpenAIOrganizers
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftNote,
    CardDraftRef,
    CardDraftStep,
    ExtractionCompilerError,
    ExtractionMeta,
    IngestReadPath,
)
from mealie.services.ai.errors import AIProviderLimitReachedError, AIProviderLocalOnlyError, describe_provider_error
from mealie.services.openai import OpenAINotEnabledException, OpenAIService
from mealie.services.recipe.import_workflow import RecipeImportWorkflow
from mealie.services.recipe.import_workflow.exceptions import NoRecipeDataError
from mealie.services.recipe.import_workflow.recipe_conversion import DEFAULT_RECIPE_NAME, DEFAULT_RECIPE_NAME_KEY
from mealie.services.recipe.organizer_resolver import OrganizerResolver

from ..matching import IngestMatcher
from ..runner.types import ProgressCallback
from .cardtext import canonical_markers
from .compilers import CapturedError
from .context import PROGRESS_CROSS_READING, PROGRESS_LINKING_INGREDIENTS, CardWorkflowContext
from .crossread import read_transcript
from .flags import compute_flags
from .ingredients import normalize_ingredients
from .llm_schemas import OpenAIRecipeCardTranscription
from .models import CardExtraction, CardPage, CardPipelineOptions, CardReadPath
from .orient import orient_page
from .reread import reread_region
from .service import JobAIRuntime, end_transaction
from .steps import card_workflow_steps

__all__ = [
    "CardExtraction",
    "CardPage",
    "CardPipelineOptions",
    "CardReadPath",
    "extract_card",
    "options_for_group",
    "orient_page",
    "reread_region",
]

logger = get_logger(__name__)

READ_ERROR_PRECEDENCE: tuple[type[BaseException], ...] = (
    exceptions.RateLimitError,
    AIProviderLocalOnlyError,
    AIProviderLimitReachedError,
    OpenAINotEnabledException,
)
"""When nothing could read the card, the recorded error to raise: the first of these, else the first recorded"""


def options_for_group(session: Session, group_id: UUID) -> CardPipelineOptions:
    """
    The pipeline options from the group's recipe card settings (defaults without a row), with `suggest_organizers`
    off when the group has no tags, categories or tools, so card text isn't sent for nothing. The eval uses it too,
    then sets `read_path` per config.
    """
    settings = IngestRepos(session, group_id, None).settings.get()
    organizers = OrganizerResolver(get_repositories(session, group_id=group_id, household_id=None)).existing_names()
    return CardPipelineOptions(cross_read=settings.cross_read, suggest_organizers=any(organizers.values()))


def _reading_error(ctx: CardWorkflowContext) -> BaseException | None:
    """The most specific reason no compiler read the card; None when one read it and found no recipe"""
    recorded = [captured.error for captured in ctx.compiler_errors]
    for kind in READ_ERROR_PRECEDENCE:
        for error in recorded:
            if isinstance(error, kind):
                return error
    if recorded:
        return recorded[0]
    if not ctx.compilers_tried:
        return OpenAINotEnabledException("The card can't be read: no image provider is set and OCR isn't available")
    return None


def _retrieve(task: asyncio.Task) -> None:
    """Marks a background task's outcome as seen, so an abandoned failure isn't reported as never retrieved"""
    if not task.cancelled():
        task.exception()


Suggestions = tuple[list[CardDraftRef], list[CardDraftRef], list[CardDraftRef]]
"""Tags, categories and tools"""


def _suggestions(repos: AllRepositories, names: OpenAIOrganizers | None) -> Suggestions:
    """The suggested tags, categories and tools that match the group's own (by name, then fuzzily); never created"""
    if names is None:
        return [], [], []

    resolver = OrganizerResolver(repos)
    try:
        return (
            [CardDraftRef(id=tag.id, name=tag.name) for tag in resolver.resolve_tags(names.tags, False)],
            [CardDraftRef(id=c.id, name=c.name) for c in resolver.resolve_categories(names.categories, False)],
            [CardDraftRef(id=tool.id, name=tool.name) for tool in resolver.resolve_tools(names.tools, False)],
        )
    finally:
        end_transaction(repos.session)


def _draft(
    recipe: Recipe,
    ctx: CardWorkflowContext,
    translator: Translator,
    ingredients: list[CardDraftIngredient],
    organizers: Suggestions,
) -> CardDraft:
    name = canonical_markers((recipe.name or "").strip())
    if name == translator.t(DEFAULT_RECIPE_NAME_KEY, DEFAULT_RECIPE_NAME):
        name = ""  # the build step found no name; the placeholder would hide `missing_name`

    tags, categories, tools = organizers
    return CardDraft(
        name=name,
        description=canonical_markers(recipe.description or ""),
        recipe_yield=canonical_markers(recipe.recipe_yield) if recipe.recipe_yield else None,
        recipe_yield_quantity=recipe.recipe_yield_quantity or None,
        recipe_servings=recipe.recipe_servings or None,
        prep_time=canonical_markers(recipe.prep_time) if recipe.prep_time else None,
        perform_time=canonical_markers(recipe.perform_time) if recipe.perform_time else None,
        total_time=canonical_markers(recipe.total_time) if recipe.total_time else None,
        attribution=ctx.attribution,
        ingredients=ingredients,
        steps=[
            CardDraftStep(
                title=canonical_markers(step.title) if step.title else None, text=canonical_markers(step.text)
            )
            for step in recipe.recipe_instructions or []
            if step.text.strip()
        ],
        notes=[
            CardDraftNote(title=canonical_markers(note.title or ""), text=canonical_markers(note.text))
            for note in recipe.notes or []
            if note.text.strip()
        ],
        tags=tags,
        categories=categories,
        tools=tools,
    )


def _reader(ai: OpenAIService, read_path: IngestReadPath | None) -> tuple[str | None, str | None]:
    """The provider and model that read the card"""
    runtime = ai.runtime
    if isinstance(runtime, JobAIRuntime) and (answered := runtime.answered_by(OpenAIRecipeCardTranscription.__name__)):
        return answered

    # without the job runtime's tallies (the eval pins one provider per slot): the slot's provider
    provider = ai.image_provider if read_path == IngestReadPath.image else ai.default_provider
    return (provider.name, provider.model) if provider else (None, None)


async def extract_card(
    pages: list[CardPage],
    *,
    ai: OpenAIService,
    repos: AllRepositories,
    translator: Translator,
    options: CardPipelineOptions,
    on_progress: ProgressCallback | None = None,
) -> CardExtraction:
    """
    Reads a card and builds its draft (§4.1): the card compilers (image, then the OCR fallback, per
    `options.read_path`), the optional cross-read on the same `ai`, the build and organizer steps, ingredient
    normalization and linking, and the flags. Makes no database writes.

    Raises the most specific recorded provider error when nothing could read the card (`RateLimitError`,
    `AIProviderLocalOnlyError`, `AIProviderLimitReachedError`, `OpenAINotEnabledException`, others), and
    `NoRecipeDataError` when the card holds no recipe.
    """
    errors: list[CapturedError] = []
    ctx = CardWorkflowContext.for_card(
        pages,
        ai=ai,
        repos=repos,
        translator=translator,
        options=options,
        errors=errors,
        on_progress=on_progress,
    )

    # the second reading runs alongside the first, so it adds cost but not wait
    cross_read: asyncio.Task[list[str]] | None = None
    if options.cross_read and ai.image_provider is not None:
        cross_read = asyncio.create_task(read_transcript(pages, ai=ai))
        cross_read.add_done_callback(_retrieve)

    try:
        try:
            result = await RecipeImportWorkflow(card_workflow_steps(options, errors)).run(ctx)
        except NoRecipeDataError:
            if ctx.compiled_source is None and (error := _reading_error(ctx)) is not None:
                # its own cause (the provider's error) stays, for `describe_provider_error`
                raise error from error.__cause__
            raise
    except BaseException:
        if cross_read is not None:
            cross_read.cancel()
        raise

    recipe = result.recipe
    compiled = ctx.compiled_source
    assert compiled is not None  # the compile step either set it or raised
    organizers = _suggestions(repos, ctx.organizer_names)

    cross_read_lines: list[str] | None = None
    cross_read_failed = False
    if cross_read is not None:
        if not cross_read.done():
            await ctx.report_progress(PROGRESS_CROSS_READING)
        try:
            cross_read_lines = await cross_read
        except Exception as e:
            cross_read_failed = True
            logger.warning(f"The second reading of a card failed ({describe_provider_error(e)}); fewer checks ran")

    await ctx.report_progress(PROGRESS_LINKING_INGREDIENTS)
    ingredients = await normalize_ingredients(
        recipe, repos=repos, translator=translator, matcher=IngestMatcher(repos), language=compiled.language
    )

    draft = _draft(recipe, ctx, translator, ingredients, organizers)
    provider, model = _reader(ai, ctx.read_path)
    runtime = ai.runtime
    extraction = ExtractionMeta(
        read_path=ctx.read_path,
        language=compiled.language,
        attribution=ctx.attribution,
        unsure=ctx.unsure,
        cross_read_lines=cross_read_lines,
        cross_read_failed=cross_read_failed,
        ocr_confidence=ctx.ocr_confidence,
        provider=provider,
        model=model,
        step_outcomes={name: outcome.value for name, outcome in result.outcomes.items()},
        compiler_errors=[
            ExtractionCompilerError(compiler=captured.compiler, error=captured.description) for captured in errors
        ],
        usage=runtime.usage if isinstance(runtime, JobAIRuntime) else [],
    )
    flags = compute_flags(draft, extraction, {}, transcription=compiled.content)
    return CardExtraction(draft=draft, flags=flags, transcription=compiled.content, extraction=extraction)
