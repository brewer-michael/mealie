"""Fork: the enumerations of recipe card ingestion (docs/ai/PHASE2.md §13)"""

from enum import StrEnum


class IngestStatus(StrEnum):
    """A job's review lifecycle (§3.1)"""

    processing = "processing"
    """The first extraction is queued or running"""
    ready = "ready"
    """A draft exists"""
    failed = "failed"
    """The first extraction failed"""
    committing = "committing"
    """A commit is in progress; `commit_started_at` is its lease"""
    committed = "committed"
    """The recipe exists (`recipe_id`)"""


class IngestSource(StrEnum):
    """Where a batch's cards came from: its jobs share it"""

    app = "app"
    api = "api"
    inbox = "inbox"


class IngestTaskKind(StrEnum):
    extract = "extract"
    """First extraction, retry or re-extract"""
    reread = "reread"
    """Re-read one region of a page"""


class IngestTaskState(StrEnum):
    """A job's pending task; a job without one has `task_state` unset"""

    queued = "queued"
    running = "running"


class IngestErrorCode(StrEnum):
    """Why a task or a commit failed (§3.6). Stored with its params; the frontend translates `recipe-ingest.error.*`."""

    ai_not_enabled = "ai_not_enabled"
    local_only_unavailable = "local_only_unavailable"
    limit_reached = "limit_reached"
    rate_limited = "rate_limited"
    provider_failed = "provider_failed"
    no_recipe_found = "no_recipe_found"
    files_missing = "files_missing"
    owner_missing = "owner_missing"
    interrupted = "interrupted"
    cancelled = "cancelled"
    timeout = "timeout"
    internal_error = "internal_error"
    commit_invalid = "commit_invalid"
    commit_interrupted = "commit_interrupted"


class IngestRejectReason(StrEnum):
    """Why an uploaded image wasn't turned into a card (§1.2)"""

    too_large = "too_large"
    unsupported_format = "unsupported_format"
    pdf_not_supported = "pdf_not_supported"
    too_many_pixels = "too_many_pixels"
    unreadable_image = "unreadable_image"
    too_many_pages = "too_many_pages"
    duplicate = "duplicate"


class IngestReadPath(StrEnum):
    """Which reader produced a draft"""

    image = "image"
    """An image provider read the photos"""
    ocr = "ocr"
    """Tesseract read the photos, and the default provider structured its text"""


class CardFlagKind(StrEnum):
    """What a flag says about a field (§4.6)"""

    illegible = "illegible"
    blank = "blank"
    missing_name = "missing_name"
    unsure = "unsure"
    not_on_card = "not_on_card"
    marker_dropped = "marker_dropped"
    read_disagreement = "read_disagreement"
    check_parse = "check_parse"
    unit_unclear = "unit_unclear"
    implausible_amount = "implausible_amount"
    implausible_temperature = "implausible_temperature"
    empty_section = "empty_section"
    read_by_ocr = "read_by_ocr"
    cross_read_failed = "cross_read_failed"
    shorthand_read = "shorthand_read"
    not_parsed = "not_parsed"
    new_food = "new_food"
    new_unit = "new_unit"


class CardFlagSeverity(StrEnum):
    error = "error"
    """Blocks commit until fixed or kept"""
    warning = "warning"
    """Highlighted; can be dismissed"""
    info = "info"
    """Shown quietly"""


class CardFlagSource(StrEnum):
    """What raised a flag"""

    marker = "marker"
    model = "model"
    validator = "validator"
    parser = "parser"
    ocr = "ocr"
    cross_read = "cross_read"


class FlagResolution(StrEnum):
    kept = "kept"
    """An error the reviewer kept as written ("Keep as written")"""
    dismissed = "dismissed"
    """A warning the reviewer dismissed ("Looks right")"""


class CardProposalKind(StrEnum):
    region = "region"
    """A re-read of one region, for one field"""
    full = "full"
    """A whole new draft from re-extracting an edited card"""


class PageRotationSource(StrEnum):
    """What last turned a page"""

    none = "none"
    ocr = "ocr"
    user = "user"
