"""Fork: request bodies of the recipe card review, commit and eval-case routes (docs/ai/PHASE2.md §14)"""

from datetime import datetime
from typing import Literal, Self

from pydantic import UUID4, ConfigDict, Field, model_validator

from mealie.schema._mealie import MealieModel

from .ingest_draft import CardDraft, CardDraftIngredient
from .ingest_enums import CardFlagSeverity, EvalCaseTag, FlagResolution, RegionHintSource
from .ingest_flags import CardFlag, ProposalTarget
from .ingest_jobs import RecipeIngestionJobRef, RecipeIngestionRecipeRef

MAX_DRAFT_ITEMS = 500
"""The most ingredients, steps, notes or organizers a saved draft may hold"""

MIN_REGION_SIDE = 0.02
"""A re-read region's smallest side, as a fraction of the page"""

EVAL_CASE_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"

MAX_EVAL_NOTES = 2000
"""The longest eval case notes"""

MAX_TRANSCRIPTION = 20_000
"""The longest transcription a rebuild takes"""

MAX_PARSE_LINES = 50
"""The most ingredient lines one AI parse takes"""

MAX_BULK_COMMIT = 100
"""The most cards one bulk commit takes"""

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
    """The answer to a saved draft: its new version, the flags computed for it, and what the save changed"""

    draft_version: int
    flags: list[CardFlag] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    ingredients: list[CardDraftIngredient] | None = None
    """The lines this save parsed (a filled blank, an edited or new line), as stored; None when it parsed none"""
    duplicate_of: RecipeIngestionRecipeRef | None = None
    """As on the job, for the saved name"""
    duplicate_job: RecipeIngestionJobRef | None = None
    duplicate_name: str | None = None


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


class UncommitRequest(MealieModel):
    force: bool = False
    """Delete the recipe even though it was edited after the card was added"""

    model_config = _STRICT


class MergeRequest(MealieModel):
    into_job_id: UUID4
    """The card this one's photos are added to, as its next pages"""

    model_config = _STRICT


class RebuildRequest(MealieModel):
    transcription: str = Field(min_length=1, max_length=MAX_TRANSCRIPTION)
    """The card's text as the reviewer corrected it"""

    model_config = _STRICT


class ParseLinesRequest(MealieModel):
    refs: list[UUID4]
    """The ingredient lines (`reference_id`s) to parse with the AI ingredient parser, 1 to 50"""

    model_config = _STRICT

    @model_validator(mode="after")
    def _check_size(self) -> Self:
        # counted here rather than with `Field` limits, which would make the generated TypeScript a union of tuples
        if not 1 <= len(self.refs) <= MAX_PARSE_LINES:
            raise ValueError(f"Choose 1 to {MAX_PARSE_LINES} lines")
        self.refs = list(dict.fromkeys(self.refs))
        return self


class RegionHintOut(MealieModel):
    """Where on an upright page a field's text probably is, in fractions of the page's width and height"""

    page: int
    x: float
    y: float
    width: float
    height: float
    source: RegionHintSource


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


class BulkCommitRequest(MealieModel):
    """The clean cards of a batch the review page listed, with the draft version it showed for each"""

    job_ids: list[UUID4]
    """1 to 100, committed in this order"""
    draft_versions: dict[UUID4, int] = Field(default_factory=dict)
    """Each listed card's `draft_version`; a card saved since is skipped (`version_conflict`)"""

    model_config = _STRICT

    @model_validator(mode="after")
    def _check_jobs(self) -> Self:
        if not 1 <= len(self.job_ids) <= MAX_BULK_COMMIT:
            raise ValueError(f"Choose 1 to {MAX_BULK_COMMIT} cards")
        self.job_ids = list(dict.fromkeys(self.job_ids))
        if missing := [job_id for job_id in self.job_ids if job_id not in self.draft_versions]:
            raise ValueError(f"No draft version for {len(missing)} of the cards")
        return self


class BulkCommitted(MealieModel):
    job_id: UUID4
    recipe_id: UUID4
    slug: str


class BulkCommitSkipped(MealieModel):
    job_id: UUID4
    code: str
    """
    Why it was left for review: `not_clean` (a highlighted problem), `version_conflict`, `invalid_status`,
    `not_found`, `unresolved_flags`, `commit_invalid`, `commit_interrupted`, `paused_for_restore` or `internal_error`
    """


class BulkCommitOut(MealieModel):
    committed: list[BulkCommitted] = Field(default_factory=list)
    skipped: list[BulkCommitSkipped] = Field(default_factory=list)


def _unique_tags(tags: list[EvalCaseTag]) -> list[EvalCaseTag]:
    return list(dict.fromkeys(tags))


class EvalCaseRequest(MealieModel):
    slug: str = Field(pattern=EVAL_CASE_SLUG_PATTERN)
    verified: bool = False
    """The reviewer checked the draft against the card"""
    tags: list[EvalCaseTag] = Field(default_factory=list)
    """What the card is like; `sideways`, `two-sided` and `blank` are added from the card itself"""
    notes: str = Field("", max_length=MAX_EVAL_NOTES)

    model_config = _STRICT

    @model_validator(mode="after")
    def _dedupe(self) -> Self:
        self.tags = _unique_tags(self.tags)
        return self


class EvalCaseUpdate(MealieModel):
    """A change to a saved eval case; a field left out keeps its value"""

    verified: bool | None = None
    tags: list[EvalCaseTag] | None = None
    """Replaces the reviewer's tags; the ones found from the card stay"""
    notes: str | None = Field(None, max_length=MAX_EVAL_NOTES)

    model_config = _STRICT

    @model_validator(mode="after")
    def _dedupe(self) -> Self:
        if self.tags is not None:
            self.tags = _unique_tags(self.tags)
        return self


class EvalCaseOut(MealieModel):
    slug: str
    files: list[str] = Field(default_factory=list)


class EvalCaseSummary(MealieModel):
    slug: str
    name: str | None = None
    page_count: int = 0
    verified: bool = False
    tags: list[str] = Field(default_factory=list)
    """Every tag in the fixture: the reviewer's (`EvalCaseTag`) and the ones found from the card"""
    notes: str = ""
    created_at: datetime | None = None
    """When the case was exported"""
