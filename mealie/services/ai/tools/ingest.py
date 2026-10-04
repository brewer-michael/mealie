"""
Recipe card tools (docs/ai/PHASE2.md §12): how many scanned cards are waiting, for a voice assistant.

**Counts only.** A card's name is text read off the card, and the MCP client or Home Assistant's conversation agent
may be cloud-hosted, so no title, transcription or other card text ever goes out, whether the card is local-only or
not. There's no write tool: scanning needs photos, which voice clients don't have.

The speech is in the caller's language (`recipe-ingest.voice` texts, falling back to en-US), with the counts in digits.
The result's keys are snake_case like every tool's, while the REST counts (`/api/ai/ingest/jobs/counts`) are camelCase.
"""

from pydantic import Field

from mealie.lang.providers import Translator
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import RecipeIngestionJobCounts
from mealie.services.ai.ingest.i18n import with_fallback

from .base import AITool, ToolArgs, ToolContext, ToolResult, run_blocking

VOICE = "recipe-ingest.voice"


class RecipeCardQueueArgs(ToolArgs):
    pass


class RecipeCardQueueResult(ToolResult):
    ready: int = Field(description="Cards read and waiting to be reviewed")
    needs_attention: int = Field(description="Of the ready cards, those with something to check")
    processing: int = Field(description="Cards still being read")
    failed: int = Field(description="Cards that couldn't be read")


def queue_speech(counts: RecipeIngestionJobCounts, translator: Translator) -> str:
    """
    '7 recipe cards are ready to review. 2 need a closer look and 1 is still being read.', in `translator`'s language
    (en-US for a text it doesn't have)
    """
    t = with_fallback(translator).t
    if not (counts.ready or counts.processing or counts.failed):
        return t(f"{VOICE}.none-waiting")
    first = t(f"{VOICE}.ready", count=counts.ready)  # "No recipe cards are ready to review yet." for none

    rest: list[str] = []
    if counts.needs_attention == counts.ready > 0:
        rest.append(t(f"{VOICE}.all-need-a-look", count=counts.ready))
    elif counts.needs_attention:
        rest.append(t(f"{VOICE}.need-a-look", count=counts.needs_attention))
    if counts.processing:
        rest.append(t(f"{VOICE}.still-reading", count=counts.processing))
    if counts.failed:
        rest.append(t(f"{VOICE}.failed", count=counts.failed))

    if not rest:
        return first
    details = rest[0]
    if len(rest) > 1:
        details = t(f"{VOICE}.list-and", first=t(f"{VOICE}.list-separator").join(rest[:-1]), last=rest[-1])
    second = t(f"{VOICE}.details", details=details)
    return f"{first} {second[:1].upper()}{second[1:]}"


def _recipe_card_queue(ctx: ToolContext, args: RecipeCardQueueArgs) -> RecipeCardQueueResult:
    # the caller's household only, like the cards page
    counts = IngestRepos(ctx.repos.session, ctx.user.group_id, ctx.user.household_id).jobs.counts()
    return RecipeCardQueueResult(
        speech=queue_speech(counts, ctx.translator),
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
