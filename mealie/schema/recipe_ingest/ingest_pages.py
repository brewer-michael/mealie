"""Fork: a recipe card's normalized pages (docs/ai/PHASE2.md §2)"""

from pydantic import UUID4, ConfigDict

from mealie.schema._mealie import MealieModel

from .ingest_enums import PageRotationSource

PAGE_IMAGE_KINDS = ("page", "view", "thumb")
"""The files of a page: `page.jpg` (long side at most 4096), `view.jpg` (2048) and `thumb.webp` (480)"""


class PageOCR(MealieModel):
    """What Tesseract read on a page while orienting it"""

    text: str = ""
    confidence: float = 0.0
    """Mean word confidence, from 0 to 100"""


class PageMeta(MealieModel):
    """One page of a card, as stored in the job's `pages` column"""

    index: int
    """0 for the front; the page's files are in `pages/<index>/`"""
    width: int
    height: int
    """`page.jpg`'s size"""
    view_width: int
    view_height: int
    """`view.jpg`'s size: both the model and the review page see this image, so crop fractions line up"""
    rotation: int = 0
    """Degrees the page was turned clockwise after EXIF transpose (0, 90, 180 or 270)"""
    rotation_source: PageRotationSource = PageRotationSource.none
    oriented: bool = False
    """Whether the page's orientation has been settled (by Tesseract or by hand), so it isn't probed again"""
    raw_sha256: str
    """SHA-256 of the uploaded bytes"""
    page_sha256: str
    """SHA-256 of `page.jpg`; changes whenever the page is rewritten"""
    original_filename: str | None = None
    """Sanitized; display only"""
    format: str
    """The uploaded image's format, e.g. `jpeg`, `mpo`, `heif`"""
    raw_bytes: int
    ocr: PageOCR | None = None

    model_config = ConfigDict(extra="ignore")


class PageOut(MealieModel):
    """A page as the review page shows it"""

    index: int
    width: int
    height: int
    view_width: int
    view_height: int
    rotation: int
    rotation_source: PageRotationSource
    oriented: bool
    original_filename: str | None = None
    page_url: str
    view_url: str
    thumb_url: str

    @classmethod
    def from_meta(cls, job_id: UUID4, meta: PageMeta) -> PageOut:
        """
        The page with the URLs of its images. Each URL carries the start of `page_sha256`, so a rotated page is
        never shown from the browser's cache.
        """
        version = meta.page_sha256[:12]
        base = f"/api/ai/ingest/jobs/{job_id}/pages/{meta.index}"
        return cls(
            index=meta.index,
            width=meta.width,
            height=meta.height,
            view_width=meta.view_width,
            view_height=meta.view_height,
            rotation=meta.rotation,
            rotation_source=meta.rotation_source,
            oriented=meta.oriented,
            original_filename=meta.original_filename,
            page_url=f"{base}/page?v={version}",
            view_url=f"{base}/view?v={version}",
            thumb_url=f"{base}/thumb?v={version}",
        )
