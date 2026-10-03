"""Fork: a group's recipe card settings, notifier toggles and `/api/ai/about` (docs/ai/PHASE2.md §10, §14)"""

from pydantic import ConfigDict, Field

from mealie.schema._mealie import MealieModel

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


class IngestInboxInfo(MealieModel):
    enabled: bool = False
    folder: str | None = None
    """This household's folder, relative to the inbox share: `<group-slug>/<household-slug>`"""


class RecipeIngestionSettingsOut(MealieModel):
    local_only: bool = False
    cross_read: bool = False
    can_read_cards: bool = False
    """A default provider, plus an image provider or OCR"""
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


class IngestAboutFeatures(MealieModel):
    ingest: IngestAboutFeature
    mcp: bool = True


class IngestAbout(MealieModel):
    """`GET /api/ai/about` (public): what this server's AI features accept, for Shortcuts and integrations"""

    version: str
    features: IngestAboutFeatures
