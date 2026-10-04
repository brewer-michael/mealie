"""
The response schemas the recipe card pipeline asks providers for (docs/ai/PHASE2.md §4.2). They live here rather than
in `mealie/schema/openai/`, so upstream's package and the code generator are untouched; `test_anthropic_adapter.py`
adds them to `RESPONSE_SCHEMAS`, so Claude's limit test and the minimal-answer parse cover them.

Claude compiles at most 24 optional and 16 union parameters per request and rejects numeric constraints: the
transcription has 4 optional parameters, the region 1 and the transcript none, with no unions. Guidance lives in the
prompts (`card-*.txt`), list-field descriptions and class docstrings, since Claude drops the description of a field
typed as one nested model. The class names label the usage log's `feature`.
"""

from typing import Literal

from pydantic import Field

from mealie.schema.openai._base import OpenAIBase


class OpenAIRecipeCardUnsure(OpenAIBase):
    """Something on the card you could read but aren't sure of"""

    text: str = Field(..., description="The uncertain words or numbers, exactly as written in `content`.")
    alternatives: list[str] = Field(
        default_factory=list,
        description="Other ways the same words or numbers could be read, most likely first, e.g. '1/2' for '1/4'.",
    )
    reason: Literal["faded", "ambiguous", "cut_off", "smudged", "other"]


class OpenAIRecipeCardTranscription(OpenAIBase):
    contains_recipe: bool = Field(
        ...,
        description="Whether the card holds a recipe at all. Set to false if there is nothing to transcribe.",
    )
    content: str = Field(
        ...,
        description=(
            "Everything written on the card, transcribed exactly as markdown: the title, every ingredient line and "
            "every step as written, with [illegible] for writing you can't read and [blank] for a gap the writer "
            "left on purpose."
        ),
    )
    language: str | None = Field(
        None,
        description="The language the card is written in, e.g., 'English' or 'French'.",
    )
    attribution: str | None = Field(
        None,
        description="Who the recipe is from, exactly as written on the card (e.g. 'From Grandma Jo'), if it says.",
    )
    unsure: list[OpenAIRecipeCardUnsure] = Field(
        default_factory=list,
        description=(
            "Every word or number in `content` you could read but aren't sure of, with the other ways it could be "
            "read. Leave it empty when you're sure of everything."
        ),
    )


class OpenAIRecipeCardTranscript(OpenAIBase):
    """Everything written on the card as plain text, one physical line of writing per line"""

    contains_recipe: bool
    text: str


class OpenAIRecipeCardRegion(OpenAIBase):
    """The writing in one cropped area of a recipe card, transcribed exactly"""

    readable: bool
    text: str
    alternatives: list[str] = Field(
        default_factory=list,
        description="Other ways the writing could be read, most likely first. Leave empty when you're sure.",
    )
