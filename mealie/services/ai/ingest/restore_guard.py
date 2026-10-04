"""
Upstream's requests during, and writes around, a backup restore (docs/ai/PHASE2.md §3.9).

A restore replaces the database, `groups/` and `recipes/` while requests keep arriving. Fork code writes inside
`storage.ingest_write()`, but upstream's own writers (a recipe image upload, an import, a data migration, any database
write) didn't: one landing mid-restore could abort it half done, or be lost. And while the tables are dropped and
imported again, any request that reads the database fails: a sign-in check answers 401 (and the app signs out), the
rest 500. So this middleware:

- **while a restore is pending or running** answers every `/api/` request but the restore route's own with `503
  paused_for_restore` and `Retry-After`, before any route, sign-in check or database access. Writes carry a translated
  `message`; reads and the app's own token refresh don't, since the frontend toasts a `message` and a page polling
  would toast every time.
- **otherwise makes every upstream API write a write section**, held for the rest of the request, background tasks
  included, so a restore waits for it (up to `RESTORE_LOCK_WAIT`, then refuses as busy) before it replaces anything.
  The body is never copied, and a client still sending one never holds a restore off (uvicorn has no body timeout):
  - a route that takes a body enters its section when it takes the body's last message. FastAPI reads a declared
    body before anything else runs, sign-in included, so the body streams to the route's own parser and nothing is
    held while it arrives. A section refused then gets the 503, and the route never runs.
  - any other route enters before it runs: it reads no body, or (the MCP endpoint) checks its token before it reads
    one, so only a signed-in client's slow body holds a restore, at most `RESTORE_LOCK_WAIT`;
  - a request no route takes (404, 405, a redirect, the SPA's files) runs nothing and enters nothing.

It checks for a pause, and enters sections, on threads of its own (`limits.GUARD_THREADS`), not the event loop's
default pool, which upstream's video and OCR image imports can keep busy for minutes.

Exempt from the section, not from the pause: reads, `/api/auth/*` (signing in) and `/api/ai/ingest*` (its routes have
finer write sections of their own). The restore route is exempt from both: it takes the lock exclusively.
"""

import asyncio
import functools
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from enum import Enum
from typing import Any

from fastapi import params
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from python_multipart.multipart import parse_options_header
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match, Mount, Router
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mealie.core.root_logger import get_logger
from mealie.lang.providers import get_locale_provider
from mealie.services.ai.errors import IngestPaused

from . import limits, storage
from .i18n import with_fallback

logger = get_logger(__name__)

PAUSED_FOR_RESTORE = "paused_for_restore"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_RESTORE_ROUTE = re.compile(r"^/api/admin/backups/[^/]+/restore/?$")
_CARD_ROUTES = re.compile(r"^/api/ai/ingest(?:/|$)")
_CARD_UPLOAD = re.compile(r"^/api/ai/ingest/?$")
"""Its refusals carry a top-level `summary` too, what an iOS Shortcut shows (`routes/ai/ingest/upload.py`)"""
_EXEMPT = re.compile(r"^/api/(?:auth/|ai/ingest(?:/|$)|admin/backups/[^/]+/restore/?$)")
_UNPROMPTED = re.compile(r"^/api/auth/refresh/?$")
"""Writes the app makes on its own (renewing its token): their 503 carries no message to toast, as a read's"""

_FORM_TYPES = frozenset({b"multipart/form-data", b"application/x-www-form-urlencoded"})
"""The content types Starlette's `request.form()` reads; it reads nothing of any other"""
READS_FORM_FIRST = frozenset({"/api/oauth/token", "/api/oauth/revoke"})
"""
Routes that declare no body but read a form by hand before anything else: the MCP OAuth token endpoints, which anyone
can call (`mealie/routes/oauth/controller_oauth.py`). Their sections start with the form's last message, as for a
declared body.
"""


def is_guarded(method: str, path: str) -> bool:
    """Whether a request is an upstream API write that a restore must wait for, and that waits for a restore"""
    return method.upper() in WRITE_METHODS and path.startswith("/api/") and not _EXEMPT.match(path)


_RECIPE_PAGES = re.compile(r"^/g/[^/]+/(?:shared/)?r/[^/]+/?$")
"""
The SPA's recipe pages, which the server renders with the recipe's meta tags (`mealie/routes/spa`): they read the
database, before their route runs too (who's signed in)
"""


def is_recipe_page(method: str, path: str) -> bool:
    """Whether a request is for a recipe page the server fills in from the database, which a restore serves plain"""
    return method.upper() in {"GET", "HEAD"} and _RECIPE_PAGES.match(path) is not None


def is_paused_for(path: str) -> bool:
    """Whether a request is answered 503 while a restore is pending or running: every `/api/` one but the restore's"""
    return path.startswith("/api/") and not _RESTORE_ROUTE.match(path)


class SectionStart(Enum):
    """When a guarded write enters its section"""

    before_the_route = "before_the_route"
    with_the_last_body_message = "with_the_last_body_message"
    never = "never"
    """No route takes the request: it runs nothing"""


def _route_for(routes: Sequence[BaseRoute], scope: Scope) -> RouteContext | None:
    """
    The route the router hands the request to (its first full match, in FastAPI's order: included routers' routes in
    place, with their prefixes and dependencies), or None (a 404, 405 or redirect)
    """
    for context in iter_route_contexts(routes):
        match, child_scope = context.matches(scope)
        if match is Match.FULL:
            route = context.original_route
            if isinstance(route, Mount) and route.routes:
                return _route_for(route.routes, {**scope, **child_scope})
            return context
    return None


def _has_body(scope: Scope) -> bool:
    """Whether the request comes with a body (HTTP/1.1: a non-zero `Content-Length`, or chunked)"""
    headers = Headers(scope=scope)
    return "transfer-encoding" in headers or headers.get("content-length", "0").strip() not in ("", "0")


def _is_form(scope: Scope) -> bool:
    content_type, _ = parse_options_header(Headers(scope=scope).get("content-type"))
    return content_type in _FORM_TYPES


_routers: dict[int, Router] = {}
"""The routers `_cached_route_for` has seen, by `id` (FastAPI's can't be hashed)"""


@functools.lru_cache(maxsize=1024)
def _cached_route_for(router_id: int, routes: int, method: str, path: str, root_path: str) -> RouteContext | None:
    """`_route_for`, remembered: an app's routes are set up once, and its router matches every request again anyway"""
    scope = {"type": "http", "method": method, "path": path, "root_path": root_path}
    return _route_for(_routers[router_id].routes, scope)


def section_start(scope: Scope) -> SectionStart:
    """When a guarded write enters its section, from the route the app's router will hand it to"""
    router = getattr(scope.get("app"), "router", None)
    if not isinstance(router, Router):
        return SectionStart.before_the_route  # not called inside an app: the safe side

    _routers.setdefault(id(router), router)
    context = _cached_route_for(
        id(router), len(router.routes), scope["method"], scope["path"], scope.get("root_path", "")
    )
    if context is None:
        return SectionStart.never
    route = context.original_route
    if isinstance(route, Mount) and isinstance(route.app, StaticFiles):
        return SectionStart.never
    if isinstance(route, APIRoute) and _has_body(scope):
        body = context.body_field  # the route's as included: its routers' dependencies may take a body too
        if body is not None and not isinstance(body.field_info, params.Form):
            return SectionStart.with_the_last_body_message  # FastAPI reads a JSON or raw body whatever its type
        if (body is not None or context.path in READS_FORM_FIRST) and _is_form(scope):
            return SectionStart.with_the_last_body_message
    # no body, a body the route doesn't declare (it may never read it), or a form Starlette won't read
    return SectionStart.before_the_route


class _Entered(Enum):
    yes = "yes"
    paused = "paused"
    unguarded = "unguarded"


class _BodyRefused(Exception):
    """Raised to the route's body parser when its section is refused: the route never runs"""


_lock_warning_logged = False
_pause_warning_logged = False

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


def _check_paused() -> bool:
    global _pause_warning_logged
    try:
        return storage.is_paused()
    except OSError as e:
        # as for a lock file that can't be opened: requests carry on, as before
        if not _pause_warning_logged:
            _pause_warning_logged = True
            logger.warning(f"Requests can't check for a backup restore ({type(e).__name__}: {e})")
        return False


async def _is_paused() -> bool:
    return await asyncio.get_running_loop().run_in_executor(_threads, _check_paused)


def paused_response(scope: Scope) -> JSONResponse:
    """503 `paused_for_restore` with `Retry-After`; a write's carries a translated `message`, a read's none"""
    detail = {"code": PAUSED_FOR_RESTORE}
    content: dict[str, Any] = {"detail": detail}
    if scope["method"].upper() in WRITE_METHODS and not _UNPROMPTED.match(scope["path"]):
        translator = with_fallback(get_locale_provider(Headers(scope=scope).get("accept-language")))
        # the card routes' own text (`_deps.paused_error`), so they answer as they always did
        card = _CARD_ROUTES.match(scope["path"])
        detail["message"] = translator.t(
            "recipe-ingest.errors.paused-for-restore" if card else "recipe-ingest.restore.writes-paused"
        )
        if _CARD_UPLOAD.match(scope["path"]):
            content["summary"] = detail["message"]
    return JSONResponse(content, status_code=503, headers={"Retry-After": str(limits.PAUSED_RETRY_AFTER)})


class RestoreGuardMiddleware:
    """See the module's docstring"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_recipe_page(scope["method"], scope["path"]) and await _is_paused():
            # the page without its meta tags, as the SPA serves any other: the app then waits for the restore itself
            scope = {**scope, "path": "/", "raw_path": b"/"}
        if scope["type"] != "http" or not is_paused_for(scope["path"]):
            await self.app(scope, receive, send)
            return

        start = section_start(scope) if is_guarded(scope["method"], scope["path"]) else SectionStart.never
        if start is SectionStart.before_the_route:
            await self._guarded(scope, receive, send)  # entering checks for a pause
            return

        if await _is_paused():
            await paused_response(scope)(scope, receive, send)
            return
        if start is SectionStart.never:
            await self.app(scope, receive, send)
            return
        await self._guarded_from_the_last_body_message(scope, receive, send)

    async def _guarded(self, scope: Scope, receive: Receive, send: Send) -> None:
        section = storage.ingest_write()
        entered = await _enter_section(section)
        if entered is _Entered.paused:
            await paused_response(scope)(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            if entered is _Entered.yes:
                # on the event loop, where a cancellation can't skip it: it unlocks a file at most
                section.__exit__(None, None, None)

    async def _guarded_from_the_last_body_message(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        The route reads its body straight from the client; the section starts as it takes the last message. Refused
        then, the route's parser fails (so the route never runs), whatever the app makes of that is dropped, and the
        client gets the 503.
        """
        section = storage.ingest_write()
        entered: _Entered | None = None
        started = False

        async def receive_then_enter() -> Message:
            nonlocal entered
            message = await receive()
            if entered is None and message["type"] == "http.request" and not message.get("more_body", False):
                entered = await _enter_section(section)
                if entered is _Entered.paused:
                    raise _BodyRefused()
            return message

        async def send_unless_refused(message: Message) -> None:
            nonlocal started
            if entered is _Entered.paused:
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            try:
                await self.app(scope, receive_then_enter, send_unless_refused)
            except Exception:
                if entered is not _Entered.paused:
                    raise
                # the route's own failure to read the refused body: answered below
            if entered is _Entered.paused and not started:
                await paused_response(scope)(scope, receive, send)
        finally:
            if entered is _Entered.yes:
                section.__exit__(None, None, None)
