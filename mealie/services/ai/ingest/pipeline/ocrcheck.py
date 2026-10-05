"""
The OCR check's second reading of a printed card (docs/ai/PHASE2.md §4.5): Tesseract read the card's pages when
orientation did (`PageOCR`), and `flags.compute_flags` compares the draft's numbers with that reading. A number that
differs only by digits Tesseract confuses ("7" for an italic "1") is read again, its line alone, cropped from the
stored page by its box (`PageOCR.lines`) and scaled to a size Tesseract reads such digits right (`ocr.read_line`).
"""

import functools
from collections.abc import Callable, Sequence
from pathlib import Path

from mealie.schema.recipe_ingest import OCRLine
from mealie.services import ocr

from .models import CardPage


def ocr_line_reader(pages: Sequence[CardPage]) -> Callable[[int], str | None] | None:
    """
    Tesseract's second reading of one of the OCR check's lines (`flags.ocr_check_lines` of these pages, in order), by
    its index: the line read again from its page's `page.jpg` (`ocr.read_line`), once; None for a line without a box
    (a page read before boxes were kept, or with more lines than were kept). None when Tesseract isn't installed.
    Blocking (Tesseract); each line is read only when the check asks for it, which is rare.
    """
    if not ocr.binary_available():
        return None

    boxes: list[tuple[Path, OCRLine] | None] = []
    for page in pages:
        page_ocr = page.meta.ocr
        if page_ocr is None:
            continue
        texts = [line.strip() for line in page_ocr.text.splitlines() if line.strip()]
        lines = page_ocr.lines
        same = len(lines) == len(texts) and all(
            line.text.strip() == text for line, text in zip(lines, texts, strict=True)
        )
        boxes += [(page.page_path, line) for line in lines] if same else [None] * len(texts)

    @functools.cache
    def read(index: int) -> str | None:
        found = boxes[index] if 0 <= index < len(boxes) else None
        if found is None:
            return None
        path, line = found
        return ocr.read_line(path, line.x, line.y, line.width, line.height)

    return read
