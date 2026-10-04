"""Fork: a group's recipe card settings, notifier toggles and `/api/ai/about` (docs/ai/PHASE2.md §10, §14)"""

from datetime import datetime

from pydantic import ConfigDict, Field

from mealie.schema._mealie import MealieModel

from .ingest_enums import InboxWaitingReason, IngestLimitedFeature, IngestRejectReason

_STRICT = ConfigDict(extra="forbid")


class RecipeIngestionSettingsUpdate(MealieModel):
    """What a group manager sets; also what's stored (no row means these defaults)"""

    local_only: bool = False
    """Keep every recipe card, and everything read from it, on this server's network"""
    cross_read: bool = False
    """Read every card a second time and flag where the readings disagree (costs a second image request)"""

    model_config = _STRICT


class ReaderInfo(MealieModel):
    """The first provider a card's read would use, for the privacy chip every member sees"""

    name: str
    """The provider's name"""
    local: bool
    """Runs on the group's own network"""
    via_ocr: bool = False
    """Tesseract reads the photo on this server, then this provider structures the text"""


class LocalReadiness(MealieModel):
    """For managers: what local-only cards could use, per slot"""

    image: list[str] = Field(default_factory=list)
    default: list[str] = Field(default_factory=list)
    fast: list[str] = Field(default_factory=list)
    """Names of the local providers each slot would try"""
    not_private: list[str] = Field(default_factory=list)
    """Providers marked as running locally whose address isn't private, which local-only cards won't use"""


class IngestLimits(MealieModel):
    max_upload_bytes: int
    max_file_bytes: int
    max_images_per_request: int
    max_pages_per_card: int
    max_pixels: int
    """The most pixels a PNG, WebP, HEIC, AVIF or TIFF page may have"""
    max_jpeg_pixels: int
    """The most pixels a JPEG may have (phone JPEGs are decoded at a reduced size, so they may be larger)"""


class IngestInboxRejection(MealieModel):
    """
    A photo the inbox couldn't add, kept in the household folder's `failed/`; or one it may not move, left where it is
    (`no_permission`)
    """

    name: str
    """The file's name"""
    reason: IngestRejectReason | None = None
    at: datetime | None = None
    """When it was refused (UTC)"""


class IngestInboxInfo(MealieModel):
    enabled: bool = False
    folder: str | None = None
    """This household's folder, relative to the inbox share: `<group-slug>/<household-slug>`"""
    waiting: int = 0
    """Photos in the folder that haven't been added yet (counting stops at 1000)"""
    waiting_reason: InboxWaitingReason | None = None
    """Why they wait, when they can't be added now"""
    rejections: list[IngestInboxRejection] = Field(default_factory=list)
    """The photos the inbox may not move (`no_permission`), then the newest it refused, newest first"""


class RecipeIngestionSettingsOut(MealieModel):
    enabled: bool = True
    """Recipe card scanning is on (`AI_INGEST_ENABLED`); when it's off, the server reads no cards"""
    local_only: bool = False
    cross_read: bool = False
    can_read_cards: bool = False
    """A default provider, plus an image provider or OCR"""
    limit_reached: bool = False
    """
    Cards can be uploaded, but every provider their reading would use under the group's policy is over its monthly
    token limit: a card read now fails `limit_reached`
    """
    limited_features: list[IngestLimitedFeature] = Field(default_factory=list)
    """Optional parts of reading a card that are skipped because their providers are over their monthly limit"""
    base_url_set: bool = False
    """`BASE_URL` is this server's address, so links in notifications work from a phone"""
    reader_running: bool = False
    """A card reader (the ingest worker) has run in the last 3 minutes; without one, cards wait"""
    ocr_available: bool = False
    reader: ReaderInfo | None = None
    """Under the group's policy"""
    local_only_available: bool = False
    """Whether a card can be read with local providers only"""
    local_readiness: LocalReadiness | None = None
    """Managers only"""
    limits: IngestLimits
    inbox: IngestInboxInfo = Field(default_factory=IngestInboxInfo)


class AINotifierEventsUpdate(MealieModel):
    recipe_ingestion_ready: bool = False
    """Send "recipe cards ready to review" through this notifier"""

    model_config = _STRICT


class AINotifierEventsOut(MealieModel):
    recipe_ingestion_ready: bool = False


class IngestAboutFeature(MealieModel):
    enabled: bool
    max_upload_bytes: int
    max_images_per_request: int
    max_pages_per_card: int
    inbox: bool
    worker: bool = False
    """A card reader (the ingest worker) has run in the last 3 minutes, so uploaded cards are read"""


class IngestAboutFeatures(MealieModel):
    ingest: IngestAboutFeature
    mcp: bool = True


class IngestAbout(MealieModel):
    """`GET /api/ai/about` (public): what this server's AI features accept, for Shortcuts and integrations"""

    version: str
    features: IngestAboutFeatures
