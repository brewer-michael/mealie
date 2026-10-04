"""
The contract between the runner and the task handlers (docs/ai/PHASE2.md §3.7): what a handler is given, what it
returns, and how it reports a failure it understands. A handler writes nothing; the runner applies its result with a
write fenced on the task's lease token.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardFlag,
    CardProposal,
    CardProposalOrigin,
    ExtractionMeta,
    IngestErrorCode,
    IngestTaskKind,
    PageMeta,
)

from .answers import KeptAnswers

ProgressCallback = Callable[[str], Awaitable[None]]
"""Reports a progress key (e.g. `recipe-ingest.progress.reading-card`); the runner stores at most one a second"""


@dataclass(frozen=True)
class TaskContext:
    """One claimed task, as its handler sees it"""

    job_id: UUID
    group_id: UUID
    household_id: UUID
    kind: IngestTaskKind
    payload: dict[str, Any] | None
    """
    `task_payload`: a re-read's page, region and target; an extract task's `mode` (`IngestTaskMode`, none for reading
    the card again) and what it needs (`tasks.rebuild_payload`, `tasks.parse_lines_payload`)
    """
    token: UUID
    """The claim's lease token"""
    locale: str
    """The uploader's language (en-US for inbox jobs); the runner has already set the locale context to it"""
    local_only: bool
    """The runner has already applied this policy (`ai_call_policy`) around the handler"""
    report_progress: ProgressCallback
    answers: KeptAnswers = field(default_factory=KeptAnswers)
    """
    Provider answers to replay rather than ask for again (an earlier task on the card that a backup restore cut off
    got them), and where the handler's AI service records the answers it gets, in case a restore cuts this one off
    """


@dataclass
class ExtractResult:
    """A finished extraction: the job's new draft and everything that goes with it"""

    draft: CardDraft
    flags: list[CardFlag]
    transcription: str | None
    extraction: ExtractionMeta
    pages: list[PageMeta]
    """The pages as they are now (orientation may have turned them)"""
    origin: CardProposalOrigin = CardProposalOrigin.reextract
    """What made it: the card read again, or the recipe built again from the reviewer's edited transcription"""


@dataclass
class RereadResult:
    """A finished region re-read"""

    proposal: CardProposal


@dataclass
class ParseLinesResult:
    """Chosen ingredient lines parsed by the AI ingredient parser ("Parse with AI")"""

    ingredients: list[CardDraftIngredient]
    """The parsed lines, each with its `reference_id`"""
    sent: dict[str, str]
    """Each line's text as it was sent, by `reference_id`: a line the reviewer changed since keeps their change"""
    units: list[str] = field(default_factory=list)
    """The group's unit names (`IngestMatcher.unit_names`), for the parsed lines' `unit_unclear`, as at extraction"""
    linked: dict[UUID, list[str]] | None = None
    """
    Every name of the foods and units the draft and the parsed lines link (`IngestMatcher.linked_names`), so the
    parsed lines' `linked_fuzzy` is judged as extraction's; None for a result kept from before it was recorded
    """


class TaskFailed(Exception):
    """A failure the handler understands, stored on the job as its error code and params (§3.6)"""

    def __init__(self, code: IngestErrorCode, params: dict[str, Any] | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.params: dict[str, Any] = params or {}
