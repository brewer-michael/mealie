"""
The import workflow's context for a recipe card (docs/ai/PHASE2.md §3.7, §4.1): the card's pages, what the card
compilers learn besides the transcription (attribution, the reader's `unsure` list, which reader read it), the errors
the compilers recorded, and progress reported as the job's keys rather than translated text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mealie.lang.providers import Translator
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.recipe_ingest import ExtractionUnsure, IngestReadPath
from mealie.services.openai import OpenAIService
from mealie.services.recipe.import_workflow.context import WorkflowContext, WorkflowInput, WorkflowOptions

from ..runner.types import ProgressCallback
from .models import CardPage, CardPipelineOptions
from .service import end_transaction

if TYPE_CHECKING:
    from .compilers import CapturedError

PROGRESS_PREFIX = "recipe-ingest.progress."
PROGRESS_ORIENTING = f"{PROGRESS_PREFIX}orienting"
PROGRESS_READING_CARD = f"{PROGRESS_PREFIX}reading-card"
PROGRESS_READING_CARD_OCR = f"{PROGRESS_PREFIX}reading-card-ocr"
PROGRESS_CROSS_READING = f"{PROGRESS_PREFIX}cross-reading"
PROGRESS_STRUCTURING = f"{PROGRESS_PREFIX}structuring"
PROGRESS_LINKING_INGREDIENTS = f"{PROGRESS_PREFIX}linking-ingredients"
PROGRESS_SUGGESTING_ORGANIZERS = f"{PROGRESS_PREFIX}suggesting-organizers"

UPSTREAM_PROGRESS = {
    "recipe.create-progress.reading-images-with-ai": PROGRESS_READING_CARD,
    "recipe.create-progress.reading-images-with-ocr": PROGRESS_READING_CARD_OCR,
    "recipe.create-progress.creating-recipe": PROGRESS_STRUCTURING,
    "recipe.create-progress.organizing-recipe": PROGRESS_SUGGESTING_ORGANIZERS,
}
"""The upstream steps' and compilers' progress keys, as the job's progress keys; other upstream keys aren't reported"""


@dataclass
class CardWorkflowContext(WorkflowContext):
    pages: list[CardPage] = field(default_factory=list)
    """The card's pages, front first; `input.images` holds their `view.jpg`s"""

    compiler_errors: list[CapturedError] = field(default_factory=list)
    """What the card compilers failed with (`capture_errors` appends to this list)"""
    compilers_tried: list[str] = field(default_factory=list)
    """The compilers that tried to read the card, by class name, whether or not they succeeded"""

    attribution: str | None = None
    unsure: list[ExtractionUnsure] = field(default_factory=list)
    read_path: IngestReadPath | None = None
    """Which reader produced the transcription"""
    ocr_confidence: float | None = None
    """Tesseract's mean word confidence over the pages, when the OCR fallback read the card"""

    _last_progress: str | None = field(default=None, repr=False)

    @classmethod
    def for_card(
        cls,
        pages: list[CardPage],
        *,
        ai: OpenAIService,
        repos: AllRepositories,
        translator: Translator,
        options: CardPipelineOptions,
        errors: list[CapturedError],
        on_progress: ProgressCallback | None = None,
    ) -> CardWorkflowContext:
        """
        A context reading `pages`. Organizers are only ever suggestions here: the organizer step stores the names it
        gets back and attaches or creates nothing (F5).
        """
        return cls(
            input=WorkflowInput(images=[page.view_path for page in pages]),
            options=WorkflowOptions(
                resolve_organizers=options.suggest_organizers, attach_organizers=False, create_new_organizers=False
            ),
            repos=repos,
            translator=translator,
            ai=ai,
            on_progress=on_progress,
            pages=pages,
            compiler_errors=errors,
        )

    async def report_progress(self, key: str) -> None:
        """
        Reports `key` as one of the job's progress keys (`recipe-ingest.progress.*`), mapping the upstream steps'
        keys. Unknown keys and repeats of the last key aren't reported; the runner stores at most one a second.
        """
        progress_key = key if key.startswith(PROGRESS_PREFIX) else UPSTREAM_PROGRESS.get(key)
        if not progress_key or progress_key == self._last_progress or not self.on_progress:
            return

        self._last_progress = progress_key
        end_transaction(self.repos.session)  # the callback awaits; no transaction may stay open across it
        await self.on_progress(progress_key)
