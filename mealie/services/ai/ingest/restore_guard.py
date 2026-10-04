"""
Upstream's writes wait for, and stop during, a backup restore (docs/ai/PHASE2.md §3.9).

A restore replaces the database, `groups/` and `recipes/` while requests keep arriving. Fork code writes inside
`storage.ingest_write()`, but upstream's own writers (a recipe image upload, an import, a data migration, any database
write) didn't: one landing mid-restore could abort it half done, or be lost. This middleware makes every upstream API
write a write section:

- while a restore is pending or running it answers 503 with a translated message and `Retry-After`, and runs nothing;
- otherwise it reads the request's body first, holding nothing: a client still sending one (uvicorn has no body
  timeout, and FastAPI reads the body before it checks who's asking) never holds a restore off. Only then does it
  enter `storage.ingest_write()`, and it holds it for the rest of the request, background tasks included, so a
  restore waits for it (up to `RESTORE_LOCK_WAIT`, then refuses as busy) before it replaces anything. The route gets
  the body as it came, then the client's own messages.

It enters the section on threads of its own (`limits.GUARD_THREADS`), not the event loop's default pool, which
upstream's video and OCR image imports can keep busy for minutes.

Exempt: reads, the restore route itself (it takes the lock exclusively), `/api/auth/*` (signing in and refreshing a
token keep working) and `/api/ai/ingest*` (its routes have finer write sections of their own).
"""

import asyncio
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from enum import Enum
from typing import IO

import anyio
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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

_threads = ThreadPoolExecutor(max_workers=limits.GUARD_THREADS, thread_name_prefix="ai-ingest-guard")
"""Where requests enter their sections and check for a pause: never behind the event loop's long default-pool work"""


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
    Enters `section` on one of the guard's threads (it opens and locks a file). A request cancelled meanwhile leaves
    the section as soon as the thread has entered it, so it's never held for a request that's gone.
    """
    entering = asyncio.get_running_loop().run_in_executor(_threads, _enter, section)
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


def _has_body(scope: Scope) -> bool:
    """Whether the request comes with a body (HTTP/1.1: a non-zero `Content-Length`, or chunked)"""
    headers = Headers(scope=scope)
    return "transfer-encoding" in headers or headers.get("content-length", "0").strip() not in ("", "0")


class _Body:
    """
    A request's body, read before the request enters its section: in memory up to `GUARD_BODY_IN_MEMORY`, the rest in
    an unnamed temporary file (written and read on worker threads), then handed to the route as it came
    """

    _BLOCK = 1024 * 1024
    """Bytes per message when the body is handed on from the file"""

    def __init__(self) -> None:
        self._pending: list[bytes] = []
        self._pending_size = 0
        self._file: IO[bytes] | None = None
        self._size = 0
        self._offset = 0
        """How much of the file the route has had"""
        self._handed_on = False

    async def read(self, receive: Receive) -> bool:
        """Reads the whole body; False when the client left first"""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return False
            chunk = message.get("body", b"")
            if chunk:
                self._pending.append(chunk)
                self._pending_size += len(chunk)
                if self._pending_size > limits.GUARD_BODY_IN_MEMORY:
                    await anyio.to_thread.run_sync(self._spill)
            if not message.get("more_body", False):
                if self._file is not None:
                    await anyio.to_thread.run_sync(self._spill)
                return True

    def _spill(self) -> None:
        if self._file is None:
            self._file = tempfile.TemporaryFile()
        data = b"".join(self._pending)
        self._file.write(data)
        self._size += len(data)
        self._pending, self._pending_size = [], 0

    def _read_at(self, offset: int) -> bytes:
        assert self._file is not None
        self._file.seek(offset)
        return self._file.read(self._BLOCK)

    def replay(self, receive: Receive) -> Receive:
        """The route's `receive`: the body, then the client's own messages (its leaving, say)"""

        async def replayed() -> Message:
            if self._handed_on:
                return await receive()
            if self._file is None:
                self._handed_on = True
                data, self._pending = b"".join(self._pending), []
                return {"type": "http.request", "body": data, "more_body": False}
            offset = self._offset
            data = await anyio.to_thread.run_sync(self._read_at, offset)
            # moved on only once read: a cancelled read hands the same block on next time
            self._offset = offset + len(data)
            self._handed_on = self._offset >= self._size or not data
            return {"type": "http.request", "body": data, "more_body": not self._handed_on}

        return replayed

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._pending = []


class RestoreGuardMiddleware:
    """See the module's docstring"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_guarded(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return

        body: _Body | None = None
        try:
            if _has_body(scope):
                # refused before an upload that would be refused anyway; checked again on entering
                if await asyncio.get_running_loop().run_in_executor(_threads, storage.is_paused):
                    await _paused_response(scope)(scope, receive, send)
                    return
                body = _Body()
                if not await body.read(receive):
                    return  # the client left: nothing to answer, nothing run
                receive = body.replay(receive)
            await self._guarded(scope, receive, send)
        finally:
            if body is not None:
                body.close()

    async def _guarded(self, scope: Scope, receive: Receive, send: Send) -> None:
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
