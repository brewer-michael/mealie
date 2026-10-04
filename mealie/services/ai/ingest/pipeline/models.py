"""
The recipe card pipeline's inputs, options and result (docs/ai/PHASE2.md §4.1). A module of their own, so the
pipeline's other modules can import them without importing the package's entry points.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from mealie.schema.recipe_ingest import CardDraft, CardFlag, ExtractionMeta, PageMeta

from ..images import PAGE_FILE, VIEW_FILE

CardReadPath = Literal["image_then_ocr", "image", "ocr"]
"""Which readers a card is read with: the image provider then the OCR fallback (production), or one of them (eval)"""


@dataclass
class CardPage:
    dir: Path
    """`pages/<n>/`, holding `page.jpg`, `view.jpg` and `thumb.webp`"""
    meta: PageMeta

    @property
    def page_path(self) -> Path:
        """`page.jpg`: the full page (long side at most 4096), for OCR and region re-reads"""
        return self.dir / PAGE_FILE

    @property
    def view_path(self) -> Path:
        """`view.jpg` (2048): what the model and the review page see"""
        return self.dir / VIEW_FILE


class CardPipelineOptions(BaseModel):
    cross_read: bool = False
    """Read the card a second time on the image slot and flag disagreements (§4.5)"""
    suggest_organizers: bool = True
    """Ask the fast slot for tags, categories and tools (only those the group already has are kept)"""
    read_path: CardReadPath = "image_then_ocr"


@dataclass
class CardExtraction:
    """What `extract_card` produces; it writes nothing to the database"""

    draft: CardDraft
    flags: list[CardFlag]
    transcription: str | None
    extraction: ExtractionMeta
