"""Fork: request bodies of the recipe card review, commit and eval-case routes (docs/ai/PHASE2.md §14)"""

from datetime import datetime
from typing import Literal, Self

from pydantic import UUID4, ConfigDict, Field, model_validator

from mealie.schema._mealie import MealieModel

from .ingest_draft import CardDraft
from .ingest_enums import CardFlagSeverity, FlagResolution
from .ingest_flags import CardFlag, ProposalTarget

MAX_DRAFT_ITEMS = 500
"""The most ingredients, steps, notes or organizers a saved draft may hold"""

MIN_REGION_SIDE = 0.02
"""A re-read region's smallest side, as a fraction of the page"""

EVAL_CASE_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"

_STRICT = ConfigDict(extra="forbid")


class CardDraftUpdate(MealieModel):
    """An autosave from the review page. A stale `draft_version` is refused with 409 `version_conflict`."""

    draft_version: int
    draft: CardDraft
    flag_resolutions: dict[str, FlagResolution | None] = Field(default_factory=dict)
    """By flag id; `None` takes a resolution back"""
    resolved_proposal_ids: list[UUID4] = Field(default_factory=list)
    """Proposals used or dismissed, which are removed"""
    clear_error: bool = False
    """Dismisses the banner of a failed re-read or re-extract"""

    model_config = _STRICT

    @model_validator(mode="after")
    def _check_sizes(self) -> Self:
        # counted here rather than with `Field` limits, which would make the generated TypeScript a union of tuples
        draft = self.draft
        for items in (draft.ingredients, draft.steps, draft.notes, draft.tags, draft.categories, draft.tools):
            if len(items) > MAX_DRAFT_ITEMS:
                raise ValueError(f"A draft can hold at most {MAX_DRAFT_ITEMS} of each kind of item")
        return self


class CardDraftSaved(MealieModel):
    """The answer to a saved draft: its new version and the flags computed for it"""

    draft_version: int
    flags: list[CardFlag] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0


class RereadRequest(MealieModel):
    """A region of an upright page, in fractions of its width and height, to read again for one field"""

    page: int = Field(ge=0)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=MIN_REGION_SIDE, le=1)
    height: float = Field(ge=MIN_REGION_SIDE, le=1)
    target: ProposalTarget

    model_config = _STRICT

    @model_validator(mode="after")
    def _inside_the_page(self) -> Self:
        # a little slack for the cropper's floating point
        if self.x + self.width > 1.0001 or self.y + self.height > 1.0001:
            raise ValueError("The region must lie inside the page")
        return self


class RotateRequest(MealieModel):
    degrees: Literal[90, 180, 270]
    """Clockwise"""

    model_config = _STRICT


class CommitRequest(MealieModel):
    draft_version: int
    draft: CardDraft | None = None
    """The final draft, saved first (with the version check) when included"""

    model_config = _STRICT


class CommitOut(MealieModel):
    recipe_id: UUID4
    slug: str
    next_job_id: UUID4 | None = None
    """The batch's next ready card, for Commit & next"""
    warnings: list[str] = Field(default_factory=list)


class UnresolvedFlagsDetail(MealieModel):
    """The `detail` of a 422 from commit while errors remain"""

    code: Literal["unresolved_flags"] = "unresolved_flags"
    flags: list[CardFlag] = Field(default_factory=list)

    @classmethod
    def of(cls, flags: list[CardFlag]) -> UnresolvedFlagsDetail:
        return cls(flags=[f for f in flags if f.severity == CardFlagSeverity.error and f.resolution is None])


class EvalCaseRequest(MealieModel):
    slug: str = Field(pattern=EVAL_CASE_SLUG_PATTERN)
    verified: bool = False
    """The reviewer checked the draft against the card"""

    model_config = _STRICT


class EvalCaseOut(MealieModel):
    slug: str
    files: list[str] = Field(default_factory=list)


class EvalCaseSummary(MealieModel):
    slug: str
    name: str | None = None
    page_count: int = 0
    verified: bool = False
    created_at: datetime | None = None
    """When the case was exported"""
