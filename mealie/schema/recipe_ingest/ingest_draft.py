"""
Fork: the editable draft of a recipe card (docs/ai/PHASE2.md §13).

`CardDraft` is the fork's own narrow model, not an upstream `Recipe`: drafts outlive upstream syncs, so they're read
leniently (unknown fields are ignored, and `schema_version` says how to migrate older ones) and only turned into a
`Recipe` at commit. Ignoring unknown fields also makes the model the whitelist of what a review can change: there is
no id, slug, assets, settings, rating or extras on it.
"""

from typing import Any
from uuid import UUID, uuid4, uuid5

from pydantic import UUID4, ConfigDict, Field, model_validator

from mealie.schema._mealie import MealieModel

CARD_DRAFT_SCHEMA_VERSION = 3
"""
The current `CardDraft.schema_version`. Version 2 gave notes an `id`; version 3 made `use_card_as_cover` optional (unset
is the household's default), which versions 1 and 2 set on every draft.
"""

COVER_CHOICE_SCHEMA_VERSION = 3
"""
The first `schema_version` whose `use_card_as_cover: true` is the reviewer's choice: earlier versions, and pages built
for them, set it on every draft
"""

NOTE_ID_NAMESPACE = UUID("6f1d2b8e-4c3a-4e57-9a0d-2b5c7e9f1a34")
"""The `uuid5` namespace of the ids a note sent or stored without one is given (`note_id_for`)"""

# NaN and infinity would reach the flag rules and the recipe as numbers; a save with one gets a 422
_LENIENT = ConfigDict(extra="ignore", allow_inf_nan=False)


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
    id: UUID4 = Field(default_factory=uuid4)
    """Server-made and stable; flags are keyed to it. A note without one gets `note_id_for` its place and text."""
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
    use_card_as_cover: bool | None = None
    """
    Whether commit makes the front of the card the recipe's image; None means the household's default (the card is the
    image unless the household's new recipes are created public: the image is served without a login, like the assets)
    """
    attach_card_photo: bool | None = None
    """
    Whether commit attaches the card's photos to the recipe as assets; None means the household's default (attached
    unless the household's new recipes are created public)
    """
    ingredients: list[CardDraftIngredient] = Field(default_factory=list)
    steps: list[CardDraftStep] = Field(default_factory=list)
    notes: list[CardDraftNote] = Field(default_factory=list)
    tags: list[CardDraftRef] = Field(default_factory=list)
    categories: list[CardDraftRef] = Field(default_factory=list)
    tools: list[CardDraftRef] = Field(default_factory=list)
    """
    Tags, categories and tools: suggestions matching the group's existing organizers by id, or names the reviewer
    added, which commit creates when the committer can organize
    """

    model_config = _LENIENT

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, data: Any) -> Any:
        """
        Brings a stored or sent draft up to `CARD_DRAFT_SCHEMA_VERSION`, keyed by its `schema_version`; a draft from a
        newer version is read as it is.

        Version 2 gave notes an `id`. A note without one (stored by version 1, or new on a page that sent none) gets
        `note_id_for` its position and text, so reading the same draft twice gives the same ids until it's saved.

        Version 3 made `use_card_as_cover` optional. Versions 1 and 2 stored `true` on every draft (their default), so
        a `true` from them (or from a draft without a version, read as version 1) says nothing: it reads as unset, the
        household's default (`review.uses_card_as_cover`), which keeps the card off recipes created public. `false`
        was always the reviewer's choice. `attach_card_photo` was optional from the start.
        """
        if not isinstance(data, dict):
            return data
        version = data.get("schema_version", data.get("schemaVersion"))
        if not isinstance(version, int) or version < CARD_DRAFT_SCHEMA_VERSION:
            data = {**data, "schema_version": CARD_DRAFT_SCHEMA_VERSION}
            data.pop("schemaVersion", None)
        if not isinstance(version, int) or version < COVER_CHOICE_SCHEMA_VERSION:
            for key in ("use_card_as_cover", "useCardAsCover"):
                if data.get(key) is True:
                    data[key] = None

        notes = data.get("notes")
        if isinstance(notes, list) and any(isinstance(note, dict) and not note.get("id") for note in notes):
            data = {
                **data,
                "notes": [
                    {**note, "id": note_id_for(index, note)} if isinstance(note, dict) and not note.get("id") else note
                    for index, note in enumerate(notes)
                ],
            }
        return data


def note_id_for(index: int, note: dict[str, Any]) -> UUID:
    """
    The id of a note that has none: the same for the same position, title and text, so repeated reads of a stored
    draft agree. Shaped as a version 4 UUID, like every other draft id.
    """
    key = f"note:{index}:{note.get('title') or ''}:{note.get('text') or ''}"
    return UUID(bytes=uuid5(NOTE_ID_NAMESPACE, key).bytes, version=4)
