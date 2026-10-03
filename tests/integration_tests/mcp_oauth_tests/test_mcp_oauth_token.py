"""
The MCP authorization server's token and revocation endpoints (docs/ai/PHASE3.md §4, RFC 6749 §2.3, §4.1.3, §5, §6,
RFC 7009, OAuth 2.1 refresh token rotation)
"""

import asyncio
import base64
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event

from mealie.app import app
from mealie.db.db_setup import engine, session_context
from mealie.db.models.ai_mcp import McpOAuthCode, McpOAuthToken
from mealie.services.ai.mcp.auth import verify_mcp_token
from mealie.services.oauth.tokens import hash_secret
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser
from tests.utils.mcp_oauth import (
    HA_REDIRECT_URI,
    MCP_URL,
    authorize,
    authorize_params,
    connect,
    consent_handle,
    create_client,
    decide,
    exchange,
    get_code,
    pkce_pair,
    query,
    refresh,
    token_request,
)


def _assert_error(response: httpx.Response, error: str, status_code: int = 400) -> None:
    """RFC 6749 §5.2"""
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["error"] == error
    assert set(body) <= {"error", "error_description"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def _set_column(model: Any, token_or_code: str, hash_column: str, **values: Any) -> None:
    with session_context() as session:
        session.execute(
            sa.update(model).where(getattr(model, hash_column) == hash_secret(token_or_code)).values(**values)
        )
        session.commit()


# ==========================================
# Responses


def test_token_response(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    response = exchange(api_client, client, code)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "refresh_token", "scope"}
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == "mcp:read"
    assert body["access_token"].startswith("mmcp_at_") and len(body["access_token"]) >= 8 + 43
    assert body["refresh_token"].startswith("mmcp_rt_") and len(body["refresh_token"]) >= 8 + 43

    # stored as SHA-256 only
    with session_context() as session:
        stored = set(session.execute(sa.select(McpOAuthToken.token_hash)).scalars())
        assert hash_secret(body["access_token"]) in stored
        assert body["access_token"] not in stored and body["refresh_token"] not in stored


@pytest.mark.parametrize(
    "data, error",
    [
        ({}, "invalid_request"),
        ({"grant_type": "password", "username": "u", "password": "p"}, "unsupported_grant_type"),
        ({"grant_type": "client_credentials"}, "unsupported_grant_type"),
        ({"grant_type": "authorization_code"}, "invalid_request"),
        ({"grant_type": "authorization_code", "code": "not-a-code", "redirect_uri": HA_REDIRECT_URI}, "invalid_grant"),
        ({"grant_type": "refresh_token"}, "invalid_request"),
        ({"grant_type": "refresh_token", "refresh_token": "mmcp_rt_nope"}, "invalid_grant"),
    ],
)
def test_token_errors(api_client: TestClient, unique_user: TestUser, data: dict[str, str], error: str):
    client = create_client(api_client, unique_user)
    _assert_error(token_request(api_client, data, client), error)


def test_repeated_parameters(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    body = (
        f"grant_type=authorization_code&code={code}&code={code}&redirect_uri={HA_REDIRECT_URI}"
        f"&client_id={client['clientId']}&client_secret={client['clientSecret']}"
    )
    response = api_client.post(
        api_routes.oauth_token, content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    _assert_error(response, "invalid_request")


def test_json_bodies_are_not_read(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": HA_REDIRECT_URI,
        "client_id": client["clientId"],
        "client_secret": client["clientSecret"],
    }
    _assert_error(api_client.post(api_routes.oauth_token, json=data), "invalid_request")


# ==========================================
# Client authentication


def test_client_secret_post_and_basic(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)

    code = get_code(api_client, unique_user, client)
    assert exchange(api_client, client, code).status_code == 200

    code = get_code(api_client, unique_user, client)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": HA_REDIRECT_URI}
    response = token_request(api_client, data, client, basic=True)
    assert response.status_code == 200, response.text


def test_client_authentication_failures(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    other = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": HA_REDIRECT_URI}

    # wrong secret, by post and by basic: 401, with a Basic challenge for the latter (RFC 6749 §5.2)
    response = token_request(api_client, data, {**client, "clientSecret": other["clientSecret"]})
    _assert_error(response, "invalid_client", 401)
    assert "www-authenticate" not in response.headers

    response = token_request(api_client, data, {**client, "clientSecret": "wrong"}, basic=True)
    _assert_error(response, "invalid_client", 401)
    assert response.headers["www-authenticate"] == 'Basic realm="Mealie"'

    response = api_client.post(api_routes.oauth_token, data=data, headers={"Authorization": "Basic !!!"})
    _assert_error(response, "invalid_client", 401)

    # a confidential client must authenticate
    _assert_error(token_request(api_client, {**data, "client_id": client["clientId"]}), "invalid_client", 401)
    _assert_error(token_request(api_client, data), "invalid_client", 401)
    _assert_error(token_request(api_client, data, {**client, "clientId": "mmcp_unknown"}), "invalid_client", 401)

    # one method at a time (RFC 6749 §2.3)
    response = token_request(api_client, {**data, "client_secret": client["clientSecret"]}, client, basic=True)
    _assert_error(response, "invalid_request")

    # none of that used the code up
    assert exchange(api_client, client, code).status_code == 200


def test_public_clients_send_no_secret(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user, isConfidential=False, pkceOptional=False)
    assert client["clientSecret"] is None
    verifier, challenge = pkce_pair()
    params = authorize_params(client, code_challenge=challenge, code_challenge_method="S256")
    code = query(decide(api_client, unique_user, consent_handle(authorize(api_client, params))).json()["redirectTo"])[
        "code"
    ]

    response = exchange(api_client, {**client, "clientSecret": "anything"}, code, code_verifier=verifier)
    _assert_error(response, "invalid_client", 401)

    response = exchange(api_client, client, code, code_verifier=verifier)
    assert response.status_code == 200, response.text


# ==========================================
# Authorization codes


def test_codes_are_single_use_and_reuse_revokes_their_tokens(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    tokens = exchange(api_client, client, code).json()
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None

    # RFC 6749 §4.1.2: refused, and everything issued from the code is revoked
    _assert_error(exchange(api_client, client, code), "invalid_grant")
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is None
    _assert_error(refresh(api_client, client, tokens["refresh_token"]), "invalid_grant")


def test_codes_are_bound_to_their_client(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    other = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    _assert_error(exchange(api_client, other, code), "invalid_grant")
    assert exchange(api_client, client, code).status_code == 200


def test_codes_expire(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)

    with session_context() as session:
        row = session.execute(sa.select(McpOAuthCode).where(McpOAuthCode.code_hash == hash_secret(code))).scalar_one()
        assert row.expires_at - row.created_at == timedelta(seconds=60)

    _set_column(McpOAuthCode, code, "code_hash", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    _assert_error(exchange(api_client, client, code), "invalid_grant")


def test_redirect_uri_must_match(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    data = {"grant_type": "authorization_code", "code": code}

    _assert_error(token_request(api_client, data, client), "invalid_grant")
    other_uri = "http://homeassistant.local:8123/auth/external/callback"
    _assert_error(token_request(api_client, {**data, "redirect_uri": other_uri}, client), "invalid_grant")
    assert exchange(api_client, client, code).status_code == 200


def test_a_verifier_without_a_challenge_is_refused(api_client: TestClient, unique_user: TestUser):
    """OAuth 2.1 §4.1.3: so PKCE can't be stripped from the authorization request alone"""
    client = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    _assert_error(exchange(api_client, client, code, code_verifier="a" * 43), "invalid_grant")


def test_resource_must_be_the_one_granted(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    code = get_code(api_client, unique_user, client)
    _assert_error(exchange(api_client, client, code, resource="https://other.example/api/mcp"), "invalid_target")
    response = exchange(api_client, client, code, resource=MCP_URL)
    assert response.status_code == 200

    with session_context() as session:
        token_hash = hash_secret(response.json()["access_token"])
        token = session.execute(sa.select(McpOAuthToken).where(McpOAuthToken.token_hash == token_hash)).scalar_one()
        # bound to this server even though Home Assistant never sends a resource
        assert token.resource == MCP_URL
        assert token.expires_at - token.created_at == timedelta(hours=1)


# ==========================================
# Refresh tokens


def test_refresh_rotates(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    first = connect(api_client, unique_user, client)

    response = refresh(api_client, client, first["refresh_token"])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    second = response.json()
    assert second["expires_in"] == 3600
    assert second["scope"] == "mcp:read"
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]

    with session_context() as session:

        def row(token: str) -> McpOAuthToken:
            stmt = sa.select(McpOAuthToken).where(McpOAuthToken.token_hash == hash_secret(token))
            return session.execute(stmt).scalar_one()

        old_refresh, new_refresh = row(first["refresh_token"]), row(second["refresh_token"])
        assert old_refresh.revoked_at is not None
        assert new_refresh.revoked_at is None
        assert new_refresh.family_id == old_refresh.family_id
        assert new_refresh.granted_at == old_refresh.granted_at
        assert new_refresh.expires_at - new_refresh.created_at == timedelta(days=90)

    # the earlier access token lives on until it expires
    assert verify_mcp_token(first["access_token"], MCP_URL) is not None
    assert verify_mcp_token(second["access_token"], MCP_URL) is not None


def test_refresh_reuse_revokes_the_family(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    first = connect(api_client, unique_user, client)
    second = refresh(api_client, client, first["refresh_token"]).json()
    third = refresh(api_client, client, second["refresh_token"]).json()
    assert verify_mcp_token(third["access_token"], MCP_URL) is not None

    # replaying a rotated refresh token: refused, and every token of the family is revoked
    _assert_error(refresh(api_client, client, first["refresh_token"]), "invalid_grant")
    _assert_error(refresh(api_client, client, third["refresh_token"]), "invalid_grant")
    for tokens in (first, second, third):
        assert verify_mcp_token(tokens["access_token"], MCP_URL) is None

    # other connections are untouched
    other = connect(api_client, unique_user, client)
    assert refresh(api_client, client, other["refresh_token"]).status_code == 200


def test_refresh_is_bound_to_its_client(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    other = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)
    _assert_error(refresh(api_client, other, tokens["refresh_token"]), "invalid_grant")
    # an access token isn't a refresh token
    _assert_error(refresh(api_client, client, tokens["access_token"]), "invalid_grant")
    assert refresh(api_client, client, tokens["refresh_token"]).status_code == 200


def test_refresh_may_narrow_the_scope(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user, allowWriteScope=True)
    tokens = connect(api_client, unique_user, client, allow_writes=True)
    assert tokens["scope"] == "mcp:read mcp:write"

    narrowed = refresh(api_client, client, tokens["refresh_token"], scope="mcp:read").json()
    assert narrowed["scope"] == "mcp:read"
    principal = verify_mcp_token(narrowed["access_token"], MCP_URL)
    assert principal is not None and not principal.can_write

    # the refresh token keeps what was granted (RFC 6749 §6)
    widened = refresh(api_client, client, narrowed["refresh_token"])
    assert widened.json()["scope"] == "mcp:read mcp:write"

    read_only = connect(api_client, unique_user, client)
    _assert_error(refresh(api_client, client, read_only["refresh_token"], scope="mcp:read mcp:write"), "invalid_scope")


def test_refresh_tokens_expire(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)
    _set_column(McpOAuthToken, tokens["refresh_token"], "token_hash", expires_at=datetime.now(UTC))
    _assert_error(refresh(api_client, client, tokens["refresh_token"]), "invalid_grant")


# ==========================================
# Revocation (RFC 7009)


def _revoke(api_client: TestClient, client: dict[str, Any], token: str, **data: str) -> httpx.Response:
    return api_client.post(
        api_routes.oauth_revoke,
        data={"token": token, "client_id": client["clientId"], "client_secret": client["clientSecret"], **data},
    )


def test_revoke_an_access_token(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None  # cached now

    response = _revoke(api_client, client, tokens["access_token"], token_type_hint="access_token")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is None

    # the refresh token still works
    assert refresh(api_client, client, tokens["refresh_token"]).status_code == 200


def test_revoke_a_refresh_token_revokes_its_family(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None

    # the hint is only a hint
    assert _revoke(api_client, client, tokens["refresh_token"], token_type_hint="access_token").status_code == 200
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is None
    _assert_error(refresh(api_client, client, tokens["refresh_token"]), "invalid_grant")


def test_revoke_answers_200_for_unknown_and_foreign_tokens(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    other = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)

    assert _revoke(api_client, client, "mmcp_at_unknown").status_code == 200
    # another client's token is left alone
    assert _revoke(api_client, other, tokens["access_token"]).status_code == 200
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None


def test_revoke_authenticates_the_client(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)

    response = _revoke(api_client, {**client, "clientSecret": "wrong"}, tokens["access_token"])
    _assert_error(response, "invalid_client", 401)
    _assert_error(
        api_client.post(api_routes.oauth_revoke, data={"token": tokens["access_token"]}), "invalid_client", 401
    )  # noqa: E501
    _assert_error(_revoke(api_client, client, ""), "invalid_request")
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None

    credentials = base64.b64encode(f"{client['clientId']}:{client['clientSecret']}".encode()).decode()
    response = api_client.post(
        api_routes.oauth_revoke,
        data={"token": tokens["access_token"]},
        headers={"Authorization": f"Basic {credentials}"},
    )
    assert response.status_code == 200
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is None


# ==========================================
# Off the event loop


def test_token_requests_dont_touch_the_database_on_the_event_loop(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """
    More simultaneous token requests than the connection pool holds all succeed, and none of their queries run on
    the event loop (PHASE1.md §5)
    """
    client = create_client(api_client, unique_user)
    refresh_tokens = [connect(api_client, unique_user, client)["refresh_token"] for _ in range(24)]

    # fail a blocked checkout after a few seconds instead of 30
    monkeypatch.setattr(engine.pool, "_timeout", 3)

    async def burst() -> list[httpx.Response | BaseException]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60) as http:
            requests = [
                http.post(
                    api_routes.oauth_token,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": client["clientId"],
                        "client_secret": client["clientSecret"],
                    },
                )
                for refresh_token in refresh_tokens
            ]
            requests.append(http.get("/.well-known/oauth-authorization-server"))
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

    statuses = [r.status_code if isinstance(r, httpx.Response) else repr(r) for r in responses]
    assert statuses == [200] * len(responses)
    assert on_the_loop == []
