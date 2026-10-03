"""
The MCP authorization server's authorization endpoint and consent API (docs/ai/PHASE3.md §4, RFC 6749 §4.1,
RFC 7636, RFC 8252 §7.3, RFC 8707, RFC 9207)
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.db.models.ai_mcp import McpOAuthClient, McpOAuthRequest
from mealie.repos.repository_mcp import MAX_PENDING_REQUESTS
from mealie.services.oauth.tokens import hash_secret
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser
from tests.utils.mcp_oauth import (
    HA_LOCAL_REDIRECT_URI,
    HA_REDIRECT_URI,
    HA_STATE,
    ORIGIN,
    authorize,
    authorize_params,
    consent_handle,
    create_client,
    decide,
    exchange,
    pkce_pair,
    query,
    token_request,
)

ISS = quote_plus(ORIGIN, safe="")


def _assert_error_page(response) -> None:
    """RFC 6749 §4.1.2.1: no redirect, just a page for the user"""
    assert response.status_code == 400
    assert "location" not in response.headers
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"


def _error_redirect(response) -> dict[str, str]:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(HA_REDIRECT_URI + "?")
    params = query(location)
    assert params["iss"] == ORIGIN
    return params


# ==========================================
# Client and redirect URI: never redirect


def test_unknown_or_missing_client_shows_a_page(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)

    for params in [
        authorize_params(client, client_id="mmcp_not_a_client"),
        {k: v for k, v in authorize_params(client).items() if k != "client_id"},
        [*authorize_params(client).items(), ("client_id", client["clientId"])],
    ]:
        _assert_error_page(authorize(api_client, params))


def test_unregistered_redirect_uri_shows_a_page(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)

    for redirect_uri in [
        "https://evil.example/redirect/oauth",
        HA_REDIRECT_URI + "/",
        HA_REDIRECT_URI + "?x=1",
        "https://MY.home-assistant.io/redirect/oauth",
        "http://homeassistant.local:8124/auth/external/callback",
    ]:
        response = authorize(api_client, authorize_params(client, redirect_uri=redirect_uri))
        _assert_error_page(response)
        assert "evil" not in response.text

    # repeated, even with a registered value
    response = authorize(api_client, [*authorize_params(client).items(), ("redirect_uri", HA_REDIRECT_URI)])
    _assert_error_page(response)

    # an invalid client is reported before anything else, and the page never echoes markup
    response = authorize(api_client, authorize_params(client, client_id="<script>alert(1)</script>"))
    _assert_error_page(response)
    assert "<script>" not in response.text


def test_redirect_uri_may_be_omitted_only_with_one_registered(api_client: TestClient, unique_user: TestUser):
    two = create_client(api_client, unique_user)
    params = {k: v for k, v in authorize_params(two).items() if k != "redirect_uri"}
    _assert_error_page(authorize(api_client, params))

    one = create_client(api_client, unique_user, redirectUris=[HA_REDIRECT_URI])
    params = {k: v for k, v in authorize_params(one).items() if k != "redirect_uri"}
    handle = consent_handle(authorize(api_client, params))
    response = decide(api_client, unique_user, handle)
    assert response.json()["redirectTo"].startswith(HA_REDIRECT_URI + "?code=")

    # the token request may then leave it out too (RFC 6749 §4.1.3)
    code = query(response.json()["redirectTo"])["code"]
    response = token_request(api_client, {"grant_type": "authorization_code", "code": code}, one)
    assert response.status_code == 200, response.text


def test_loopback_redirect_uris_match_any_port(api_client: TestClient, unique_user: TestUser):
    """RFC 8252 §7.3, for native clients such as Claude Code"""
    client = create_client(
        api_client,
        unique_user,
        name="Claude Code",
        redirectUris=["http://127.0.0.1/callback"],
        isConfidential=False,
        pkceOptional=False,
    )
    verifier, challenge = pkce_pair()
    redirect_uri = "http://127.0.0.1:43123/callback"
    params = {
        "response_type": "code",
        "client_id": client["clientId"],
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": f"{ORIGIN}/api/mcp",
        "state": "xyz",
    }
    handle = consent_handle(authorize(api_client, params))
    location = decide(api_client, unique_user, handle).json()["redirectTo"]
    assert location.startswith(redirect_uri + "?code=")

    # the code is bound to the port it was sent to
    code = query(location)["code"]
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    response = token_request(api_client, {**data, "redirect_uri": "http://127.0.0.1:43124/callback"}, client)
    assert response.json()["error"] == "invalid_grant"

    _assert_error_page(authorize(api_client, {**params, "redirect_uri": "http://127.0.0.1:43123/other"}))
    _assert_error_page(authorize(api_client, {**params, "redirect_uri": "http://localhost:43123/callback"}))


# ==========================================
# Other errors go back to the client, with state and iss


def test_error_redirects_are_exact(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)

    response = authorize(api_client, authorize_params(client, response_type="token"))
    assert response.status_code == 302
    assert response.headers["location"] == (
        f"{HA_REDIRECT_URI}?error=unsupported_response_type"
        "&error_description=Only+the+authorization+code+flow+is+supported"
        f"&state={HA_STATE}&iss={ISS}"
    )
    assert response.headers["cache-control"] == "no-store"

    # without a state, none comes back
    params = {k: v for k, v in authorize_params(client).items() if k not in ("response_type", "state")}
    response = authorize(api_client, params)
    assert response.headers["location"] == (
        f"{HA_REDIRECT_URI}?error=invalid_request&error_description=response_type+is+required&iss={ISS}"
    )


@pytest.mark.parametrize(
    "params, error",
    [
        ({"scope": "mcp:read admin"}, "invalid_scope"),
        ({"scope": "openid"}, "invalid_scope"),
        ({"resource": "https://other.example/api/mcp"}, "invalid_target"),
        ({"resource": f"{ORIGIN}/api/other"}, "invalid_target"),
        ({"code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"}, "invalid_request"),  # implies plain
        (
            {"code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", "code_challenge_method": "plain"},
            "invalid_request",
        ),  # noqa: E501
        ({"code_challenge": "too-short", "code_challenge_method": "S256"}, "invalid_request"),
        ({"code_challenge_method": "S256"}, "invalid_request"),
    ],
)
def test_invalid_requests(api_client: TestClient, unique_user: TestUser, params: dict[str, str], error: str):
    client = create_client(api_client, unique_user)
    response = authorize(api_client, authorize_params(client, **params))
    redirect = _error_redirect(response)
    assert redirect["error"] == error
    assert redirect["state"] == HA_STATE
    assert "code" not in redirect


def test_repeated_parameters_are_refused(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    for name in ("response_type", "scope", "code_challenge"):
        params = [*authorize_params(client, code_challenge=pkce_pair()[1], code_challenge_method="S256").items()]
        params.append((name, dict(params)[name]))
        redirect = _error_redirect(authorize(api_client, params))
        assert redirect["error"] == "invalid_request"
        assert redirect["state"] == HA_STATE

    # with two states, neither is echoed
    redirect = _error_redirect(authorize(api_client, [*authorize_params(client).items(), ("state", "other")]))
    assert redirect["error"] == "invalid_request"
    assert "state" not in redirect


def test_overlong_requests_are_refused(api_client: TestClient, unique_user: TestUser):
    """Anyone can make a pending request, so what one stores is bounded"""
    client = create_client(api_client, unique_user)
    consent_handle(authorize(api_client, authorize_params(client, state="s" * 2048)))

    redirect = _error_redirect(authorize(api_client, authorize_params(client, state="s" * 2049)))
    assert redirect["error"] == "invalid_request"
    assert "state" not in redirect

    # a request this long isn't read at all
    _assert_error_page(authorize(api_client, authorize_params(client, prompt="p" * 8192)))


def test_resource_defaults_to_this_server_and_may_be_given(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    for resource in (None, f"{ORIGIN}/api/mcp", f"{ORIGIN}/api/mcp/"):
        params = authorize_params(client, **({"resource": resource} if resource else {}))
        consent_handle(authorize(api_client, params))


# ==========================================
# PKCE: required unless the client is confidential and flagged


def test_pkce_is_required_for_public_clients(api_client: TestClient, unique_user: TestUser):
    client = create_client(
        api_client, unique_user, redirectUris=[HA_REDIRECT_URI], isConfidential=False, pkceOptional=False
    )
    assert "clientSecret" not in client or client["clientSecret"] is None

    redirect = _error_redirect(authorize(api_client, authorize_params(client)))
    assert redirect["error"] == "invalid_request"
    assert "PKCE" in redirect["error_description"]

    verifier, challenge = pkce_pair()
    params = authorize_params(client, code_challenge=challenge, code_challenge_method="S256")
    handle = consent_handle(authorize(api_client, params))
    code = query(decide(api_client, unique_user, handle).json()["redirectTo"])["code"]

    # the verifier is required at the token endpoint too, and must match
    response = exchange(api_client, client, code)
    assert response.json()["error"] == "invalid_grant"
    response = exchange(api_client, client, code, code_verifier=verifier)
    # (the failed attempt above didn't use the code up)
    assert response.status_code == 200, response.text


def test_pkce_is_required_for_confidential_clients_without_the_flag(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user, pkceOptional=False)
    redirect = _error_redirect(authorize(api_client, authorize_params(client)))
    assert redirect["error"] == "invalid_request"

    _, challenge = pkce_pair()
    consent_handle(
        authorize(api_client, authorize_params(client, code_challenge=challenge, code_challenge_method="S256"))
    )


def test_pkce_optional_is_only_for_confidential_clients(api_client: TestClient, unique_user: TestUser):
    data = {
        "name": "Public",
        "redirectUris": [HA_REDIRECT_URI],
        "isConfidential": False,
        "pkceOptional": True,
    }
    response = api_client.post(api_routes.groups_mcp_clients, json=data, headers=unique_user.token)
    assert response.status_code == 422

    public = create_client(api_client, unique_user, isConfidential=False, pkceOptional=False)
    response = api_client.put(
        api_routes.groups_mcp_clients_item_id(public["id"]),
        json={**data, "name": "Still public"},
        headers=unique_user.token,
    )
    assert response.status_code == 400
    assert "confidential" in response.json()["detail"]["message"]


def test_a_flagged_confidential_client_may_still_use_pkce(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    verifier, challenge = pkce_pair()
    params = authorize_params(client, code_challenge=challenge, code_challenge_method="S256")
    code = query(decide(api_client, unique_user, consent_handle(authorize(api_client, params))).json()["redirectTo"])[
        "code"
    ]
    assert exchange(api_client, client, code).json()["error"] == "invalid_grant"
    assert exchange(api_client, client, code, code_verifier=verifier).status_code == 200


# ==========================================
# Consent


def test_consent_shows_the_request(api_client: TestClient, unique_user: TestUser):
    read_only = create_client(api_client, unique_user)
    handle = consent_handle(authorize(api_client, authorize_params(read_only)))
    response = api_client.get(api_routes.oauth_requests_handle(handle), headers=unique_user.token)
    assert response.status_code == 200
    body = response.json()
    assert body["clientName"] == "Home Assistant"
    # mcp:write was asked for, but this client may not have it
    assert body["scopes"] == ["mcp:read"]
    assert body["writesOffered"] is False
    assert body["redirectHost"] == "my.home-assistant.io"

    writer = create_client(api_client, unique_user, allowWriteScope=True)
    params = authorize_params(writer, redirect_uri=HA_LOCAL_REDIRECT_URI)
    handle = consent_handle(authorize(api_client, params))
    body = api_client.get(api_routes.oauth_requests_handle(handle), headers=unique_user.token).json()
    assert body["scopes"] == ["mcp:read", "mcp:write"]
    assert body["writesOffered"] is True
    assert body["redirectHost"] == "homeassistant.local:8123"

    # not offered when the client didn't ask
    handle = consent_handle(authorize(api_client, authorize_params(writer, scope="mcp:read")))
    body = api_client.get(api_routes.oauth_requests_handle(handle), headers=unique_user.token).json()
    assert body["writesOffered"] is False


def test_consent_shows_an_international_host_in_ascii(api_client: TestClient, unique_user: TestUser):
    """So a lookalike letter from another script stands out. Registration refuses such hosts in the first place."""
    lookalike = "https://my.home-assіstant.io/redirect/oauth"  # with a Cyrillic "і"
    client = create_client(api_client, unique_user)
    with session_context() as session:
        session.execute(
            sa.update(McpOAuthClient).where(McpOAuthClient.id == client["id"]).values(redirect_uris=[lookalike])
        )
        session.commit()

    handle = consent_handle(authorize(api_client, authorize_params(client, redirect_uri=lookalike)))
    body = api_client.get(api_routes.oauth_requests_handle(handle), headers=unique_user.token).json()
    assert body["redirectHost"] == "my.xn--home-assstant-bil.io"


@pytest.mark.parametrize(
    "allow_writes, writable, scope",
    [(False, True, "mcp:read"), (True, True, "mcp:read mcp:write"), (True, False, "mcp:read")],
)  # noqa: E501
def test_writes_need_the_client_and_the_user(
    api_client: TestClient, unique_user: TestUser, allow_writes: bool, writable: bool, scope: str
):
    client = create_client(api_client, unique_user, allowWriteScope=writable)
    handle = consent_handle(authorize(api_client, authorize_params(client)))
    location = decide(api_client, unique_user, handle, allow_writes=allow_writes).json()["redirectTo"]
    response = exchange(api_client, client, query(location)["code"])
    assert response.json()["scope"] == scope


def test_approval_redirect_is_exact(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    handle = consent_handle(authorize(api_client, authorize_params(client)))
    location = decide(api_client, unique_user, handle).json()["redirectTo"]

    code = query(location)["code"]
    assert len(code) >= 43
    assert location == f"{HA_REDIRECT_URI}?code={code}&state={HA_STATE}&iss={ISS}"


def test_denial_redirect_is_exact(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user, redirectUris=["https://ha.example/cb?tenant=a"])
    params = authorize_params(client, redirect_uri="https://ha.example/cb?tenant=a")
    handle = consent_handle(authorize(api_client, params))
    response = decide(api_client, unique_user, handle, approve=False)
    assert response.status_code == 200
    assert response.json()["redirectTo"] == (
        "https://ha.example/cb?tenant=a&error=access_denied&error_description=The+user+declined"
        f"&state={HA_STATE}&iss={ISS}"
    )


def test_each_request_is_decided_once(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    handle = consent_handle(authorize(api_client, authorize_params(client)))
    assert decide(api_client, unique_user, handle).status_code == 200
    assert decide(api_client, unique_user, handle).status_code == 404
    assert decide(api_client, unique_user, handle, approve=False).status_code == 404
    assert api_client.get(api_routes.oauth_requests_handle(handle), headers=unique_user.token).status_code == 404


def test_only_a_clients_newest_pending_requests_are_kept(api_client: TestClient, unique_user: TestUser):
    """Anyone who knows a client ID can make pending requests: they can't pile up"""
    client, other = create_client(api_client, unique_user), create_client(api_client, unique_user)
    others = consent_handle(authorize(api_client, authorize_params(other)))
    handles = [consent_handle(authorize(api_client, authorize_params(client))) for _ in range(MAX_PENDING_REQUESTS + 2)]

    with session_context() as session:
        pending = session.execute(
            sa.select(sa.func.count()).where(McpOAuthRequest.oauth_client_id == client["id"])
        ).scalar_one()
    assert pending == MAX_PENDING_REQUESTS == 50
    for handle, status_code in ((handles[0], 404), (handles[1], 404), (handles[2], 200), (handles[-1], 200)):
        response = api_client.get(api_routes.oauth_requests_handle(handle), headers=unique_user.token)
        assert response.status_code == status_code
    assert api_client.get(api_routes.oauth_requests_handle(others), headers=unique_user.token).status_code == 200


def test_requests_expire(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    handle = consent_handle(authorize(api_client, authorize_params(client)))

    with session_context() as session:
        session.execute(
            sa.update(McpOAuthRequest)
            .where(McpOAuthRequest.handle_hash == hash_secret(handle))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()

    assert api_client.get(api_routes.oauth_requests_handle(handle), headers=unique_user.token).status_code == 404
    assert decide(api_client, unique_user, handle).status_code == 404


def test_consent_needs_a_bearer_token(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    handle = consent_handle(authorize(api_client, authorize_params(client)))
    url = api_routes.oauth_requests_handle(handle)

    assert api_client.get(url).status_code == 401
    assert api_client.post(url, json={"approve": True}).status_code == 401

    # the session cookie alone isn't enough: a cross-site form could carry it
    token = unique_user.token["Authorization"].removeprefix("Bearer ")
    api_client.cookies.set("mealie.access_token", token)
    try:
        assert api_client.get(url).status_code == 401
        assert api_client.post(url, json={"approve": True}).status_code == 401
    finally:
        api_client.cookies.clear()

    # still pending
    assert api_client.get(url, headers=unique_user.token).status_code == 200


def test_only_the_clients_group_can_connect_it(
    api_client: TestClient, unique_user: TestUser, h2_user: TestUser, g2_user: TestUser
):
    client = create_client(api_client, unique_user)
    handle = consent_handle(authorize(api_client, authorize_params(client)))

    # another group's user can't see or answer it
    assert api_client.get(api_routes.oauth_requests_handle(handle), headers=g2_user.token).status_code == 404
    assert decide(api_client, g2_user, handle).status_code == 404

    # any user of the group can, managers or not
    response = decide(api_client, h2_user, handle)
    assert response.status_code == 200
    response = exchange(api_client, client, query(response.json()["redirectTo"])["code"])
    assert response.status_code == 200
