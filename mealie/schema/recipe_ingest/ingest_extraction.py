"""
Fork: how a card's draft was produced (docs/ai/PHASE2.md §4, §13): which reader, what it was unsure of, the second
reading, provider usage and the pipeline version. Never holds a provider's response body.
"""

from pydantic import ConfigDict, Field

from mealie.schema._mealie import MealieModel

from .ingest_enums import IngestReadPath

CARD_PIPELINE_VERSION = 1
"""Recorded on every extraction, so drafts from an older pipeline can be told apart"""


class ExtractionUnsure(MealieModel):
    """Something on the card the reader could read but wasn't sure of"""

    text: str
    alternatives: list[str] = Field(default_factory=list)
    reason: str = "other"
    """`faded`, `ambiguous`, `cut_off`, `smudged` or `other`"""

    model_config = ConfigDict(extra="ignore")


class ExtractionUsage(MealieModel):
    """Tokens and time one kind of request took, per provider"""

    feature: str
    """The response schema's class name, as in the usage log"""
    slot: str
    provider: str | None = None
    model: str | None = None
    """The model that answered"""
    requests: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    model_config = ConfigDict(extra="ignore")


class ExtractionCompilerError(MealieModel):
    """A reader that failed, described safely (`describe_provider_error`)"""

    compiler: str
    error: str

    model_config = ConfigDict(extra="ignore")


class CardReadInfo(MealieModel):
    """What the review page's checks line says ("Read by Claude Sonnet · checked against a second reading")"""

    read_path: IngestReadPath | None = None
    provider: str | None = None
    model: str | None = None
    ocr_confidence: float | None = None
    """Tesseract's mean word confidence (0-100), when the OCR fallback read the card"""
    cross_read: bool = False
    """Checked against a second, independent reading"""
    cross_read_failed: bool = False


class ExtractionMeta(MealieModel):
    """Stored in the job's `extraction` column"""

    pipeline_version: int = CARD_PIPELINE_VERSION
    read_path: IngestReadPath | None = None
    language: str | None = None
    attribution: str | None = None
    unsure: list[ExtractionUnsure] = Field(default_factory=list)
    cross_read_lines: list[str] | None = None
    """The second reading's lines; None when there wasn't one"""
    cross_read_failed: bool = False
    ocr_confidence: float | None = None
    provider: str | None = None
    """The provider that read the card"""
    model: str | None = None
    """The model that read the card"""
    step_outcomes: dict[str, str] = Field(default_factory=dict)
    """Upstream's workflow step outcomes by step name"""
    compiler_errors: list[ExtractionCompilerError] = Field(default_factory=list)
    usage: list[ExtractionUsage] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    def read_info(self) -> CardReadInfo:
        return CardReadInfo(
            read_path=self.read_path,
            provider=self.provider,
            model=self.model,
            ocr_confidence=self.ocr_confidence if self.read_path == IngestReadPath.ocr else None,
            cross_read=self.cross_read_lines is not None,
            cross_read_failed=self.cross_read_failed,
        )
