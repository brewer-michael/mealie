"""
Discovery for the MCP server (docs/ai/PHASE3.md §4 "Metadata"): RFC 9728 and RFC 8414 documents, and a JSON 404 for
every other `/.well-known` path, which must win over the production SPA's catch-all.
"""

import asyncio
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mealie.app as mealie_app
from mealie.core.config import get_app_settings
from mealie.routes import spa

ORIGIN = "http://testserver"

PROTECTED_RESOURCE = {
    "resource": f"{ORIGIN}/api/mcp",
    "authorization_servers": [ORIGIN],
    "scopes_supported": ["mcp:read", "mcp:write"],
    "bearer_methods_supported": ["header"],
    "resource_name": "Mealie",
}

AUTHORIZATION_SERVER = {
    "issuer": ORIGIN,
    "authorization_endpoint": f"{ORIGIN}/api/oauth/authorize",
    "token_endpoint": f"{ORIGIN}/api/oauth/token",
    "revocation_endpoint": f"{ORIGIN}/api/oauth/revoke",
    "scopes_supported": ["mcp:read", "mcp:write"],
    "response_types_supported": ["code"],
    "response_modes_supported": ["query"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
    "revocation_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
    "authorization_response_iss_parameter_supported": True,
}

NOT_FOUND_PATHS = [
    "/.well-known/openid-configuration",
    "/.well-known/openid-configuration/api/mcp",
    "/.well-known/oauth-protected-resource/api/other",
    "/.well-known/oauth-authorization-server/api/other",
    "/.well-known/oauth-protected-resource/",
    "/.well-known/security.txt",
    "/.well-known/",
]


@pytest.mark.parametrize(
    "path, document",
    [
        ("/.well-known/oauth-protected-resource/api/mcp", PROTECTED_RESOURCE),
        ("/.well-known/oauth-protected-resource", PROTECTED_RESOURCE),
        ("/.well-known/oauth-authorization-server", AUTHORIZATION_SERVER),
        ("/.well-known/oauth-authorization-server/api/mcp", AUTHORIZATION_SERVER),
    ],
)
def test_metadata_documents(api_client: TestClient, path: str, document: dict):
    response = api_client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == document

    assert api_client.head(path).status_code == 200


def test_metadata_follows_the_request_origin(api_client: TestClient):
    """The issuer and resource are what the client reached: Home Assistant compares `resource` to what was typed"""
    response = api_client.get(
        "https://Mealie.Example.com/.well-known/oauth-protected-resource/api/mcp",
        headers={"Host": "Mealie.Example.com"},
    )
    assert response.json()["resource"] == "https://mealie.example.com/api/mcp"
    assert response.json()["authorization_servers"] == ["https://mealie.example.com"]

    response = api_client.get("/.well-known/oauth-authorization-server", headers={"Host": "192.168.1.20:9925"})
    assert response.json()["issuer"] == "http://192.168.1.20:9925"
    assert response.json()["token_endpoint"] == "http://192.168.1.20:9925/api/oauth/token"


@pytest.mark.parametrize("path", NOT_FOUND_PATHS)
def test_other_well_known_paths_are_json_404s(api_client: TestClient, path: str):
    response = api_client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "Not Found"}

    assert api_client.head(path).status_code == 404


# ==========================================
# Under the production SPA mount


@pytest.fixture()
def production_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[FastAPI]:
    """
    A fresh app wired by `mealie.app.api_routers` exactly as in production, SPA catch-all included: `PRODUCTION` on,
    `TESTING` off and a built frontend in `STATIC_FILES`
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
    # mount_spa replaces the SPA module's HTML; put it back afterwards
    monkeypatch.setattr(spa, "__contents", getattr(spa, "__contents"))

    app = FastAPI()
    monkeypatch.setattr(mealie_app, "app", app)
    mealie_app.api_routers()

    assert any(getattr(route, "name", None) == "spa" for route in app.routes), "the SPA wasn't mounted"
    yield app


def test_json_404_wins_over_the_spa(production_app: FastAPI):
    client = TestClient(production_app)

    # the SPA answers unknown paths with its HTML shell and a 200...
    response = client.get("/some/page")
    assert response.status_code == 200
    assert "Mealie SPA shell" in response.text

    # ...but never a /.well-known path
    for path in NOT_FOUND_PATHS:
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found"}

    assert client.get("/.well-known/oauth-protected-resource/api/mcp").json() == PROTECTED_RESOURCE
    assert client.get("/.well-known/oauth-authorization-server").json() == AUTHORIZATION_SERVER


def test_home_assistant_discovery_under_the_spa(production_app: FastAPI):
    """
    Home Assistant's discovery (homeassistant/components/mcp/config_flow.py): it fetches the candidate URLs at once
    and parses the first 2xx as JSON, so a 200 from the SPA would break it. Every candidate either serves the
    document or is a JSON 404.
    """

    async def first_2xx(http: httpx.AsyncClient, urls: list[str]) -> httpx.Response:
        tasks = [asyncio.ensure_future(http.get(url)) for url in urls]
        try:
            for future in asyncio.as_completed(tasks):
                response = await future
                if response.is_success:
                    return response
                assert response.status_code == 404 and response.json() == {"detail": "Not Found"}, response.url
        finally:
            for task in tasks:
                task.cancel()
        raise AssertionError("no 2xx")

    async def discover() -> tuple[dict, dict]:
        transport = httpx.ASGITransport(app=production_app)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as http:
            prm = await first_2xx(
                http,
                [
                    f"{ORIGIN}/.well-known/oauth-protected-resource/api/mcp",  # from WWW-Authenticate
                    f"{ORIGIN}/.well-known/oauth-protected-resource/api/mcp",  # path insertion
                    f"{ORIGIN}/.well-known/oauth-protected-resource",
                ],
            )
            assert prm.json()["resource"] == f"{ORIGIN}/api/mcp"  # what the user typed
            issuer = prm.json()["authorization_servers"][0]
            asm = await first_2xx(
                http,
                [f"{issuer}/.well-known/oauth-authorization-server", f"{issuer}/.well-known/openid-configuration"],
            )
            return prm.json(), asm.json()

    for _ in range(5):
        prm, asm = asyncio.run(discover())
        assert prm == PROTECTED_RESOURCE
        assert asm == AUTHORIZATION_SERVER
