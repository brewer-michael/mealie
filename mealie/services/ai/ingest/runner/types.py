"""
The contract between the runner and the task handlers (docs/ai/PHASE2.md §3.7): what a handler is given, what it
returns, and how it reports a failure it understands. A handler writes nothing; the runner applies its result with a
write fenced on the task's lease token.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mealie.schema.recipe_ingest import (
    CardDraft,
    CardFlag,
    CardProposal,
    ExtractionMeta,
    IngestErrorCode,
    IngestTaskKind,
    PageMeta,
)

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
    """`task_payload`: a re-read's page, region and target"""
    token: UUID
    """The claim's lease token"""
    locale: str
    """The uploader's language (en-US for inbox jobs); the runner has already set the locale context to it"""
    local_only: bool
    """The runner has already applied this policy (`ai_call_policy`) around the handler"""
    report_progress: ProgressCallback


@dataclass
class ExtractResult:
    """A finished extraction: the job's new draft and everything that goes with it"""

    draft: CardDraft
    flags: list[CardFlag]
    transcription: str | None
    extraction: ExtractionMeta
    pages: list[PageMeta]
    """The pages as they are now (orientation may have turned them)"""


@dataclass
class RereadResult:
    """A finished region re-read"""

    proposal: CardProposal


class TaskFailed(Exception):
    """A failure the handler understands, stored on the job as its error code and params (§3.6)"""

    def __init__(self, code: IngestErrorCode, params: dict[str, Any] | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.params: dict[str, Any] = params or {}
