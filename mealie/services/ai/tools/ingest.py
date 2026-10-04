"""
Recipe card tools (docs/ai/PHASE2.md §12): how many scanned cards are waiting, for a voice assistant.

**Counts only.** A card's name is text read off the card, and the MCP client or Home Assistant's conversation agent
may be cloud-hosted, so no title, transcription or other card text ever goes out, whether the card is local-only or
not. There's no write tool: scanning needs photos, which voice clients don't have.
"""

from pydantic import Field

from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import RecipeIngestionJobCounts

from .base import AITool, ToolArgs, ToolContext, ToolResult, run_blocking
from .speech import join_words


class RecipeCardQueueArgs(ToolArgs):
    pass


class RecipeCardQueueResult(ToolResult):
    ready: int = Field(description="Cards read and waiting to be reviewed")
    needs_attention: int = Field(description="Of the ready cards, those with something to check")
    processing: int = Field(description="Cards still being read")
    failed: int = Field(description="Cards that couldn't be read")


def _cards(count: int) -> str:
    return f"{count} recipe card{'' if count == 1 else 's'}"


def queue_speech(counts: RecipeIngestionJobCounts) -> str:
    """'7 recipe cards are ready to review. 2 need a closer look and 1 is still being read.'"""
    if counts.ready:
        first = f"{_cards(counts.ready)} {'is' if counts.ready == 1 else 'are'} ready to review."
    elif counts.processing or counts.failed:
        first = "No recipe cards are ready to review yet."
    else:
        return "No recipe cards are waiting to be reviewed."

    rest: list[str] = []
    if counts.needs_attention == counts.ready > 0:
        rest.append("it needs a closer look" if counts.ready == 1 else "all of them need a closer look")
    elif counts.needs_attention:
        rest.append(f"{counts.needs_attention} {'needs' if counts.needs_attention == 1 else 'need'} a closer look")
    if counts.processing:
        rest.append(f"{counts.processing} {'is' if counts.processing == 1 else 'are'} still being read")
    if counts.failed:
        rest.append(f"{counts.failed} couldn't be read")

    if not rest:
        return first
    second = join_words(rest)
    return f"{first} {second[0].upper()}{second[1:]}."


def _recipe_card_queue(ctx: ToolContext, args: RecipeCardQueueArgs) -> RecipeCardQueueResult:
    # the caller's household only, like the cards page
    counts = IngestRepos(ctx.repos.session, ctx.user.group_id, ctx.user.household_id).jobs.counts()
    return RecipeCardQueueResult(
        speech=queue_speech(counts),
        ready=counts.ready,
        needs_attention=counts.needs_attention,
        processing=counts.processing,
        failed=counts.failed,
    )


recipe_card_queue = AITool(
    name="recipe_card_queue",
    description=(
        "How many scanned recipe cards are waiting in Mealie: ready to review, needing a closer look, still being "
        "read, or failed. Use it for questions like 'any recipe cards to review?'. Returns counts only; the cards "
        "themselves are reviewed in Mealie's Recipe cards page."
    ),
    args=RecipeCardQueueArgs,
    result=RecipeCardQueueResult,
    writes=False,
    handler=run_blocking(_recipe_card_queue),
)
