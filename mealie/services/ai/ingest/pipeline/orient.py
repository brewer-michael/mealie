"""
Turning a card page upright (docs/ai/PHASE2.md §4.1 step 0, §4.4).

Phones held flat over a table record the wrong EXIF orientation (F7), so intake's transpose can leave a card sideways.
Tesseract reads the page every way up, and the page turns only when the best reading scores at least
`ORIENT_MIN_RATIO` times the upright one: handwriting scores low every way, and a wrong turn makes every later read
worse. The same run's text and confidence are kept (`PageMeta.ocr`) for the OCR fallback, read at the chosen
rotation, so they match the stored page.
"""

from mealie.schema.recipe_ingest import PageMeta, PageOCR, PageRotationSource
from mealie.services import ocr

from .. import images, limits, storage
from .models import CardPage


def orient_page(page: CardPage) -> PageMeta:
    """
    Turns a page upright when Tesseract is available and sure enough (`ORIENT_MIN_RATIO`), rewriting its files
    inside the ingest write lock, and records the OCR text it read. Returns the page's new metadata, with `oriented`
    set. Without Tesseract nothing is settled and the metadata comes back unchanged (the review page offers Rotate).
    Blocking (Tesseract); run it in a thread from async code.

    Raises `IngestPaused` when a backup restore holds the write lock, and `FileNotFoundError` when the page is gone.
    """
    if page.meta.oriented or not ocr.is_available():
        return page.meta

    if not page.page_path.is_file():
        raise FileNotFoundError(page.page_path)

    result = ocr.extract_text(page.page_path, min_ratio=limits.ORIENT_MIN_RATIO)
    if result.rotation:
        with storage.ingest_write():
            meta = images.rotate_page_files(page.dir, page.meta, result.rotation, PageRotationSource.ocr)
    else:
        meta = page.meta.model_copy(update={"oriented": True})

    return meta.model_copy(update={"ocr": PageOCR(text=result.text, confidence=result.confidence)})
