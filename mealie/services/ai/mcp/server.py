"""
The MCP server (docs/ai/PHASE3.md §1): the `mcp` SDK's low-level `Server`, named "Mealie", offering the AI tool
registry and nothing else (capability `tools` only).

The low-level server rather than FastMCP, because FastMCP builds schemas from function signatures and Mealie serves
the registry's own. `McpEndpoint` (`endpoint.py`) authenticates every request before it gets here and leaves who is
calling in the request's scope; neither `initialize` nor `tools/list` touches the database.
"""

import logging
from dataclasses import dataclass
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from starlette.requests import Request

from mealie.core.settings.static import APP_VERSION
from mealie.lang.providers import Translator

from . import tool_bridge
from .auth import McpPrincipal

SERVER_NAME = "Mealie"
"""Home Assistant names the connection after it, and prefixes the tools with it (`mealie__…`) when it combines
several tool sources"""

REQUEST_STATE_KEY = "mealie_mcp"
"""Where `McpEndpoint` leaves the `McpRequestState` in the request's `scope["state"]`"""

# The SDK logs at INFO for every POST and every request it handles, six lines for one Home Assistant tool call;
# `tool_bridge` logs one per call instead. A level Mealie's log configuration sets for these is kept.
for _name in ("mcp.server.lowlevel.server", "mcp.server.streamable_http"):
    if logging.getLogger(_name).level == logging.NOTSET:
        logging.getLogger(_name).setLevel(logging.WARNING)


@dataclass(frozen=True)
class McpRequestState:
    """Who is calling, set by `McpEndpoint` once the bearer token checks out"""

    principal: McpPrincipal
    translator: Translator
    arrived: float
    """When the request came in (`time.monotonic()`): a tool gets what's left of `tool_bridge.TOOL_TIMEOUT`"""


def request_state(request: Request | None) -> McpRequestState:
    """The state `McpEndpoint` attached to the HTTP request an MCP message came in"""
    state = request.scope.get("state", {}).get(REQUEST_STATE_KEY) if request is not None else None
    if not isinstance(state, McpRequestState):
        # only reachable by serving the session manager without `McpEndpoint` in front of it
        raise RuntimeError("MCP request without an authenticated caller")
    return state


def create_mcp_server() -> Server[Any, Request]:
    server: Server[Any, Request] = Server(SERVER_NAME, version=APP_VERSION)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tool_bridge.list_tools(request_state(server.request_context.request).principal)

    async def call_tool(request: types.CallToolRequest) -> types.ServerResult:
        state = request_state(server.request_context.request)
        params = request.params
        result = await tool_bridge.call_tool(
            state.principal, params.name, params.arguments or {}, state.translator, state.arrived
        )
        return types.ServerResult(result)

    # Registered directly, not with `@server.call_tool()`: the tool's pydantic model validates the arguments, as over
    # REST, and the decorator would look each name up in a listing it reruns for names it hasn't seen (logging any
    # unknown one verbatim), and answer an exception with its message
    server.request_handlers[types.CallToolRequest] = call_tool

    return server
