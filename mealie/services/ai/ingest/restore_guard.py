"""
Upstream's writes wait for, and stop during, a backup restore (docs/ai/PHASE2.md §3.9).

A restore replaces the database, `groups/` and `recipes/` while requests keep arriving. Fork code writes inside
`storage.ingest_write()`, but upstream's own writers (a recipe image upload, an import, a data migration, any database
write) didn't: one landing mid-restore could abort it half done, or be lost. This middleware makes every upstream API
write a write section:

- while a restore is pending or running it answers 503 with a translated message and `Retry-After`, and runs nothing;
- otherwise it holds `storage.ingest_write()` for the whole request, background tasks included, so a restore waits
  for it (up to `RESTORE_LOCK_WAIT`, then refuses as busy) before it replaces anything.

Exempt: reads, the restore route itself (it takes the lock exclusively), `/api/auth/*` (signing in and refreshing a
token keep working) and `/api/ai/ingest*` (its routes have finer write sections of their own).
"""

import asyncio
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import Enum

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mealie.core.root_logger import get_logger
from mealie.lang.providers import get_locale_provider
from mealie.schema.response.responses import ErrorResponse
from mealie.services.ai.errors import IngestPaused

from . import limits, storage
from .i18n import with_fallback

logger = get_logger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXEMPT = re.compile(r"^/api/(?:auth/|ai/ingest(?:/|$)|admin/backups/[^/]+/restore/?$)")


def is_guarded(method: str, path: str) -> bool:
    """Whether a request is an upstream API write that a restore must wait for, and that waits for a restore"""
    return method.upper() in WRITE_METHODS and path.startswith("/api/") and not _EXEMPT.match(path)


class _Entered(Enum):
    yes = "yes"
    paused = "paused"
    unguarded = "unguarded"


_lock_warning_logged = False


def _enter(section: AbstractContextManager[None]) -> _Entered:
    global _lock_warning_logged
    try:
        section.__enter__()
    except IngestPaused:
        return _Entered.paused
    except OSError as e:
        # the lock file can't be opened (say, another user owns it): upstream keeps working, unguarded, as before
        if not _lock_warning_logged:
            _lock_warning_logged = True
            logger.warning(f"Writes can't wait for a backup restore ({e}): {storage.lock_path()}")
        return _Entered.unguarded
    return _Entered.yes


def _leave_when_entered(section: AbstractContextManager[None]) -> Callable[[asyncio.Future[_Entered]], None]:
    def leave(entering: asyncio.Future[_Entered]) -> None:
        if not entering.cancelled() and entering.exception() is None and entering.result() is _Entered.yes:
            section.__exit__(None, None, None)

    return leave


async def _enter_section(section: AbstractContextManager[None]) -> _Entered:
    """
    Enters `section` on a worker thread (it opens and locks a file). A request cancelled meanwhile leaves the section
    as soon as the thread has entered it, so it's never held for a request that's gone.
    """
    entering = asyncio.get_running_loop().run_in_executor(None, _enter, section)
    try:
        return await asyncio.shield(entering)
    except asyncio.CancelledError:
        entering.add_done_callback(_leave_when_entered(section))
        raise


def _paused_response(scope: Scope) -> JSONResponse:
    translator = with_fallback(get_locale_provider(Headers(scope=scope).get("accept-language")))
    message = translator.t("recipe-ingest.restore.writes-paused")
    return JSONResponse(
        {"detail": ErrorResponse.respond(message)},
        status_code=503,
        headers={"Retry-After": str(limits.PAUSED_RETRY_AFTER)},
    )


class RestoreGuardMiddleware:
    """See the module's docstring"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_guarded(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return

        section = storage.ingest_write()
        entered = await _enter_section(section)
        if entered is _Entered.paused:
            await _paused_response(scope)(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            if entered is _Entered.yes:
                # on the event loop, where a cancellation can't skip it: it unlocks a file at most
                section.__exit__(None, None, None)
