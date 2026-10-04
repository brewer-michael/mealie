"""
The card workflow's steps (docs/ai/PHASE2.md §4.1): upstream's import workflow with card compilers and prompts, and
no translation or `finalize_scraped_recipe`.
"""

from mealie.core.root_logger import get_logger
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.schema.openai.recipe import OpenAIRecipe
from mealie.services.ai.errors import AIProviderLimitReachedError, AIProviderLocalOnlyError, describe_provider_error
from mealie.services.recipe.import_workflow.base import WorkflowStep
from mealie.services.recipe.import_workflow.compilers.base import SourceCompiler
from mealie.services.recipe.import_workflow.context import WorkflowContext
from mealie.services.recipe.import_workflow.exceptions import NoRecipeDataError
from mealie.services.recipe.import_workflow.recipe_conversion import to_recipe
from mealie.services.recipe.import_workflow.steps import BuildRecipeStep, CompileSourceStep, ResolveOrganizersStep
from mealie.services.recipe.import_workflow.steps.build_recipe import BUILD_RECIPE_PROMPT
from mealie.services.scraper import cleaner

from .compilers import CapturedError, CardImageCompiler, CardOCRCompiler, capture_errors
from .context import CardWorkflowContext
from .models import CardPipelineOptions

logger = get_logger(__name__)

CARD_BUILD_RULES_PROMPT = "recipes.card-build-rules"


class CardBuildRecipeStep(BuildRecipeStep):
    """
    Upstream's build step (default slot, `OpenAIRecipe`, then `to_recipe` and `cleaner.clean`), with
    `card-build-rules.txt` appended to its prompt: markers stay where they are and ingredient lines stay as written.
    """

    async def run(self, ctx: WorkflowContext) -> None:
        prompt = f"{ctx.ai.get_prompt(BUILD_RECIPE_PROMPT)}\n\n{ctx.ai.get_prompt(CARD_BUILD_RULES_PROMPT)}"
        response = await ctx.ai.get_response(prompt, self._build_message(ctx), response_schema=OpenAIRecipe)

        if not response:
            raise NoRecipeDataError(ctx.translator.t("recipe.import-errors.provider-returned-nothing"))

        if not (response.ingredients or response.instructions):
            raise NoRecipeDataError(ctx.translator.t("recipe.import-errors.no-recipe-found"))

        ctx.draft_recipe = cleaner.clean(to_recipe(ctx, response), ctx.translator)


class OrganizerSuggestionFailed(Exception):
    """The organizer step failed; the message is `describe_provider_error`'s, never the provider's"""


class CardResolveOrganizersStep(ResolveOrganizersStep):
    """
    Upstream's organizer step (fast slot), configured by the context's `WorkflowOptions` to store the names only.
    It's optional, so the workflow logs a failure with its traceback and moves on; this raises a failure again with
    only its safe description, so no provider response body reaches the log.

    When no provider may be asked (a local-only card whose fast slot has no local provider, or every provider over
    its monthly limit) it isn't a failure: the reason is kept on the context (`organizers_skipped`), and the step's
    outcome says so (`flags.organizers_outcome`), so the review page can tell the reviewer.
    """

    async def run(self, ctx: WorkflowContext) -> None:
        try:
            await super().run(ctx)
        except (AIProviderLocalOnlyError, AIProviderLimitReachedError) as e:
            reason = "local_only" if isinstance(e, AIProviderLocalOnlyError) else "limit_reached"
            if isinstance(ctx, CardWorkflowContext):
                ctx.organizers_skipped = reason
            logger.info(f"Tag suggestions skipped ({describe_provider_error(e)})")
        except Exception as e:
            raise OrganizerSuggestionFailed(describe_provider_error(e)) from None


class TranscriptionStep(WorkflowStep):
    """
    Takes a transcription as it is (the reviewer's edit of the card's), in place of reading the card: the build step
    then structures it. Its name is the extraction's record of where the draft came from.
    """

    name = "transcription"

    def __init__(self, transcription: str, language: str | None) -> None:
        self.transcription = transcription
        self.language = language

    async def run(self, ctx: WorkflowContext) -> None:
        if not self.transcription.strip():
            raise NoRecipeDataError(ctx.translator.t("recipe.import-errors.no-recipe-found"))
        ctx.compiled_source = OpenAICompiledSource(
            contains_recipe=True, content=self.transcription, language=self.language
        )


def card_rebuild_steps(options: CardPipelineOptions, transcription: str, language: str | None) -> list[WorkflowStep]:
    """`TranscriptionStep`, then the card build step, then `ResolveOrganizersStep` when `options.suggest_organizers`"""
    steps: list[WorkflowStep] = [TranscriptionStep(transcription, language), CardBuildRecipeStep()]
    if options.suggest_organizers:
        steps.append(CardResolveOrganizersStep())
    return steps


def card_workflow_steps(options: CardPipelineOptions, errors: list[CapturedError]) -> list[WorkflowStep]:
    """
    `CompileSourceStep` with the card compilers for `options.read_path`, each wrapped by `capture_errors(…, errors)`;
    then the card build step; then `ResolveOrganizersStep` when `options.suggest_organizers`.
    """
    compilers: list[type[SourceCompiler]]
    match options.read_path:
        case "image":
            compilers = [CardImageCompiler]
        case "ocr":
            compilers = [CardOCRCompiler]
        case _:
            compilers = [CardImageCompiler, CardOCRCompiler]

    steps: list[WorkflowStep] = [
        CompileSourceStep(compilers=[capture_errors(compiler, errors) for compiler in compilers]),
        CardBuildRecipeStep(),
    ]
    if options.suggest_organizers:
        steps.append(CardResolveOrganizersStep())
    return steps
