"""
The MCP endpoint at `/api/mcp` (docs/ai/PHASE3.md §1-2, §7): methods, `Origin`, bearer authentication and its 401,
the handshake, running in the app's lifespan, request size, logging, the time limit, and staying off the event loop
under load.
"""

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import mcp.types as types
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event

import mealie.app as mealie_app
from mealie.app import app
from mealie.core.settings.static import APP_VERSION
from mealie.db import init_db
from mealie.db.db_setup import engine, session_context
from mealie.db.models.ai_mcp import McpOAuthToken
from mealie.routes.ai import mcp as mcp_routes
from mealie.services.ai.mcp import endpoint as mcp_endpoint
from mealie.services.ai.mcp import tool_bridge
from mealie.services.ai.mcp.auth import clear_mcp_principal_cache, verify_mcp_token
from mealie.services.ai.tools import ToolContext, get_tool, registry
from mealie.services.oauth.tokens import hash_secret
from tests.integration_tests.ai_tests.mcp.mcp_helpers import (
    INVALID_TOKEN_CHALLENGE,
    JSON_RPC_HEADERS,
    UNAUTHORIZED_CHALLENGE,
    api_token,
    create_list,
    create_recipe,
    http_client,
    mcp_running,
    mcp_session,
    oauth_token,
    post,
    raw_payload,
    rpc,
    tool_call,
)
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser
from tests.utils.mcp_oauth import MCP_URL, ORIGIN, connect, create_client

INITIALIZE = rpc(
    "initialize",
    {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
)


@pytest.fixture(scope="module")
def token(api_client: TestClient, unique_user: TestUser) -> str:
    return oauth_token(api_client, unique_user)


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def server_side(self) -> list[str]:
        """What the server logged: Mealie through the root logger (as the SDK's warnings do), and the SDK's server"""
        return [r.getMessage() for r in self.records if r.name == "root" or r.name.startswith("mcp.server")]


@pytest.fixture
def logs() -> Generator[Records]:
    """Everything logged at INFO and above, as in production"""
    records = Records()
    root = logging.getLogger()
    level = root.level
    root.addHandler(records)
    root.setLevel(logging.INFO)
    try:
        yield records
    finally:
        root.removeHandler(records)
        root.setLevel(level)


# ==========================================
# Methods and paths


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH", "OPTIONS"])
def test_only_post_is_allowed(token: str, method: str):
    """Stateless: no event stream (GET) and no session to end (DELETE). Nothing falls through to the SPA."""

    async def scenario() -> httpx.Response:
        async with mcp_running(), http_client(token) as http:
            return await http.request(method, "/api/mcp", headers={"Accept": "text/event-stream"})

    response = asyncio.run(scenario())
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["message"] == "Method Not Allowed"


def test_trailing_slash(token: str):
    """A typed trailing slash reaches the endpoint too (Home Assistant doesn't follow redirects)"""

    async def scenario() -> types.InitializeResult:
        async with mcp_running(), mcp_session(token, url=f"{MCP_URL}/") as (_, init):
            return init

    assert asyncio.run(scenario()).serverInfo.name == "Mealie"


def test_not_running(token: str):
    """Without the lifespan (e.g. `TestClient(app)` without `with`) authentication still answers, then a 503"""

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with http_client() as http:
            unauthorized = await post(http, INITIALIZE)
            authorized = await post(http, INITIALIZE, Authorization=f"Bearer {token}")
            return unauthorized, authorized

    unauthorized, authorized = asyncio.run(scenario())
    assert unauthorized.status_code == 401
    assert authorized.status_code == 503


# ==========================================
# Origin


@pytest.mark.parametrize(
    "origin, status_code",
    [
        (None, 200),
        (ORIGIN, 200),
        ("HTTP://TestServer:80", 200),
        ("http://evil.example", 403),
        ("https://testserver", 403),
        ("http://testserver:8080", 403),
        ("null", 403),
        ("http://testserver.evil.example", 403),
    ],
)
def test_origin(token: str, origin: str | None, status_code: int):
    """An `Origin` that isn't this server is refused with a 403, before authentication (spec: DNS rebinding)"""

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        headers = {"Origin": origin} if origin else {}
        async with mcp_running(), http_client(token) as http, http_client() as anonymous:
            return await post(http, INITIALIZE, **headers), await post(anonymous, INITIALIZE, **headers)

    response, anonymous = asyncio.run(scenario())
    assert response.status_code == status_code, response.text
    assert anonymous.status_code == (401 if status_code == 200 else 403)
    if status_code == 403:
        assert "www-authenticate" not in anonymous.headers


# ==========================================
# Authentication


def test_no_token(api_client: TestClient, unique_user: TestUser):
    """Home Assistant's first contact: `initialize` without a token must get a 401 it can start OAuth from"""

    async def scenario() -> list[httpx.Response]:
        async with mcp_running(), http_client() as http:
            return [
                await post(http, INITIALIZE),
                await post(http, INITIALIZE, path="/api/mcp/"),
                await post(http, INITIALIZE, Authorization="Bearer"),
                await post(http, INITIALIZE, Authorization="Basic dXNlcjpwYXNz"),
            ]

    for response in asyncio.run(scenario()):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == UNAUTHORIZED_CHALLENGE
        assert response.json()["error"]["message"] == "Unauthorized"


def test_rejected_tokens(api_client: TestClient, unique_user: TestUser):
    expired = oauth_token(api_client, unique_user)
    with session_context() as session:
        session.execute(
            sa.update(McpOAuthToken)
            .where(McpOAuthToken.token_hash == hash_secret(expired))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()

    session_token = unique_user.token["Authorization"].removeprefix("Bearer ")
    _, api = api_token(api_client, unique_user)
    tokens = {
        "garbage": "nope",
        "expired": expired,
        "session JWT": session_token,
        # a refresh token or a client secret is never a bearer token
        "refresh token": "mmcp_rt_" + "x" * 43,
        "API token with a stray suffix": api + "x",
    }

    clear_mcp_principal_cache()

    async def scenario() -> dict[str, httpx.Response]:
        async with mcp_running(), http_client() as http:
            return {name: await post(http, INITIALIZE, Authorization=f"Bearer {t}") for name, t in tokens.items()}

    responses = asyncio.run(scenario())
    for name, response in responses.items():
        assert response.status_code == 401, name
        assert response.headers["www-authenticate"] == INVALID_TOKEN_CHALLENGE, name


def test_revoked_token(api_client: TestClient, unique_user: TestUser):
    """A revocation applies at once, although the token's verification was cached"""
    client = create_client(api_client, unique_user, name=f"Revocable {random_string(6)}")
    tokens = connect(api_client, unique_user, client)

    async def initialize() -> httpx.Response:
        async with mcp_running(), http_client(tokens["access_token"]) as http:
            return await post(http, INITIALIZE)

    assert asyncio.run(initialize()).status_code == 200
    response = api_client.post(
        api_routes.oauth_revoke,
        data={
            "token": tokens["access_token"],
            "client_id": client["clientId"],
            "client_secret": client["clientSecret"],
        },
    )
    assert response.status_code == 200
    response = asyncio.run(initialize())
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == INVALID_TOKEN_CHALLENGE


def test_session_cookies_are_ignored(api_client: TestClient, unique_user: TestUser):
    """A browser session can't drive MCP: the cookie isn't a bearer token, and the session JWT isn't accepted"""
    session_token = unique_user.token["Authorization"].removeprefix("Bearer ")

    async def scenario() -> httpx.Response:
        async with mcp_running(), http_client() as http:
            http.cookies.set("mealie.access_token", session_token)
            return await post(http, INITIALIZE)

    response = asyncio.run(scenario())
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == UNAUTHORIZED_CHALLENGE


def test_challenge_follows_the_request_origin(api_client: TestClient, unique_user: TestUser):
    """The 401 points at the metadata of the server as the client reached it, which is what HA compares"""
    token = oauth_token(api_client, unique_user)

    async def scenario() -> list[httpx.Response]:
        async with mcp_running(), http_client() as http:
            return [
                await post(http, INITIALIZE, Host="192.168.1.20:9925"),
                # an OAuth token is only valid at the server it was issued for (audience)
                await post(http, INITIALIZE, Host="mealie.example", Authorization=f"Bearer {token}"),
            ]

    lan, other_host = asyncio.run(scenario())
    assert lan.headers["www-authenticate"] == (
        'Bearer resource_metadata="http://192.168.1.20:9925/.well-known/oauth-protected-resource/api/mcp", '
        'scope="mcp:read mcp:write"'
    )
    assert other_host.status_code == 401
    assert 'error="invalid_token"' in other_host.headers["www-authenticate"]
    assert 'resource_metadata="http://mealie.example/' in other_host.headers["www-authenticate"]


def test_cached_tokens_need_no_worker_thread(token: str, monkeypatch: pytest.MonkeyPatch):
    """
    Home Assistant sends four requests per tool call: only the first waits for a worker thread to verify the token,
    which can take long when the threads are busy with other requests
    """
    loop_thread = threading.get_ident()
    verified_in: list[int] = []

    def tracked(token: str, resource: str) -> Any:
        verified_in.append(threading.get_ident())
        return verify_mcp_token(token, resource)

    monkeypatch.setattr(mcp_endpoint, "verify_mcp_token", tracked)
    clear_mcp_principal_cache()

    async def scenario() -> list[str]:
        async with mcp_running(), mcp_session(token) as (session, _):
            return [tool.name for tool in (await session.list_tools()).tools]

    assert asyncio.run(scenario())
    assert len(verified_in) == 1 and verified_in[0] != loop_thread


def test_api_tokens_are_accepted(api_client: TestClient, unique_user: TestUser):
    _, token = api_token(api_client, unique_user)

    async def scenario() -> types.InitializeResult:
        async with mcp_running(), mcp_session(token) as (_, init):
            return init

    assert asyncio.run(scenario()).serverInfo.name == "Mealie"


# ==========================================
# Handshake


def test_initialize(token: str):
    async def scenario() -> types.InitializeResult:
        async with mcp_running(), mcp_session(token) as (_, init):
            return init

    init = asyncio.run(scenario())
    assert (init.serverInfo.name, init.serverInfo.version) == ("Mealie", APP_VERSION)
    assert init.protocolVersion == types.LATEST_PROTOCOL_VERSION
    assert init.capabilities.tools is not None
    assert init.capabilities.prompts is None and init.capabilities.resources is None
    assert init.capabilities.logging is None and init.capabilities.completions is None


def test_initialize_and_list_dont_touch_the_database(token: str):
    """Once the token is verified (and cached), the handshake and `tools/list` run no queries at all"""
    statements: list[str] = []

    def record(conn, cursor, statement: str, parameters, context, executemany) -> None:
        statements.append(statement)

    async def scenario() -> list[str]:
        async with mcp_running(), http_client(token) as http:
            assert (await post(http, INITIALIZE)).status_code == 200  # verifies and caches the token
            event.listen(engine, "before_cursor_execute", record)
            try:
                async with mcp_session(token, http=http) as (session, _):
                    return [tool.name for tool in (await session.list_tools()).tools]
            finally:
                event.remove(engine, "before_cursor_execute", record)

    assert asyncio.run(scenario())
    assert statements == []


def test_the_app_lifespan_runs_the_mcp_server(token: str, monkeypatch: pytest.MonkeyPatch):
    """The router's lifespan is merged into Mealie's app through `include_router`, with no change to app.py"""

    async def no_scheduler() -> None:
        return None

    monkeypatch.setattr(init_db, "main", lambda: None)
    monkeypatch.setattr(mealie_app, "start_scheduler", no_scheduler)

    async def scenario() -> tuple[int, int, int]:
        async with app.router.lifespan_context(app):
            running = len(mcp_routes.endpoint._managers)
            async with http_client(token) as http:
                status_code = (await post(http, INITIALIZE)).status_code
        return running, status_code, len(mcp_routes.endpoint._managers)

    assert asyncio.run(scenario()) == (1, 200, 0)


# ==========================================
# Off the event loop


def test_concurrent_calls_dont_block_the_event_loop(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """
    More simultaneous tool calls than the connection pool holds (15 by default) all succeed, and no query runs on
    the event loop: not the token lookups (several tokens, nothing cached), and not the tools' own (PHASE1.md §5)
    """
    recipe = create_recipe(unique_user)
    _, api = api_token(api_client, unique_user)
    tokens = [api, *(oauth_token(api_client, unique_user) for _ in range(3))]
    calls = [
        ("whats_planned", {}),
        ("search_recipes", {"query": recipe.name}),
        ("get_recipe", {"slug": recipe.slug}),
        ("get_cooking_step", {"slug": recipe.slug, "step": 1}),  # not found, after reading the recipe
    ] * 7

    # fail a blocked checkout after a few seconds instead of 30
    monkeypatch.setattr(engine.pool, "_timeout", 3)
    # all of them run, rather than some being turned away as busy
    monkeypatch.setattr(tool_bridge, "MAX_RUNNING_TOOLS", len(calls))
    clear_mcp_principal_cache()

    async def burst() -> list[httpx.Response | BaseException]:
        async with mcp_running(), http_client() as http:
            requests = [
                post(http, tool_call(tool, args, i), Authorization=f"Bearer {tokens[i % len(tokens)]}")
                for i, (tool, args) in enumerate(calls)
            ]
            return await asyncio.gather(*requests, return_exceptions=True)

    loop_thread = threading.get_ident()
    on_the_loop: list[str] = []

    def record(conn, cursor, statement: str, parameters, context, executemany) -> None:
        if threading.get_ident() == loop_thread:
            on_the_loop.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        responses = asyncio.run(burst())
    finally:
        event.remove(engine, "before_cursor_execute", record)

    outcomes = []
    for (tool, _), response in zip(calls, responses, strict=True):
        assert isinstance(response, httpx.Response), repr(response)
        data, is_error = raw_payload(response)
        outcomes.append((tool, data.get("error"), is_error))
    assert outcomes == [
        (tool, "not_found", True) if tool == "get_cooking_step" else (tool, None, False) for tool, _ in calls
    ]
    assert on_the_loop == []


# ==========================================
# Request size and logging


def test_request_size_limit(token: str):
    """
    The largest tool call fits, even with every character escaped; anything much bigger is refused before it's read,
    so it can't fill the log through the SDK's warnings, which quote what they couldn't parse
    """
    largest = tool_call("add_to_shopping_list", {"items": ["é" * 200] * 50, "list_name": "x" * 100})

    async def scenario() -> list[httpx.Response]:
        async with mcp_running(), http_client(token) as http:
            return [
                await post(http, largest),
                await post(http, tool_call("search_recipes", {"query": "x" * 100_000})),
                await post(
                    http, {"jsonrpc": "2.0", "method": "notifications/whatever", "params": {"x": "x" * 100_000}}
                ),
            ]

    fits, *too_big = asyncio.run(scenario())
    assert len(json.dumps(largest)) > 60_000
    assert raw_payload(fits)[0]["error"] == "write_not_allowed"
    assert [response.status_code for response in too_big] == [413, 413]


def test_a_tool_call_logs_one_line(api_client: TestClient, unique_user: TestUser, token: str, logs: Records):
    """Home Assistant's four POSTs log Mealie's line for the call, and nothing from the SDK"""
    shopping_list = create_list(unique_user, ["eggs"])

    async def scenario() -> list[str]:
        async with mcp_running():
            logs.records.clear()
            async with mcp_session(token) as (session, _):
                await session.call_tool("get_shopping_list", {"list_name": shopping_list.name})
                await session.list_tools()
            return logs.server_side()

    [line] = asyncio.run(scenario())
    assert re.fullmatch(r"MCP tool get_shopping_list for user [0-9a-f-]{36} via 'Kitchen \w+': ok \(\d+ ms\)", line)


def test_hostile_input_stays_out_of_the_logs(token: str, logs: Records):
    """A tool name can't write lines of its own, or much of anything, to the log"""
    forged = "x\nERROR 2026-10-03T00:00:00 - forged line\n" + "A" * 50_000

    async def scenario() -> tuple[list[tuple[dict, bool]], list[str]]:
        async with mcp_running(), http_client(token) as http:
            logs.records.clear()
            unknown = raw_payload(await post(http, tool_call(forged)))
            refused = raw_payload(await post(http, tool_call("plan_meal", {"date": "2030-01-01"})))
            return [unknown, refused], logs.server_side()

    [(unknown, _), (refused, _)], lines = asyncio.run(scenario())
    assert unknown["error"] == "unknown_tool" and refused["error"] == "write_not_allowed"
    assert len(lines) == 2
    assert lines[0].startswith("MCP tool 'x\\nERROR 2026-10-03T00:00:00 - forged line\\nAAA")
    assert lines[1].startswith("MCP tool plan_meal for user ") and lines[1].endswith(
        ": refused (no write grant) (0 ms)"
    )
    assert all("\n" not in line and len(line) < 300 for line in lines)


# ==========================================
# Time limit


def test_the_time_limit_counts_from_arrival(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Time spent before the tool runs (here, a slow token lookup) comes out of the tool's time, not on top of it"""
    token = oauth_token(api_client, unique_user)
    finished: list[bool] = []

    def slow_verification(token: str, resource: str) -> Any:
        time.sleep(0.6)
        return verify_mcp_token(token, resource)

    async def slow_tool(ctx: ToolContext, args: Any) -> Any:
        await asyncio.sleep(1)
        finished.append(True)

    tool = get_tool("whats_planned")
    assert tool is not None
    monkeypatch.setitem(registry._TOOLS, "whats_planned", replace(tool, handler=slow_tool))
    monkeypatch.setattr(mcp_endpoint, "verify_mcp_token", slow_verification)
    monkeypatch.setattr(tool_bridge, "TOOL_TIMEOUT", 0.9)
    monkeypatch.setattr(tool_bridge, "MIN_TOOL_TIME", 0.1)
    clear_mcp_principal_cache()

    async def scenario() -> tuple[httpx.Response, float]:
        async with mcp_running(), http_client(token) as http:
            started = time.perf_counter()
            response = await http.post(
                "/api/mcp", content=json.dumps(tool_call("whats_planned")), headers=JSON_RPC_HEADERS
            )
            return response, time.perf_counter() - started

    response, elapsed = asyncio.run(scenario())
    assert raw_payload(response) == ({"speech": "Mealie took too long to answer. Try again.", "error": "timeout"}, True)
    # 0.6 s verifying and 0.3 s left for the tool, rather than 0.6 + 0.9
    assert 0.85 <= elapsed < 1.3
    assert finished == [True]  # the lifespan waited for it
