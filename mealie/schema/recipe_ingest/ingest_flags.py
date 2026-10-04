"""Fork: the flags that say what to check on a card, and the proposals re-reads make (docs/ai/PHASE2.md §4.6, §4.7)"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import UUID4, ConfigDict, Field

from mealie.schema._mealie import MealieModel

from .ingest_draft import CardDraft
from .ingest_enums import (
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    CardProposalKind,
    CardProposalOrigin,
    FlagResolution,
)


class CardFlag(MealieModel):
    """
    Something on the draft worth checking, with its stated cause. Keyed to a field plus the ingredient's
    `reference_id` or the step's `id`, never an index, under the stable id `"<kind>:<field>:<ref>"`.
    """

    id: str
    kind: CardFlagKind
    severity: CardFlagSeverity
    source: CardFlagSource
    field: str
    """The draft field: `name`, `description`, `ingredients`, `steps`, `notes`, a time or yield field, or `card`"""
    ref: str | None = None
    """The ingredient's `reference_id`, or the step's or note's `id`, for list fields"""
    params: dict[str, Any] = Field(default_factory=dict)
    """Values the flag's text shows, e.g. `value`, `token`, `suggestion`, `confidence`"""
    alternatives: list[str] = Field(default_factory=list)
    """Other readings the reviewer can apply with one tap"""
    resolution: FlagResolution | None = None

    model_config = ConfigDict(extra="ignore")


class ProposalTarget(MealieModel):
    """The draft field a re-read is for"""

    field: str
    """`name`, `description`, `ingredients`, `steps`, `notes`, `attribution`, or a time or yield field"""
    ref: str | None = None
    """The ingredient's `reference_id`, or the step's or note's `id`, for list fields"""

    model_config = ConfigDict(extra="ignore")


class CardProposal(MealieModel):
    """
    A reading the reviewer can use or dismiss: a region re-read for one field, or a whole new draft from a
    re-extract of an edited card. Accepting one is an ordinary edit.
    """

    id: UUID4 = Field(default_factory=uuid4)
    kind: CardProposalKind
    target: ProposalTarget | None = None
    """What a region re-read is for"""
    text: str | None = None
    """A region re-read's text, with `[illegible]` and `[blank]` markers as read"""
    readable: bool = True
    """False when nothing in the region could be read"""
    alternatives: list[str] = Field(default_factory=list)
    via_ocr: bool = False
    """Tesseract read the region, since the group has no image provider"""
    draft: CardDraft | None = None
    """A re-extract's whole new draft"""
    origin: CardProposalOrigin = CardProposalOrigin.reextract
    """What made a whole-card proposal: the card read again, or the recipe built again from the edited transcription"""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = ConfigDict(extra="ignore")
