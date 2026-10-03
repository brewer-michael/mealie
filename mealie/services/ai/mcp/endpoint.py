"""
The ASGI app behind `/api/mcp` (docs/ai/PHASE3.md §1-2). It checks the request's `Origin`, method and bearer token,
then hands it to the MCP SDK's Streamable HTTP session manager.
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from mealie.lang.providers import get_locale_provider
from mealie.services.oauth.urls import canonical_resource, mcp_url, request_origin

from . import tool_bridge
from .auth import cached_mcp_principal, mcp_www_authenticate, verify_mcp_token
from .server import REQUEST_STATE_KEY, McpRequestState

INVALID_REQUEST = -32600
INTERNAL_ERROR = -32603

MAX_REQUEST_BODY = 64 * 1024
"""Bytes a request may have: the largest tool call is about 10 KB (50 shopping list items). The SDK's 4 MiB default
would let one request put megabytes into the SDK's warnings, which quote what they couldn't parse."""
SHUTDOWN_TIMEOUT = 10.0
"""Seconds a stopping server waits for tools that outlived their requests, and for events still being sent"""


def _error(
    status_code: int, message: str, code: int = INVALID_REQUEST, headers: dict[str, str] | None = None
) -> JSONResponse:
    """A JSON-RPC error with no request id, as the SDK answers HTTP-level errors"""
    body = {"jsonrpc": "2.0", "id": "server-error", "error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status_code, headers=headers)


def _bearer_token(authorization: str | None) -> str | None:
    scheme, _, credentials = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip() or None


def is_same_origin(origin_header: str | None, origin: str) -> bool:
    """Whether a request's `Origin` header (if it has one) names this server as the request reached it"""
    if origin_header is None:
        return True
    return canonical_resource(origin_header.strip()) == origin


class McpEndpoint:
    """
    Mounted as a Starlette `Route` endpoint: a class instance, so it's called as an ASGI app with every method,
    and answers anything but POST itself. A route limited to POST would let other methods fall through to the
    production SPA, which answers them with HTML.
    """

    def __init__(self, server: Server[Any, Any]) -> None:
        self.server = server
        self._managers: list[StreamableHTTPSessionManager] = []

    @asynccontextmanager
    async def lifespan(self, _app: Any) -> AsyncIterator[None]:
        """
        Runs a session manager while the app runs. A manager can only run once, so every lifespan gets a new one.

        Stateless, so no session ids: any worker process can answer any request, and Home Assistant opens a new
        session for each tool call anyway. JSON responses, so no event streams pass through Mealie's middleware.
        """
        manager = StreamableHTTPSessionManager(
            app=self.server, stateless=True, json_response=True, max_request_body_size=MAX_REQUEST_BODY
        )
        try:
            async with manager.run():
                self._managers.append(manager)
                try:
                    yield
                finally:
                    self._managers.remove(manager)
        finally:
            await tool_bridge.finish_detached(SHUTDOWN_TIMEOUT)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        arrived = time.monotonic()
        headers = Headers(scope=scope)
        origin = request_origin(scope)

        # The spec requires a 403 for an Origin that isn't ours: another site's page can't call Mealie through a
        # visitor's browser. Against DNS rebinding, where the page's origin is the Host it reaches, this doesn't help;
        # what does is that only a bearer token authenticates here, never a cookie a browser would add. Home
        # Assistant, Claude and other MCP clients that aren't web pages send no Origin.
        if not is_same_origin(headers.get("origin"), origin):
            await _error(403, "Forbidden: requests from other origins aren't accepted")(scope, receive, send)
            return

        if scope["method"] != "POST":
            # Stateless: there's no event stream to open (GET) and no session to end (DELETE). A 405 is what the
            # spec says to answer, and what makes Home Assistant stop at Streamable HTTP instead of trying SSE.
            await _error(405, "Method Not Allowed", headers={"Allow": "POST"})(scope, receive, send)
            return

        token = _bearer_token(headers.get("authorization"))
        resource = mcp_url(origin)
        # Home Assistant sends four requests per tool call: the cache answers those after the first without waiting
        # for a worker thread. A lookup uses the database, so it runs in one (PHASE1.md §5).
        principal = cached_mcp_principal(token, resource) if token else None
        if token and principal is None:
            principal = await run_in_threadpool(verify_mcp_token, token, resource)
        if principal is None:
            # RFC 6750 §3.1: `invalid_token` only when a token was sent. Home Assistant reads `resource_metadata` and
            # `scope` from this header to start its OAuth flow.
            challenge = mcp_www_authenticate(origin, "invalid_token" if token else None)
            message = "Unauthorized: the token is invalid, expired or revoked" if token else "Unauthorized"
            await _error(401, message, headers={"WWW-Authenticate": challenge})(scope, receive, send)
            return

        if not self._managers:
            await _error(503, "The MCP server isn't running", code=INTERNAL_ERROR)(scope, receive, send)
            return

        translator = get_locale_provider(headers.get("accept-language"))
        state = McpRequestState(principal=principal, translator=translator, arrived=arrived)
        scope = {**scope, "state": {**scope.get("state", {}), REQUEST_STATE_KEY: state}}
        await self._managers[-1].handle_request(scope, receive, send)
