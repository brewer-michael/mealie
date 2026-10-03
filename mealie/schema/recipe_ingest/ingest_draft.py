"""
Fork: the editable draft of a recipe card (docs/ai/PHASE2.md §13).

`CardDraft` is the fork's own narrow model, not an upstream `Recipe`: drafts outlive upstream syncs, so they're read
leniently (unknown fields are ignored, and `schema_version` says how to migrate older ones) and only turned into a
`Recipe` at commit. Ignoring unknown fields also makes the model the whitelist of what a review can change: there is
no id, slug, assets, settings, rating or extras on it.
"""

from typing import Any
from uuid import uuid4

from pydantic import UUID4, ConfigDict, Field, model_validator

from mealie.schema._mealie import MealieModel

CARD_DRAFT_SCHEMA_VERSION = 1
"""The current `CardDraft.schema_version`"""

_LENIENT = ConfigDict(extra="ignore")


class CardDraftRef(MealieModel):
    """A food, unit, tag, category or tool: linked by id when it exists in the group, else by name only"""

    id: UUID4 | None = None
    name: str = ""

    model_config = _LENIENT


class CardDraftIngredient(MealieModel):
    reference_id: UUID4 = Field(default_factory=uuid4)
    """Server-made and stable; flags are keyed to it, never to the line's index"""
    title: str | None = None
    """A section title shown above this line"""
    original_text: str = ""
    """The line exactly as read from the card"""
    quantity: float | None = None
    unit: CardDraftRef | None = None
    food: CardDraftRef | None = None
    note: str = ""
    display: str = ""
    parse_confidence: float | None = None
    """The NLP parser's average confidence, when the line was parsed"""
    extracted_hash: str | None = None
    """A hash of the parsed fields as extracted; parse flags drop off once the line no longer matches it"""

    model_config = _LENIENT


class CardDraftStep(MealieModel):
    id: UUID4 = Field(default_factory=uuid4)
    """Server-made and stable; flags are keyed to it"""
    title: str | None = None
    text: str = ""

    model_config = _LENIENT


class CardDraftNote(MealieModel):
    title: str = ""
    text: str = ""

    model_config = _LENIENT


class CardDraft(MealieModel):
    """What the review page edits, and what commit turns into a recipe"""

    schema_version: int = CARD_DRAFT_SCHEMA_VERSION
    name: str = ""
    description: str = ""
    recipe_yield: str | None = None
    recipe_yield_quantity: float | None = None
    recipe_servings: float | None = None
    prep_time: str | None = None
    perform_time: str | None = None
    total_time: str | None = None
    attribution: str | None = None
    """Who the recipe is from, exactly as on the card ("From Grandma Jo"); becomes a note titled "From" at commit"""
    use_card_as_cover: bool = True
    """Whether commit makes the front of the card the recipe's image"""
    ingredients: list[CardDraftIngredient] = Field(default_factory=list)
    steps: list[CardDraftStep] = Field(default_factory=list)
    notes: list[CardDraftNote] = Field(default_factory=list)
    tags: list[CardDraftRef] = Field(default_factory=list)
    categories: list[CardDraftRef] = Field(default_factory=list)
    tools: list[CardDraftRef] = Field(default_factory=list)
    """Suggestions matching the group's existing organizers; commit never creates any"""

    model_config = _LENIENT

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, data: Any) -> Any:
        """
        Brings a stored draft up to `CARD_DRAFT_SCHEMA_VERSION`. Version 1 is the first; later versions add their
        steps here, keyed by the stored `schema_version`. A draft from a newer version is read as it is.
        """
        if isinstance(data, dict):
            version = data.get("schema_version", data.get("schemaVersion"))
            if not isinstance(version, int) or version < 1:
                data = {**data, "schema_version": CARD_DRAFT_SCHEMA_VERSION}
                data.pop("schemaVersion", None)
        return data
