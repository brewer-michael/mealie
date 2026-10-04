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
    IngestTaskState,
)
from .ingest_extraction import CardReadInfo
from .ingest_flags import CardFlag, CardProposal
from .ingest_pages import PageOut


class RecipeIngestionJobTask(MealieModel):
    """A job's pending task"""

    kind: IngestTaskKind
    state: IngestTaskState
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
    """The user may discard the card (§9): its uploader, anyone for an inbox card, otherwise household managers"""
    created_at: datetime | None = None


class RecipeIngestionJobPagination(PaginationBase):
    items: list[RecipeIngestionJobSummary]


class RecipeIngestionJobPermissions(MealieModel):
    can_create_foods: bool = False
    """Commit creates foods that don't exist yet; otherwise their names are kept as text"""
    can_discard: bool = False
    can_export_eval: bool = False
    """May save the card as an eval case (group managers)"""


class RecipeIngestionJobOut(RecipeIngestionJobSummary):
    draft_version: int
    pages: list[PageOut] = Field(default_factory=list)
    transcription: str | None = None
    read: CardReadInfo | None = None
    draft: CardDraft | None = None
    flags: list[CardFlag] = Field(default_factory=list)
    proposals: list[CardProposal] = Field(default_factory=list)
    permissions: RecipeIngestionJobPermissions = Field(default_factory=RecipeIngestionJobPermissions)
    duplicate_of: RecipeIngestionRecipeRef | None = None
    """A group recipe whose slug matches the draft's name: committing would make "Name (1)\""""
    household_recipes_public: bool = False
    """New recipes in the household are public, so the card photo would be too"""


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
