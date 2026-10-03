"""Helpers for the MCP OAuth tests (docs/ai/PHASE3.md §4): register clients and walk the authorization code flow"""

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from fastapi.testclient import TestClient

from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser

ORIGIN = "http://testserver"
"""What `TestClient` requests carry as their origin"""
MCP_URL = f"{ORIGIN}/api/mcp"
HA_REDIRECT_URI = "https://my.home-assistant.io/redirect/oauth"
HA_LOCAL_REDIRECT_URI = "http://homeassistant.local:8123/auth/external/callback"
HA_STATE = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    + "eyJmbG93X2lkIjoiMDFKQUJDREVGIiwicmVkaXJlY3RfdXJpIjoiaHR0cHM6Ly9teS5ob21lLWFzc2lzdGFudC5pby9yZWRpcmVjdC9vYXV0aCJ9"
    * 4
    + ".c2lnbmF0dXJlX2lzX2xvbmdfZW5vdWdoX3RvX2JlX2xpa2VfaGE"
)
"""Like Home Assistant's: a long signed JWT, which must come back unchanged"""


def create_client(api_client: TestClient, user: TestUser, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Home Assistant",
        "redirectUris": [HA_REDIRECT_URI, HA_LOCAL_REDIRECT_URI],
        "isConfidential": True,
        "pkceOptional": True,
        "allowWriteScope": False,
        **overrides,
    }
    response = api_client.post(api_routes.groups_mcp_clients, json=data, headers=user.token)
    assert response.status_code == 201, response.text
    return response.json()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def authorize(api_client: TestClient, params: dict[str, str] | list[tuple[str, str]]) -> httpx.Response:
    return api_client.get(api_routes.oauth_authorize, params=params, follow_redirects=False)


def authorize_params(client: dict[str, Any], **params: str) -> dict[str, str]:
    """Home Assistant's authorize request: no PKCE, no resource"""
    return {
        "response_type": "code",
        "client_id": client["clientId"],
        "redirect_uri": HA_REDIRECT_URI,
        "state": HA_STATE,
        "access_type": "offline",
        "prompt": "consent",
        "scope": "mcp:read mcp:write",
        **params,
    }


def consent_handle(response: httpx.Response) -> str:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("/oauth/consent?request=")
    return query(location)["request"]


def decide(
    api_client: TestClient, user: TestUser, handle: str, *, approve: bool = True, allow_writes: bool = False
) -> httpx.Response:
    return api_client.post(
        api_routes.oauth_requests_handle(handle),
        json={"approve": approve, "allowWrites": allow_writes},
        headers=user.token,
    )


def get_code(
    api_client: TestClient, user: TestUser, client: dict[str, Any], *, allow_writes: bool = False, **params: str
) -> str:
    handle = consent_handle(authorize(api_client, authorize_params(client, **params)))
    response = decide(api_client, user, handle, allow_writes=allow_writes)
    assert response.status_code == 200, response.text
    return query(response.json()["redirectTo"])["code"]


def token_request(
    api_client: TestClient, data: dict[str, str], client: dict[str, Any] | None = None, *, basic: bool = False
) -> httpx.Response:
    """POSTs to the token endpoint, authenticating as `client` with client_secret_post (or Basic)"""
    headers = {}
    if client is not None:
        if basic:
            credentials = f"{client['clientId']}:{client['clientSecret']}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
        else:
            data = {"client_id": client["clientId"], **data}
            if client.get("clientSecret"):
                data["client_secret"] = client["clientSecret"]
    return api_client.post(api_routes.oauth_token, data=data, headers=headers)


def exchange(api_client: TestClient, client: dict[str, Any], code: str, **data: str) -> httpx.Response:
    return token_request(
        api_client, {"grant_type": "authorization_code", "code": code, "redirect_uri": HA_REDIRECT_URI, **data}, client
    )


def refresh(api_client: TestClient, client: dict[str, Any], refresh_token: str, **data: str) -> httpx.Response:
    return token_request(api_client, {"grant_type": "refresh_token", "refresh_token": refresh_token, **data}, client)


def connect(
    api_client: TestClient, user: TestUser, client: dict[str, Any], *, allow_writes: bool = False
) -> dict[str, Any]:
    """The whole flow, Home Assistant style. Returns the token response."""
    code = get_code(api_client, user, client, allow_writes=allow_writes)
    response = exchange(api_client, client, code)
    assert response.status_code == 200, response.text
    return response.json()
