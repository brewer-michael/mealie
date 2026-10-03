"""
Helpers for the MCP server's tests (docs/ai/PHASE3.md §7). They run the server in-process: httpx's ASGI transport
over the app, with the MCP router's lifespan entered by hand, since `api_client` never runs lifespans.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
import mcp.types as types
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

from mealie.app import app
from mealie.routes.ai import mcp as mcp_routes
from mealie.schema.household.group_shopping_list import ShoppingListItemCreate, ShoppingListOut, ShoppingListSave
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_settings import RecipeSettings
from mealie.schema.recipe.recipe_step import RecipeStep
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser
from tests.utils.mcp_oauth import MCP_URL, ORIGIN, connect, create_client

JSON_RPC_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

UNAUTHORIZED_CHALLENGE = (
    f'Bearer resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/api/mcp", scope="mcp:read mcp:write"'
)
"""`WWW-Authenticate` without a token (RFC 6750 §3.1: no error code)"""
INVALID_TOKEN_CHALLENGE = (
    f'Bearer error="invalid_token", resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/api/mcp", '
    'scope="mcp:read mcp:write"'
)
"""`WWW-Authenticate` for a token that isn't accepted"""

READ_TOOLS = [
    "search_recipes",
    "get_recipe",
    "get_cooking_step",
    "suggest_from_ingredients",
    "whats_planned",
    "get_shopping_list",
]
"""In the registry's order"""
ALL_TOOLS = [*READ_TOOLS, "add_to_shopping_list", "plan_meal"]


@asynccontextmanager
async def mcp_running(target: ASGIApp = app) -> AsyncIterator[None]:
    """The MCP router's lifespan, which runs the session manager"""
    async with mcp_routes.endpoint.lifespan(target):
        yield


def http_client(
    token: str | None = None,
    *,
    target: ASGIApp = app,
    headers: dict[str, str] | None = None,
    event_hooks: dict[str, list[Callable[..., Any]]] | None = None,
) -> httpx.AsyncClient:
    all_headers = {**(headers or {})}
    if token is not None:
        all_headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=target),
        base_url=ORIGIN,
        headers=all_headers,
        timeout=30,
        event_hooks=event_hooks,
    )


@asynccontextmanager
async def mcp_session(
    token: str, *, url: str = MCP_URL, http: httpx.AsyncClient | None = None
) -> AsyncIterator[tuple[ClientSession, types.InitializeResult]]:
    """An initialized MCP SDK client session (the SDK's client is what Home Assistant uses), over `http` if given"""
    async with AsyncExitStack() as stack:
        client = http or await stack.enter_async_context(http_client(token))
        read, write, _ = await stack.enter_async_context(streamable_http_client(url, http_client=client))
        session = await stack.enter_async_context(ClientSession(read, write))
        yield session, await session.initialize()


def rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def tool_call(name: str, arguments: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
    return rpc("tools/call", {"name": name, "arguments": arguments or {}}, request_id)


async def post(client: httpx.AsyncClient, body: Any, path: str = "/api/mcp", **headers: str) -> httpx.Response:
    """One JSON-RPC message, POSTed as a Streamable HTTP client does (stateless: no initialize needed)"""
    return await client.post(path, content=json.dumps(body), headers={**JSON_RPC_HEADERS, **headers})


def payload(result: types.CallToolResult) -> dict[str, Any]:
    """The JSON in a tool result's single text block"""
    assert result.structuredContent is None
    assert len(result.content) == 1 and isinstance(result.content[0], types.TextContent)
    data = json.loads(result.content[0].text)
    assert next(iter(data)) == "speech"
    return data


def raw_payload(response: httpx.Response) -> tuple[dict[str, Any], bool]:
    """The tool result's JSON and `isError` from a raw `tools/call` response"""
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return json.loads(result["content"][0]["text"]), result.get("isError", False)


# ==========================================
# Credentials


def oauth_token(api_client: TestClient, user: TestUser, *, writes: bool = False, name: str | None = None) -> str:
    """An MCP OAuth access token from the real flow, with a write grant if `writes`"""
    client = create_client(api_client, user, name=name or f"Kitchen {random_string(6)}", allowWriteScope=writes)
    return connect(api_client, user, client, allow_writes=writes)["access_token"]


def api_token(api_client: TestClient, user: TestUser) -> tuple[int, str]:
    """A Mealie long-lived API token, made in Profile → API Tokens"""
    response = api_client.post(api_routes.users_api_tokens, json={"name": random_string()}, headers=user.token)
    assert response.status_code == 201
    return response.json()["id"], response.json()["token"]


def set_api_token_writes(api_client: TestClient, user: TestUser, token_id: int, allow: bool) -> None:
    response = api_client.put(
        api_routes.users_self_mcp_api_tokens_token_id(token_id), json={"allowWrites": allow}, headers=user.token
    )
    assert response.status_code == 200, response.text


# ==========================================
# Data


def create_recipe(user: TestUser, steps: list[str] | None = None, **kwargs: Any) -> Recipe:
    return user.repos.recipes.create(
        Recipe(
            user_id=user.user_id,
            group_id=UUID(user.group_id),
            name=f"Dish {random_string()}",
            settings=RecipeSettings(),
            recipe_instructions=[RecipeStep(text=text) for text in steps or []],
            **kwargs,
        )
    )


def create_list(user: TestUser, items: list[str] | None = None) -> ShoppingListOut:
    shopping_list = user.repos.group_shopping_lists.create(
        ShoppingListSave(name=f"List {random_string()}", group_id=UUID(user.group_id), user_id=user.user_id)
    )
    if items:
        user.repos.group_shopping_list_item.create_many(
            [ShoppingListItemCreate(shopping_list_id=shopping_list.id, note=note) for note in items]
        )
    return shopping_list


def rest_result(api_client: TestClient, user: TestUser, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """What the same tool call returns over REST (`/api/ai/tools`)"""
    response = api_client.post(api_routes.ai_tools_name(tool), json=arguments, headers=user.token)
    assert response.status_code == 200, response.text
    return response.json()["result"]
