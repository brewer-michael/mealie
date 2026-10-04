"""
What a task's failure means for its job (docs/ai/PHASE2.md §3.6). The runtime has already tried every candidate
provider with the SDK's own retries (F13), so most errors are final: they become an `IngestErrorCode` with params.
Two aren't: every provider answering 429 requeues the task with a backoff, and anything that happens while a backup
restore has paused ingestion waits for the pause to end and then gives the task back (§3.9).

Provider errors are stored as `describe_provider_error`'s text (the error's type and HTTP status), never `str(e)`,
which can hold the provider's response body. Logs carry the job id and the code, never card text.
"""

import os
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from mealie.core import exceptions
from mealie.core.root_logger import get_logger
from mealie.schema.recipe_ingest import IngestErrorCode
from mealie.services.ai.errors import (
    AIProviderError,
    AIProviderLimitReachedError,
    AIProviderLocalOnlyError,
    IngestPaused,
    describe_provider_error,
    is_rate_limit_error,
)
from mealie.services.openai.openai import OpenAINotEnabledException
from mealie.services.recipe.import_workflow.exceptions import NoRecipeDataError

from .. import limits
from .types import TaskFailed

logger = get_logger(__name__)

UPSTREAM_PROVIDER_FAILURE = "OpenAI Request Failed."
"""How upstream's `OpenAIService.get_response` wraps a provider's error (`raise Exception(...) from e`)"""

_CHAIN_DEPTH = 8


class Disposition(StrEnum):
    fail = "fail"
    """Store the code on the job: a first extraction fails, a ready job keeps its draft and shows a banner"""
    rate_limited = "rate_limited"
    """Every provider answered 429: requeue with a backoff, or fail `rate_limited` once the retries are used up"""
    paused = "paused"
    """A backup restore paused ingestion: wait for it to end, then give the task back without using up an attempt"""


@dataclass(frozen=True)
class Classified:
    disposition: Disposition
    code: IngestErrorCode | None = None
    """For `fail`"""
    params: dict[str, Any] = field(default_factory=dict)


def rate_limit_delay(retries: int) -> int:
    """Seconds before a rate-limited task runs again, after `retries` earlier rate-limit retries: 60, 120, ... 900"""
    return min(limits.RATE_LIMIT_BACKOFF * 2 ** max(retries, 0), limits.RATE_LIMIT_BACKOFF_MAX)


def _chain(error: BaseException) -> Iterator[BaseException]:
    """`error` and the errors it was raised from (`raise ... from`), innermost last"""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < _CHAIN_DEPTH:
        seen.add(id(current))
        yield current
        current = current.__cause__


def _find[E: BaseException](error: BaseException, kind: type[E]) -> E | None:
    return next((e for e in _chain(error) if isinstance(e, kind)), None)


def is_provider_error(error: BaseException) -> bool:
    """Whether `error` came from an AI provider call (as opposed to a bug or a database problem here)"""
    import anthropic
    import httpx
    import openai

    provider_types = (
        AIProviderError,
        exceptions.OpenAIServiceError,
        openai.APIError,
        anthropic.APIError,
        httpx.HTTPError,
    )
    for e in _chain(error):
        if isinstance(e, provider_types):
            return True
        if type(e) is Exception and str(e).startswith(UPSTREAM_PROVIDER_FAILURE):
            return True
    return False


def safe_trace(error: BaseException) -> str:
    """
    Where an unexpected error happened, for the log: the stack and the exception types, without their messages,
    which could hold card text.
    """
    parts: list[str] = []
    for e in reversed(list(_chain(error))):
        parts.append("".join(traceback.format_tb(e.__traceback__)))
        parts.append(f"{type(e).__module__}.{type(e).__qualname__}\n")
    return "".join(parts)


def _is_job_file(missing: FileNotFoundError, job_dir: Path | None) -> bool:
    """Whether a missing file is one of the job's own (unknown paths count): not, say, a prompt file"""
    if job_dir is None or not missing.filename:
        return True
    try:
        return Path(os.fsdecode(missing.filename)).resolve().is_relative_to(job_dir.resolve())
    except OSError, TypeError, ValueError:
        return True


def classify(error: BaseException, *, job_id: UUID, paused: bool, job_dir: Path | None = None) -> Classified:
    """
    The §3.6 outcome of a task that raised `error`. `paused` says whether the pause marker is set now: any error
    while paused waits for the restore, since missing files and dropped tables are expected then. `job_dir` is the
    job's directory: a missing file elsewhere is an internal error, not `files_missing`.
    """
    if paused or _find(error, IngestPaused) is not None:
        return Classified(Disposition.paused)

    if isinstance(error, TaskFailed):
        return Classified(Disposition.fail, error.code, dict(error.params))

    if any(isinstance(e, exceptions.RateLimitError) or is_rate_limit_error(e) for e in _chain(error)):
        return Classified(Disposition.rate_limited)

    missing = _find(error, FileNotFoundError)
    if missing is not None and _is_job_file(missing, job_dir):
        return Classified(Disposition.fail, IngestErrorCode.files_missing)

    specific: list[tuple[type[BaseException], IngestErrorCode]] = [
        (AIProviderLocalOnlyError, IngestErrorCode.local_only_unavailable),
        (AIProviderLimitReachedError, IngestErrorCode.limit_reached),
        (OpenAINotEnabledException, IngestErrorCode.ai_not_enabled),
        (NoRecipeDataError, IngestErrorCode.no_recipe_found),
    ]
    for kind, code in specific:
        if _find(error, kind) is not None:
            return Classified(Disposition.fail, code)

    if is_provider_error(error):
        detail = describe_provider_error(error)
        logger.warning(f"Recipe card job {job_id}: the AI provider failed ({detail})")
        return Classified(Disposition.fail, IngestErrorCode.provider_failed, {"detail": detail})

    logger.error(f"Recipe card job {job_id} failed with an internal error:\n{safe_trace(error)}")
    return Classified(Disposition.fail, IngestErrorCode.internal_error)
