"""
Fork: recipe card jobs and batches as the API returns them (docs/ai/PHASE2.md §14).

No response model has a top-level `message` field: the frontend's axios interceptor toasts any `message` it sees.
"""

from datetime import datetime
from typing import Any

from pydantic import UUID4, Field

from mealie.schema._mealie import MealieModel
from mealie.schema.response.pagination import PaginationBase

from .ingest_draft import CardDraft
from .ingest_enums import (
    IngestErrorCode,
    IngestRejectReason,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskMode,
    IngestTaskState,
)
from .ingest_extraction import CardReadInfo
from .ingest_flags import CardFlag, CardProposal
from .ingest_pages import PageOut


class RecipeIngestionJobTask(MealieModel):
    """A job's pending task"""

    kind: IngestTaskKind
    state: IngestTaskState
    mode: IngestTaskMode | None = None
    """
    What an `extract` task does (its `task_payload`): read the whole card (`reextract`, also a first read or a retry),
    build the recipe again from the corrected text (`rebuild`), or parse chosen lines (`parse_lines`); None for a
    region re-read
    """
    refs: list[str] = Field(default_factory=list)
    """The ingredient lines (`referenceId`s) a `parse_lines` task parses"""
    progress_key: str | None = None
    """A translation key, e.g. `recipe-ingest.progress.reading-card`"""
    cancel_requested: bool = False


class RecipeIngestionJobError(MealieModel):
    """Why the last task or commit failed; the frontend translates `recipe-ingest.error.<code>` with the params"""

    code: IngestErrorCode
    params: dict[str, Any] = Field(default_factory=dict)


class RecipeIngestionRecipeRef(MealieModel):
    """A recipe a job is linked to"""

    id: UUID4
    slug: str | None = None
    name: str | None = None


class RecipeIngestionJobRef(MealieModel):
    """Another card of the household"""

    id: UUID4
    title: str | None = None


class RecipeIngestionJobSummary(MealieModel):
    id: UUID4
    batch_id: UUID4
    position: int
    status: IngestStatus
    source: IngestSource
    source_name: str | None = None
    title: str | None = None
    page_count: int
    thumb_url: str | None = None
    """The front page's thumbnail"""
    draft_version: int
    """What a save or commit of the draft names; a newer one means it changed (409 `version_conflict`)"""
    error_count: int = 0
    warning_count: int = 0
    """Unresolved errors and warnings"""
    task: RecipeIngestionJobTask | None = None
    error: RecipeIngestionJobError | None = None
    recipe: RecipeIngestionRecipeRef | None = None
    """The committed recipe"""
    local_only: bool = False
    """
    Only local providers read the card: it was sent so, or its group keeps cards local now and the card can still be
    read again (it isn't committed)
    """
    can_discard: bool = False
    """
    The user may discard the card (§9): its uploader, any household member for a card from the inbox or sent with an
    API token, otherwise household managers
    """
    created_at: datetime | None = None
    committed_at: datetime | None = None
    """When the card was added as a recipe"""
    auto_retry_at: datetime | None = None
    """
    A card that failed because every provider was over its monthly limit is read again automatically by then (UTC,
    when the month's limit resets), or within about 10 minutes after the limit is raised
    """
    expires_at: datetime | None = None
    """A failed card is removed then, with its photos (UTC)"""
    household_recipes_public: bool = False
    """
    New recipes in the household can be seen without a login now (not a private household, and recipes created
    public), so a card photo on one would be too: what the review page warns about. The photo switches' defaults follow
    whether new recipes are created public (`card_photo_default`, `card_cover_default`)
    """


class RecipeIngestionJobPagination(PaginationBase):
    items: list[RecipeIngestionJobSummary]


class RecipeIngestionJobPermissions(MealieModel):
    can_create_foods: bool = False
    """Commit creates foods that don't exist yet; otherwise their names are kept as text"""
    can_create_organizers: bool = False
    """Commit creates tags, categories and tools added by name; otherwise they are left out with a warning"""
    can_discard: bool = False
    can_export_eval: bool = False
    """May save the card as an eval case (group managers)"""
    can_read_with_cloud: bool = False
    """
    May have this failed local-only card read by any of the group's providers: its uploader or a household manager,
    while the group doesn't keep cards local
    """
    can_uncommit: bool = False
    """May delete the recipe this card became and bring the card back for review (its committer or a manager)"""
    can_merge: bool = False
    """May add this card's photos to another card as its back (both uploaded by the user, or a household manager)"""


class RecipeIngestionJobOut(RecipeIngestionJobSummary):
    pages: list[PageOut] = Field(default_factory=list)
    transcription: str | None = None
    read: CardReadInfo | None = None
    draft: CardDraft | None = None
    flags: list[CardFlag] = Field(default_factory=list)
    proposals: list[CardProposal] = Field(default_factory=list)
    permissions: RecipeIngestionJobPermissions = Field(default_factory=RecipeIngestionJobPermissions)
    duplicate_of: RecipeIngestionRecipeRef | None = None
    """
    A group recipe with the draft's name (its slug is taken, so committing names the recipe `duplicate_name`), else
    the household's recipe whose name is most like it ("Bananna Bread" for "Banana Bread")
    """
    duplicate_job: RecipeIngestionJobRef | None = None
    """Another card of the household, waiting or being read, with the same name"""
    duplicate_name: str | None = None
    """
    The name committing gives the recipe while `duplicate_of` has the draft's name: the first free "Name (n)", e.g.
    "Banana Bread (2)"; None when the name is only similar, or no free one is left
    """
    card_photo_default: bool = True
    """
    Whether the card's photos are attached to the recipe when the draft doesn't say (not where the household's new
    recipes are created public)
    """
    card_cover_default: bool = True
    """
    Whether the front of the card becomes the recipe's image when the draft doesn't say (not where the household's new
    recipes are created public)
    """


class RecipeIngestionJobState(MealieModel):
    """What the review page polls while a task runs"""

    draft_version: int
    status: IngestStatus
    task: RecipeIngestionJobTask | None = None
    proposal_ids: list[UUID4] = Field(default_factory=list)
    error: RecipeIngestionJobError | None = None


class RecipeIngestionJobCounts(MealieModel):
    processing: int = 0
    ready: int = 0
    needs_attention: int = 0
    """Ready cards with an unresolved error or warning"""
    failed: int = 0
    """Cards that couldn't be read, not counting those waiting for a monthly limit"""
    waiting: int = 0
    """
    Cards waiting for a monthly limit: they failed `limit_reached` and are read again automatically once it resets or
    is raised (`auto_retry_at`)
    """


class RecipeIngestionBatchJob(MealieModel):
    id: UUID4
    position: int
    status: IngestStatus
    error_count: int = 0
    warning_count: int = 0


class RecipeIngestionBatchOut(MealieModel):
    id: UUID4
    source: IngestSource
    created_at: datetime | None = None
    last_upload_at: datetime | None = None
    sealed_at: datetime | None = None
    notified_at: datetime | None = None
    counts: RecipeIngestionJobCounts = Field(default_factory=RecipeIngestionJobCounts)
    jobs: list[RecipeIngestionBatchJob] = Field(default_factory=list)
    """In review order: `position`, then arrival"""


class IngestedJob(MealieModel):
    id: UUID4
    status: IngestStatus
    page_count: int
    review_path: str
    """The review page, e.g. `/g/home/recipes/cards/<id>`"""


class IngestRejected(MealieModel):
    index: int
    """The image's position in the request"""
    filename: str | None = None
    reason: IngestRejectReason
    duplicate_of: UUID4 | None = None
    """The earlier job holding the same card"""


class IngestResponse(MealieModel):
    """`POST /api/ai/ingest`'s answer (202, or 400 inside `detail` when nothing was accepted)"""

    batch_id: UUID4 | None = None
    jobs: list[IngestedJob] = Field(default_factory=list)
    rejected: list[IngestRejected] = Field(default_factory=list)
    summary: str
    """A sentence in the request's language, for a Shortcut's notification"""
