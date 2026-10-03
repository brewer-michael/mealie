"""
What every recipe card route shares (docs/ai/PHASE2.md §9, §14): the household-scoped controller base, the checks
made before a route does anything, the write section that turns a restore's pause into a 503, and the error body.

**Error bodies** are `{"detail": {"code": ..., "message"?: ..., **params}}`. Errors the review page handles itself
(`version_conflict`, `busy`, `unresolved_flags`) carry no `message`, because the frontend's axios interceptor toasts
any `detail.message`. Errors it doesn't handle (503, 429, 413, 415, 401) carry a translated `message`, which the
interceptor toasts and an iOS Shortcut can show.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import cached_property
from typing import Any

from fastapi import HTTPException, status
from fastapi.encoders import jsonable_encoder

from mealie.lang.providers import Translator, get_locale_context, get_locale_provider
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.routes._base.base_controllers import BaseUserController
from mealie.services.ai.errors import IngestPaused
from mealie.services.ai.ingest import limits, storage
from mealie.services.ai.ingest.settings import get_ingest_settings

PAUSED_FOR_RESTORE = "paused_for_restore"
INGEST_DISABLED = "ingest_disabled"


class IngestController(BaseUserController):
    """A controller for the recipe card routes, scoped to the user's group and household"""

    @cached_property
    def ingest_repos(self) -> IngestRepos:
        return IngestRepos(self.session, self.group_id, self.household_id)


def _translator(translator: Translator | None) -> Translator:
    if translator is not None:
        return translator
    if context := get_locale_context():
        return context[0]
    return get_locale_provider("en-US")


def ingest_error(
    status_code: int,
    code: str,
    *,
    message_key: str | None = None,
    translator: Translator | None = None,
    headers: Mapping[str, str] | None = None,
    message_params: Mapping[str, Any] | None = None,
    **params: Any,
) -> HTTPException:
    """
    An `HTTPException` with the body `{"detail": {"code": code, **params}}`, plus a translated `message` when
    `message_key` is given (only for errors the page doesn't handle itself). The message is in the request's
    language unless `translator` says otherwise; `message_params` fill its placeholders.
    """
    detail: dict[str, Any] = {"code": code, **jsonable_encoder(params)}
    if message_key:
        detail["message"] = _translator(translator).t(message_key, **dict(message_params or {}))
    return HTTPException(status_code, detail=detail, headers=dict(headers) if headers else None)


def paused_error(translator: Translator | None = None) -> HTTPException:
    """503 `paused_for_restore`, with `Retry-After`"""
    return ingest_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        PAUSED_FOR_RESTORE,
        message_key="recipe-ingest.errors.paused-for-restore",
        translator=translator,
        headers={"Retry-After": str(limits.PAUSED_RETRY_AFTER)},
    )


def require_enabled(translator: Translator | None = None) -> None:
    """503 `ingest_disabled` when `AI_INGEST_ENABLED` is off"""
    if not get_ingest_settings().ENABLED:
        raise ingest_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            INGEST_DISABLED,
            message_key="recipe-ingest.errors.ingest-disabled",
            translator=translator,
        )


def require_not_paused(translator: Translator | None = None) -> None:
    """503 `paused_for_restore` (with `Retry-After: 60`) while a backup restore pauses ingestion"""
    if storage.is_paused():
        raise paused_error(translator)


@contextmanager
def write_section(translator: Translator | None = None) -> Iterator[None]:
    """
    `storage.ingest_write()` for a route that writes files: a restore pausing ingestion, at the start or while the
    section runs, becomes 503 `paused_for_restore`.
    """
    try:
        with storage.ingest_write():
            yield
    except IngestPaused as e:
        raise paused_error(translator) from e
