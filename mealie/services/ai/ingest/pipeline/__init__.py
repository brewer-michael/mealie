"""
The recipe card extraction pipeline (docs/ai/PHASE2.md §4, §5): everything between a job's normalized pages and its
draft, flags and transcription. The worker and the eval (§11) call exactly these functions, so the eval scores the
production pipeline.

The signatures here are final. Work item B1 provides the implementations; until then `extract_card`, `orient_page`,
`reread_region` and `options_for_group` raise `NotImplementedError`.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from mealie.lang.providers import Translator
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardFlag,
    CardProposal,
    ExtractionMeta,
    PageMeta,
    ProposalTarget,
)
from mealie.services.openai import OpenAIService

from ..images import RegionLike
from ..runner.types import ProgressCallback

CardReadPath = Literal["image_then_ocr", "image", "ocr"]
"""Which readers a card is read with: the image provider then the OCR fallback (production), or one of them (eval)"""


@dataclass
class CardPage:
    dir: Path
    """`pages/<n>/`, holding `page.jpg`, `view.jpg` and `thumb.webp`"""
    meta: PageMeta


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


def options_for_group(session: Session, group_id: UUID) -> CardPipelineOptions:
    """
    The pipeline options from the group's recipe card settings (defaults without a row), with `suggest_organizers`
    off when the group has no tags, categories or tools, so card text isn't sent for nothing. The eval uses it too,
    then sets `read_path` per config.
    """
    raise NotImplementedError("The card pipeline is work item B1")


async def extract_card(
    pages: list[CardPage],
    *,
    ai: OpenAIService,
    repos: AllRepositories,
    translator: Translator,
    options: CardPipelineOptions,
    on_progress: ProgressCallback | None = None,
) -> CardExtraction:
    """
    Reads a card and builds its draft (§4.1): the card compilers (image, then the OCR fallback, per
    `options.read_path`), the optional cross-read on the same `ai`, the build and organizer steps, ingredient
    normalization and linking, and the flags. Makes no database writes.

    Raises the most specific recorded provider error when nothing could be read (`RateLimitError`,
    `AIProviderLocalOnlyError`, `AIProviderLimitReachedError`, `OpenAINotEnabledException`, others), and
    `NoRecipeDataError` when the card holds no recipe.
    """
    raise NotImplementedError("The card pipeline is work item B1")


def orient_page(page: CardPage) -> PageMeta:
    """
    Turns a page upright when Tesseract is available and sure enough (`ORIENT_MIN_RATIO`), rewriting its files
    inside the ingest write lock, and records the OCR text it read. Returns the page's new metadata, with `oriented`
    set. Blocking (Tesseract); run it in a thread from async code.
    """
    raise NotImplementedError("The card pipeline is work item B1")


async def reread_region(
    page: CardPage, region: RegionLike, target: ProposalTarget, previous_text: str | None, *, ai: OpenAIService
) -> CardProposal:
    """
    Reads one region of a page again (§4.7): crops `page.jpg` in memory with a margin, sends only the crop to the
    image slot (or OCRs it when there's no image provider) and returns the reading as a region proposal for
    `target`.
    """
    raise NotImplementedError("The card pipeline is work item B1")
