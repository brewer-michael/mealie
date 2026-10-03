"""
Home Assistant's MCP client end to end (docs/ai/PHASE3.md §7), replayed from its documented behaviour (research for
PHASE3.md, "HA's MCP client") against the app as production wires it, with the SPA's catch-all mounted:

1. `initialize` without a token gets a 401, whose `WWW-Authenticate` gives the metadata URL and the scopes;
2. protected resource metadata: three URLs fetched at once, the first 2xx wins, and its `resource` must be exactly
   the URL the user typed;
3. authorization server metadata: two URLs at once, the first 2xx wins (the SPA must never answer with a 200);
4. authorize with HA's parameters (no PKCE, no `resource`, a long `state`), the user consents in Mealie;
5. the code is exchanged with `client_secret_post`; the response has `expires_in` and a refresh token;
6. each tool call is four POSTs (initialize, initialized, tools/call, tools/list) with the access token;
7. refresh (rotating), then revocation: the next call gets a 401, and the refresh a 4xx, so HA asks to reauthorize.
"""

import asyncio
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

import mealie.app as mealie_app
from mealie.core.config import get_app_settings
from mealie.routes import spa
from mealie.routes.ai import mcp as mcp_routes
from tests.integration_tests.ai_tests.mcp.mcp_helpers import (
    create_list,
    http_client,
    mcp_session,
    payload,
)
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

ORIGIN = "http://testserver"
TYPED_URL = f"{ORIGIN}/api/mcp"
"""What the user types into Home Assistant"""
HA_URL = "http://homeassistant.local:8123"
REDIRECT_URI = "https://my.home-assistant.io/redirect/oauth"
STATE = "eyJhbGciOiJIUzI1NiJ9." + "eyJmbG93X2lkIjoiMDFKOVhZWiIsInJlZGlyZWN0X3VyaSI6Im15In0" * 6 + ".c2lnbmF0dXJl"
"""HA's state is a signed JWT, much longer than most"""

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "mcp", "version": "0.1.0"}},
}


@pytest.fixture()
def production_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[FastAPI]:
    """
    A fresh app wired by `mealie.app.api_routers` as in production, with the SPA's catch-all and the middleware
    production adds (CORS is development only)
    """
    shell = "<!doctype html><html><head></head><body>Mealie SPA shell</body></html>"
    (tmp_path / "index.html").write_text(shell)
    (tmp_path / "404.html").write_text(shell)

    # The settings `mealie.app` and the SPA module read: some tests replace `get_app_settings()`'s since they were
    # imported (by clearing its cache), so these can be other objects than the current one
    settings_objects = {id(s): s for s in (get_app_settings(), mealie_app.settings, getattr(spa, "__app_settings"))}
    for settings in settings_objects.values():
        monkeypatch.setattr(settings, "STATIC_FILES", str(tmp_path))
        monkeypatch.setattr(settings, "PRODUCTION", True)
        monkeypatch.setattr(settings, "TESTING", False)
    monkeypatch.setattr(spa, "__contents", getattr(spa, "__contents"))

    app = FastAPI()
    app.user_middleware = [m for m in mealie_app.app.user_middleware if m.cls is not CORSMiddleware]
    assert app.user_middleware, "the production middleware wasn't found"
    monkeypatch.setattr(mealie_app, "app", app)
    mealie_app.api_routers()

    assert any(getattr(route, "name", None) == "spa" for route in app.routes), "the SPA wasn't mounted"
    yield app


async def first_success(http: httpx.AsyncClient, urls: list[str]) -> httpx.Response:
    """
    GETs every URL at once and returns the first 2xx to arrive, as HA does. Every other answer must be a JSON 404:
    an HTML 200 from the SPA could win the race.
    """
    responses = [await next_done for next_done in asyncio.as_completed([http.get(url) for url in urls])]
    for response in responses:
        if not response.is_success:
            assert response.status_code == 404, (response.url, response.status_code)
            assert response.json() == {"detail": "Not Found"}, (response.url, response.text[:200])

    success = next((response for response in responses if response.is_success), None)
    assert success is not None, f"no 2xx from {urls}"
    return success


def query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


def test_home_assistant(production_app: FastAPI, api_client: TestClient, unique_user: TestUser):
    # A group manager adds Home Assistant as an app (the preset, allowed to ask for changes); the user copies the
    # client ID and secret into HA's Application Credentials
    preset = api_client.get(
        api_routes.groups_mcp_presets_home_assistant, params={"homeAssistantUrl": HA_URL}, headers=unique_user.token
    ).json()
    response = api_client.post(
        api_routes.groups_mcp_clients,
        json={**preset, "name": f"Home Assistant {random_string(6)}", "allowWriteScope": True},
        headers=unique_user.token,
    )
    assert response.status_code == 201, response.text
    client = response.json()
    assert REDIRECT_URI in client["redirectUris"] and f"{HA_URL}/auth/external/callback" in client["redirectUris"]

    shopping_list = create_list(unique_user)
    sent: list[str] = []

    async def record(request: httpx.Request) -> None:
        if request.url.path == "/api/mcp":
            sent.append(re.search(rb'"method":\s*"([^"]+)"', request.content).group(1).decode())  # type: ignore[union-attr]

    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        # the app's own lifespan: the MCP router's is merged into it by include_router
        async with production_app.router.lifespan_context(production_app):
            assert len(mcp_routes.endpoint._managers) == 1

            async with http_client(target=production_app) as http:
                # 1. first contact
                response = await http.post(
                    TYPED_URL, json=INITIALIZE, headers={"Accept": "application/json, text/event-stream"}
                )
                assert response.status_code == 401
                challenge = response.headers["www-authenticate"]
                metadata_url = re.search(r'resource_metadata="([^"]+)"', challenge).group(1)  # type: ignore[union-attr]
                scope = re.search(r'scope="([^"]+)"', challenge).group(1)  # type: ignore[union-attr]

                # 2. protected resource metadata
                prm = await first_success(
                    http,
                    [
                        metadata_url,
                        f"{ORIGIN}/.well-known/oauth-protected-resource/api/mcp",
                        f"{ORIGIN}/.well-known/oauth-protected-resource",
                    ],
                )
                assert prm.json()["resource"] == TYPED_URL
                issuer = prm.json()["authorization_servers"][0]

                # 3. authorization server metadata (the issuer has no path)
                asm = (
                    await first_success(
                        http,
                        [
                            f"{issuer}/.well-known/oauth-authorization-server",
                            f"{issuer}/.well-known/openid-configuration",
                        ],
                    )
                ).json()

                # 4. authorize, then the user consents in Mealie, allowing changes
                response = await http.get(
                    asm["authorization_endpoint"],
                    params={
                        "response_type": "code",
                        "client_id": client["clientId"],
                        "redirect_uri": REDIRECT_URI,
                        "state": STATE,
                        "access_type": "offline",
                        "prompt": "consent",
                        "scope": scope,
                    },
                )
                assert response.status_code == 302
                consent_page = response.headers["location"]
                assert consent_page.startswith("/oauth/consent?request=")
                page = await http.get(consent_page)
                assert page.status_code == 200 and "Mealie SPA shell" in page.text

                handle = query(consent_page)["request"]
                response = await http.post(
                    f"/api/oauth/requests/{handle}",
                    json={"approve": True, "allowWrites": True},
                    headers=unique_user.token,
                )
                assert response.status_code == 200, response.text
                callback = response.json()["redirectTo"]
                assert callback.startswith(REDIRECT_URI + "?")
                assert query(callback)["state"] == STATE

                # 5. code exchange, client_secret_post
                credentials = {"client_id": client["clientId"], "client_secret": client["clientSecret"]}
                response = await http.post(
                    asm["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": query(callback)["code"],
                        "redirect_uri": REDIRECT_URI,
                        **credentials,
                    },
                )
                assert response.status_code == 200, response.text
                tokens = response.json()
                assert tokens["expires_in"] > 0 and tokens["refresh_token"]
                assert tokens["scope"] == "mcp:read mcp:write"

            # 6. a tool call, the way HA's coordinator makes one: a new session per call
            async with (
                http_client(tokens["access_token"], target=production_app, event_hooks={"request": [record]}) as http,
                mcp_session(tokens["access_token"], url=TYPED_URL, http=http) as (session, init),
            ):
                seen["server"] = init.serverInfo.name
                result = await session.call_tool(
                    "add_to_shopping_list", {"items": ["oat milk"], "list_name": shopping_list.name}
                )
                seen["call"] = (result.isError, payload(result)["added"])
            seen["posts"] = list(sent)

            async with http_client(target=production_app) as http:
                # 7. refresh: a new refresh token each time, which HA merges into what it has
                response = await http.post(
                    asm["token_endpoint"],
                    data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], **credentials},
                )
                assert response.status_code == 200, response.text
                refreshed = response.json()
                assert refreshed["refresh_token"] != tokens["refresh_token"]
                tokens = {**tokens, **refreshed}

            async with (
                http_client(tokens["access_token"], target=production_app) as http,
                mcp_session(tokens["access_token"], url=TYPED_URL, http=http) as (session, _),
            ):
                result = await session.call_tool("get_shopping_list", {"list_name": shopping_list.name})
                seen["after_refresh"] = [item["text"] for item in payload(result)["items"]]

            # ...and revoked, e.g. on removing the integration
            async with http_client(target=production_app) as http:
                response = await http.post(
                    asm["revocation_endpoint"], data={"token": tokens["refresh_token"], **credentials}
                )
                assert response.status_code == 200

                response = await http.post(
                    TYPED_URL,
                    json=INITIALIZE,
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Authorization": f"Bearer {tokens['access_token']}",
                    },
                )
                seen["after_revoke"] = (response.status_code, response.headers.get("www-authenticate"))

                response = await http.post(
                    asm["token_endpoint"],
                    data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], **credentials},
                )
                seen["refresh_after_revoke"] = response.status_code

        assert mcp_routes.endpoint._managers == []
        return seen

    seen = asyncio.run(scenario())
    assert seen["server"] == "Mealie"
    assert seen["call"] == (False, 1)
    assert seen["posts"] == ["initialize", "notifications/initialized", "tools/call", "tools/list"]
    assert seen["after_refresh"] == ["oat milk"]
    status_code, challenge = seen["after_revoke"]
    assert status_code == 401 and challenge.startswith('Bearer error="invalid_token", resource_metadata="')
    assert 400 <= seen["refresh_after_revoke"] < 500
