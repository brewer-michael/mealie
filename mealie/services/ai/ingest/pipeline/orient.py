"""
Turning a card page upright (docs/ai/PHASE2.md §4.1 step 0, §4.4).

Phones held flat over a table record the wrong EXIF orientation (F7), so intake's transpose can leave a card sideways.
Tesseract reads the page every way up, and the page turns only when the best reading scores at least
`ORIENT_MIN_RATIO` times the upright one: handwriting scores low every way, and a wrong turn makes every later read
worse. The same run's text and confidence are kept (`PageMeta.ocr`) for the OCR fallback, read at the chosen
rotation, so they match the stored page, with the box of each line it found (`PageOCR.lines`), from which a re-read's
selection starts at the line it's about (`regions.region_hint`).

`decide_orientation` only decides: it writes no file, so the runner can stage the turned files, store their metadata
and swap them in, and a crash between those steps is recovered (`images.recover_staged`). `orient_page` decides and
turns the files at once, for callers that work on copies (the eval) or hold no stored metadata.

Orientation has its own switch, `AI_INGEST_ORIENT` (`orientation_available`): it needs only the `tesseract` command,
so `OCR_ENABLED=false` turns off the OCR fallback reader but not this, and the text read here is still stored.
"""

from dataclasses import dataclass

from mealie.schema.recipe_ingest import OCRLine, PageMeta, PageOCR, PageRotationSource
from mealie.services import ocr

from .. import images, limits, storage
from ..settings import get_ingest_settings
from .models import CardPage

MAX_OCR_LINES = 300
"""At most this many of the lines Tesseract found are kept with a page (a card has far fewer)"""


def orientation_available() -> bool:
    """Whether pages are turned upright automatically: `AI_INGEST_ORIENT` is on and Tesseract is installed"""
    return get_ingest_settings().ORIENT and ocr.binary_available()


@dataclass(frozen=True)
class OrientDecision:
    """What Tesseract decided for a page"""

    rotation: int
    """Degrees to turn the page clockwise (0, 90, 180 or 270); 0 when it stays as it is"""
    ocr: PageOCR | None
    """What Tesseract read, at that rotation (so it matches the page once turned); None when it didn't run or failed"""
    settled: bool
    """
    Whether the page's orientation is now settled: false without Tesseract (the review page offers Rotate) or when it
    timed out or failed (the next extraction tries again, and the OCR fallback reads the page itself)
    """


def decide_orientation(page: CardPage) -> OrientDecision:
    """
    Whether and how far a page must turn to be upright, when orientation is on (`orientation_available`) and
    Tesseract is sure enough (`ORIENT_MIN_RATIO`), and the text it read. Writes nothing. A page already oriented is
    settled as it is. Blocking (Tesseract); run it in a thread from async code.

    Raises `FileNotFoundError` when the page is gone.
    """
    if page.meta.oriented:
        return OrientDecision(rotation=0, ocr=page.meta.ocr, settled=True)
    if not orientation_available():
        return OrientDecision(rotation=0, ocr=None, settled=False)
    if not page.page_path.is_file():
        raise FileNotFoundError(page.page_path)

    # its own switch: read whenever Tesseract is installed, whatever `OCR_ENABLED` says about the OCR fallback
    result = ocr.extract_text(page.page_path, min_ratio=limits.ORIENT_MIN_RATIO, require_enabled=False)
    if result.failed:
        return OrientDecision(rotation=0, ocr=None, settled=False)
    lines = [
        OCRLine(text=line.text, x=line.x, y=line.y, width=line.width, height=line.height)
        for line in result.lines[:MAX_OCR_LINES]
    ]
    return OrientDecision(
        rotation=result.rotation % 360,
        ocr=PageOCR(text=result.text, confidence=result.confidence, lines=lines),
        settled=True,
    )


def oriented_meta(meta: PageMeta, decision: OrientDecision) -> PageMeta:
    """
    A page's metadata once `decision` is applied without turning it (rotation 0): settled, with the OCR text. A turn's
    metadata comes from the files it writes (`images.rotate_page_files`, or the staged rotation the runner stores).
    """
    if not decision.settled:
        return meta
    return meta.model_copy(update={"oriented": True, "ocr": decision.ocr})


def orient_page(page: CardPage) -> PageMeta:
    """
    `decide_orientation`, then the page's files turned at once (inside the ingest write lock) when it says so.
    Returns the page's new metadata, with `oriented` set; unchanged when nothing was settled. For callers that work on
    copies, such as the eval: the runner stages the turn instead, so a crash can't split the files from their metadata.

    Raises `IngestPaused` when a backup restore holds the write lock, and `FileNotFoundError` when the page is gone.
    """
    decision = decide_orientation(page)
    if not decision.settled or page.meta.oriented:
        return page.meta
    if not decision.rotation:
        return oriented_meta(page.meta, decision)

    with storage.ingest_write():
        meta = images.rotate_page_files(page.dir, page.meta, decision.rotation, PageRotationSource.ocr)
    return meta.model_copy(update={"ocr": decision.ocr})
