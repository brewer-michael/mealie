"""
The MCP endpoint's token verifier (docs/ai/PHASE3.md §2-3): which tokens it accepts, the audience check, expiry,
revocation, password changes, deletions and its cache. Ends with a replay of Home Assistant's whole OAuth flow.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event

from mealie.core.security import create_access_token
from mealie.db.db_setup import engine, session_context
from mealie.db.models.ai_mcp import McpApiTokenGrant, McpOAuthClient, McpOAuthCode, McpOAuthRequest, McpOAuthToken
from mealie.db.models.users.users import LongLiveToken, User
from mealie.schema.mcp.mcp_oauth import McpClientCreate
from mealie.schema.user.user import CreateToken
from mealie.services.ai.mcp import auth
from mealie.services.ai.mcp.auth import (
    PRINCIPAL_CACHE_TTL,
    cached_mcp_principal,
    clear_mcp_principal_cache,
    invalidate_mcp_principals,
    mcp_www_authenticate,
    verify_mcp_token,
)
from mealie.services.oauth.clients import McpClientService
from mealie.services.oauth.tokens import hash_secret
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser
from tests.utils.mcp_oauth import (
    HA_REDIRECT_URI,
    HA_STATE,
    MCP_URL,
    ORIGIN,
    authorize,
    authorize_params,
    connect,
    consent_handle,
    create_client,
    decide,
    exchange,
    get_code,
    query,
    refresh,
)


def _update_token(token: str, **values: Any) -> None:
    with session_context() as session:
        session.execute(sa.update(McpOAuthToken).where(McpOAuthToken.token_hash == hash_secret(token)).values(**values))
        session.commit()


def _api_token(api_client: TestClient, user: TestUser) -> tuple[int, str]:
    response = api_client.post(api_routes.users_api_tokens, json={"name": random_string()}, headers=user.token)
    assert response.status_code == 201
    return response.json()["id"], response.json()["token"]


def _count(model: Any, *where: Any) -> int:
    with session_context() as session:
        return session.execute(sa.select(sa.func.count()).select_from(model).where(*where)).scalar_one()


def _in_another_thread[T](fn: Callable[[], T]) -> T:
    """`fn()` as another request would run it: in its own thread, with its own session"""
    result: list[T] = []
    thread = threading.Thread(target=lambda: result.append(fn()))
    thread.start()
    thread.join()
    return result[0]


# ==========================================
# What it accepts


def test_oauth_principal(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user, name="Kitchen", allowWriteScope=True)
    tokens = connect(api_client, unique_user, client, allow_writes=True)

    principal = verify_mcp_token(tokens["access_token"], MCP_URL)
    assert principal is not None
    assert principal.user.id == unique_user.user_id
    assert (str(principal.group_id), str(principal.household_id)) == (unique_user.group_id, unique_user.household_id)
    assert (principal.client_name, principal.client_id, principal.api_token_id) == ("Kitchen", client["clientId"], None)
    assert principal.scopes == {"mcp:read", "mcp:write"}
    assert principal.can_write is True
    assert principal.integration_id == "mcp:Kitchen"


def test_access_tokens_are_bound_to_this_server(api_client: TestClient, unique_user: TestUser):
    """The audience check (RFC 8707; MCP authorization's token audience validation)"""
    client = create_client(api_client, unique_user)
    token = connect(api_client, unique_user, client)["access_token"]

    for resource in ("https://other.example/api/mcp", "http://testserver:8080/api/mcp", f"{ORIGIN}/api/other", "nope"):
        assert verify_mcp_token(token, resource) is None
    assert verify_mcp_token(token, MCP_URL) is not None
    # a cached verification is still checked against the resource
    assert verify_mcp_token(token, "https://other.example/api/mcp") is None
    assert verify_mcp_token(token, f"{ORIGIN}/api/mcp/") is not None
    assert verify_mcp_token(token, "HTTP://TESTSERVER/api/mcp") is not None


def test_other_tokens_are_refused(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)
    session_token = unique_user.token["Authorization"].removeprefix("Bearer ")

    # the session token works on the REST API, but not here (no token passthrough)
    assert api_client.get(api_routes.users_self, headers=unique_user.token).status_code == 200
    assert verify_mcp_token(session_token, MCP_URL) is None

    # an API token that was never issued, though correctly signed
    forged, _ = create_access_token({"long_token": True, "id": str(unique_user.user_id), "name": "forged"})
    assert verify_mcp_token(forged, MCP_URL) is None
    other_secret = jwt.encode({"long_token": True, "id": str(unique_user.user_id)}, "not-the-secret-" * 4, "HS256")
    assert verify_mcp_token(other_secret, MCP_URL) is None

    for token in (
        tokens["refresh_token"],
        client["clientSecret"],
        client["clientId"],
        "mmcp_at_" + "x" * 43,
        "",
        "x" * 5000,
    ):
        assert verify_mcp_token(token, MCP_URL) is None


def test_mcp_tokens_dont_work_on_the_rest_api(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    token = connect(api_client, unique_user, client)["access_token"]
    response = api_client.get(api_routes.users_self, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_api_token_principal(api_client: TestClient, unique_user: TestUser):
    token_id, token = _api_token(api_client, unique_user)
    principal = verify_mcp_token(token, MCP_URL)
    assert principal is not None
    assert principal.user.id == unique_user.user_id
    assert (principal.client_name, principal.client_id, principal.api_token_id) == ("API token", None, token_id)
    assert (principal.scopes, principal.can_write) == ({"mcp:read"}, False)
    # API tokens aren't bound to an audience: they're the user's own credential for this Mealie
    assert verify_mcp_token(token, "https://mealie.example/api/mcp") is not None


def test_identical_api_tokens(api_client: TestClient, unique_user: TestUser):
    """
    Two API tokens given the same name in the same second are the same JWT. Upstream accepts it, and so does this,
    with the first one's write grant.
    """
    token_id, token = _api_token(api_client, unique_user)
    twin = unique_user.repos.api_tokens.create(CreateToken(name="twin", token=token, user_id=unique_user.user_id))
    response = api_client.put(
        api_routes.users_self_mcp_api_tokens_token_id(twin.id), json={"allowWrites": True}, headers=unique_user.token
    )
    assert response.status_code == 200

    principal = verify_mcp_token(token, MCP_URL)
    assert principal is not None
    assert (principal.api_token_id, principal.can_write) == (token_id, False)


# ==========================================
# Expiry, revocation and the cache


def test_expired_api_tokens_are_refused(unique_user: TestUser):
    """Checked as upstream checks them: the JWT's expiry, and the token still being on record"""
    expired, _ = create_access_token(
        {"long_token": True, "id": str(unique_user.user_id), "name": "expired"}, timedelta(seconds=-1)
    )
    unique_user.repos.api_tokens.create(CreateToken(name="expired", token=expired, user_id=unique_user.user_id))
    assert verify_mcp_token(expired, MCP_URL) is None


def test_expired_tokens_are_refused(api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch):
    client = create_client(api_client, unique_user)
    fresh = connect(api_client, unique_user, client)["access_token"]
    _update_token(fresh, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert verify_mcp_token(fresh, MCP_URL) is None

    # a cached token is dropped when it expires, even inside the cache's TTL
    cached = connect(api_client, unique_user, client)["access_token"]
    _update_token(cached, expires_at=datetime.now(UTC) + timedelta(seconds=30))
    assert verify_mcp_token(cached, MCP_URL) is not None
    _update_token(cached, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    later = auth.monotonic() + 31
    monkeypatch.setattr(auth, "monotonic", lambda: later)
    assert verify_mcp_token(cached, MCP_URL) is None


def test_cache_and_invalidation(api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch):
    client = create_client(api_client, unique_user)
    token = connect(api_client, unique_user, client)["access_token"]
    assert verify_mcp_token(token, MCP_URL) is not None

    # gone from the database without telling the cache: still trusted for a while...
    _update_token(token, revoked_at=datetime.now(UTC))
    assert verify_mcp_token(token, MCP_URL) is not None

    # ...until it's invalidated
    invalidate_mcp_principals(token_hash=hash_secret(token))
    assert verify_mcp_token(token, MCP_URL) is None

    # or until the TTL runs out
    other = connect(api_client, unique_user, client)["access_token"]
    assert verify_mcp_token(other, MCP_URL) is not None
    _update_token(other, revoked_at=datetime.now(UTC))
    assert verify_mcp_token(other, MCP_URL) is not None
    later = auth.monotonic() + PRINCIPAL_CACHE_TTL + 1
    monkeypatch.setattr(auth, "monotonic", lambda: later)
    assert verify_mcp_token(other, MCP_URL) is None
    assert PRINCIPAL_CACHE_TTL == 60


def test_cached_principals_dont_touch_the_database(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """`cached_mcp_principal` is `verify_mcp_token`'s cache alone, for the event loop: it never runs a query"""
    client = create_client(api_client, unique_user)
    token = connect(api_client, unique_user, client)["access_token"]
    _, api_token = _api_token(api_client, unique_user)

    statements: list[str] = []

    def record(conn, cursor, statement: str, *args: Any) -> None:
        statements.append(statement)

    def cached(token: str, resource: str = MCP_URL) -> auth.McpPrincipal | None:
        event.listen(engine, "before_cursor_execute", record)
        try:
            return cached_mcp_principal(token, resource)
        finally:
            event.remove(engine, "before_cursor_execute", record)

    # not verified yet
    assert cached(token) is None
    assert cached(api_token) is None

    principal, api_principal = verify_mcp_token(token, MCP_URL), verify_mcp_token(api_token, MCP_URL)
    assert principal is not None and api_principal is not None
    assert cached(token) is principal
    assert cached(token, f"{ORIGIN}/api/mcp/") is principal
    assert cached(token, "https://other.example/api/mcp") is None  # checked against the audience as ever
    assert cached(api_token, "https://other.example/api/mcp") is api_principal
    for nothing in ("", "x" * 5000, "mmcp_at_" + "x" * 43):
        assert cached(nothing) is None

    # dropped as verify_mcp_token drops them: invalidated, or stale
    invalidate_mcp_principals(token_hash=hash_secret(token))
    assert cached(token) is None
    later = auth.monotonic() + PRINCIPAL_CACHE_TTL + 1
    monkeypatch.setattr(auth, "monotonic", lambda: later)
    assert cached(api_token) is None

    assert statements == []


def test_a_verification_racing_a_revocation_isnt_cached(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    client = create_client(api_client, unique_user)
    token = connect(api_client, unique_user, client)["access_token"]
    verify_access_token = auth._verify_access_token

    def revoked_meanwhile(*args: Any) -> Any:
        entry = verify_access_token(*args)
        _update_token(token, revoked_at=datetime.now(UTC))
        invalidate_mcp_principals(token_hash=hash_secret(token))
        return entry

    monkeypatch.setattr(auth, "_verify_access_token", revoked_meanwhile)
    assert verify_mcp_token(token, MCP_URL) is not None  # read before the revocation
    monkeypatch.undo()
    assert verify_mcp_token(token, MCP_URL) is None


def test_last_use_is_recorded(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    token = connect(api_client, unique_user, client)["access_token"]
    assert verify_mcp_token(token, MCP_URL) is not None

    with session_context() as session:
        row = session.execute(
            sa.select(McpOAuthToken).where(McpOAuthToken.token_hash == hash_secret(token))
        ).scalar_one()
        assert row.last_used_at is not None
        assert row.oauth_client.last_used_at is not None


def _assert_password_change_revokes(api_client: TestClient, user: TestUser, change_password: Callable[[], str]) -> None:
    """
    `change_password()` changes the user's password and returns the new one. Everything is issued right before it,
    most likely in the same second: upstream's `tokens_valid_after` is whole seconds, so comparing times wouldn't do.
    """
    client = create_client(api_client, user)
    tokens = connect(api_client, user, client)
    refreshed = refresh(api_client, client, tokens["refresh_token"]).json()
    code = get_code(api_client, user, client)
    _, api_token = _api_token(api_client, user)
    assert verify_mcp_token(refreshed["access_token"], MCP_URL) is not None  # cached now
    assert len(api_client.get(api_routes.users_self_mcp_connections, headers=user.token).json()) == 1

    new_password = change_password()

    for access_token in (tokens["access_token"], refreshed["access_token"]):
        assert verify_mcp_token(access_token, MCP_URL) is None
    assert refresh(api_client, client, refreshed["refresh_token"]).json()["error"] == "invalid_grant"
    assert exchange(api_client, client, code).json()["error"] == "invalid_grant"
    assert _count(McpOAuthToken, McpOAuthToken.user_id == user.user_id, McpOAuthToken.revoked_at.is_(None)) == 0
    assert _count(McpOAuthCode, McpOAuthCode.user_id == user.user_id) == 0

    # the app no longer counts as connected
    login = api_client.post(api_routes.auth_token, data={"username": user.email, "password": new_password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert api_client.get(api_routes.users_self_mcp_connections, headers=headers).json() == []

    # API tokens outlive a password change, as upstream's do on the REST API
    assert verify_mcp_token(api_token, MCP_URL) is not None


def test_password_change_revokes(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped

    def change_password() -> str:
        response = api_client.put(
            api_routes.users_password,
            json={"currentPassword": user.password, "newPassword": "a-new-password"},
            headers=user.token,
        )
        assert response.status_code == 200, response.text
        return "a-new-password"

    _assert_password_change_revokes(api_client, user, change_password)


def test_password_reset_revokes(api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser):
    """The reset link's flow, here with a reset token an admin made"""
    user = unique_user_fn_scoped

    def reset_password() -> str:
        response = api_client.post(
            api_routes.admin_users_password_reset_token, json={"email": user.email}, headers=admin_token
        )
        assert response.status_code == 201, response.text
        data = {
            "token": response.json()["token"],
            "email": user.email,
            "password": "a-new-password",
            "passwordConfirm": "a-new-password",
        }
        assert api_client.post(api_routes.users_reset_password, json=data).status_code == 200
        return "a-new-password"

    _assert_password_change_revokes(api_client, user, reset_password)


def test_tokens_issued_in_the_second_of_a_password_change_are_refused(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """
    A grant committed while the password change was being committed escapes its revocation. Its tokens are still
    refused: anything issued before the end of the change's second counts as before it.
    """
    user = unique_user_fn_scoped
    client = create_client(api_client, user)
    tokens = connect(api_client, user, client)
    with session_context() as session:
        session.get(User, user.user_id).update_password("a-new-password-hash")
        session.commit()
        changed = session.get(User, user.user_id).tokens_valid_after
    assert changed is not None

    for token in (tokens["access_token"], tokens["refresh_token"]):
        _update_token(token, revoked_at=None, created_at=changed + timedelta(milliseconds=900))
    clear_mcp_principal_cache()

    assert verify_mcp_token(tokens["access_token"], MCP_URL) is None
    response = refresh(api_client, client, tokens["refresh_token"])
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": "The user's password changed since this was issued",
    }


def test_cache_invalidation_waits_for_the_commit(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """A verification made between a password change's flush and its commit isn't trusted after the commit"""
    user = unique_user_fn_scoped
    client = create_client(api_client, user)
    token = connect(api_client, user, client)["access_token"]
    assert verify_mcp_token(token, MCP_URL) is not None  # records the token's use, so the next ones only read
    clear_mcp_principal_cache()

    with session_context() as session:
        session.get(User, user.user_id).update_password("a-new-password-hash")
        session.flush()
        # another request, before the commit: the change isn't visible to it, and it caches what it saw
        assert _in_another_thread(lambda: verify_mcp_token(token, MCP_URL)) is not None
        session.commit()

    assert verify_mcp_token(token, MCP_URL) is None


def test_moving_a_user_applies_to_cached_verifications(
    api_client: TestClient, admin_token: dict, unique_user: TestUser
):
    """The tools act in the principal's household: one an admin moved the user out of mustn't linger in the cache"""
    group = api_client.get(api_routes.groups_self, headers=unique_user.token).json()
    household = api_client.get(api_routes.households_self, headers=unique_user.token).json()
    user = _create_user(api_client, admin_token, group["name"], household["name"])
    tokens = connect(api_client, user, create_client(api_client, user))
    _, api_token = _api_token(api_client, user)
    for token in (tokens["access_token"], api_token):
        principal = verify_mcp_token(token, MCP_URL)  # cached now
        assert principal is not None and str(principal.household_id) == household["id"]

    response = api_client.post(
        api_routes.admin_households, json={"name": random_string(), "groupId": group["id"]}, headers=admin_token
    )
    assert response.status_code == 201, response.text
    moved_to = response.json()
    data = api_client.get(api_routes.admin_users_item_id(user.user_id), headers=admin_token).json()
    response = api_client.put(
        api_routes.admin_users_item_id(user.user_id), json={**data, "household": moved_to["name"]}, headers=admin_token
    )
    assert response.status_code == 200, response.text
    assert response.json()["householdId"] == moved_to["id"]

    for token in (tokens["access_token"], api_token):
        principal = verify_mcp_token(token, MCP_URL)
        assert principal is not None and str(principal.household_id) == moved_to["id"]


def test_invalidations_wait_for_the_commit(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Deletions and password changes made anywhere invalidate cached verifications once committed, and only then"""
    user = unique_user_fn_scoped
    invalidated: list[dict[str, Any]] = []
    monkeypatch.setattr(auth, "invalidate_mcp_principals", lambda **kwargs: invalidated.append(kwargs))
    token_id, _ = _api_token(api_client, user)

    with session_context() as session:
        session.delete(session.get(LongLiveToken, token_id))
        session.get(User, user.user_id).update_password("a-new-password-hash")
        session.flush()
        assert invalidated == []
        # rolled back: nothing changed, so nothing is invalidated, then or at the session's next commit
        session.rollback()
        session.commit()
        assert invalidated == []

        session.delete(session.get(LongLiveToken, token_id))
        session.get(User, user.user_id).update_password("a-new-password-hash")
        session.flush()
        assert invalidated == []
        session.commit()

    assert sorted(invalidated, key=str) == [{"api_token_id": token_id}, {"user_id": user.user_id}]


# ==========================================
# Deletions


def _create_user(api_client: TestClient, admin_token: dict, group: str, household: str) -> TestUser:
    data = {
        "fullName": random_string(),
        "username": random_string(),
        "email": f"{random_string()}@example.com",
        "password": "useruser",
        "group": group,
        "household": household,
        "admin": False,
        "canManage": True,
        "tokens": [],
    }
    response = api_client.post(api_routes.admin_users, json=data, headers=admin_token)
    assert response.status_code == 201, response.text
    login = api_client.post(api_routes.auth_token, data={"username": data["email"], "password": "useruser"})
    token = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = api_client.get(api_routes.users_self, headers=token).json()
    return TestUser(
        email=data["email"],
        user_id=UUID(me["id"]),
        username=data["username"],
        full_name=data["fullName"],
        password="useruser",
        _group_id=me["groupId"],
        _household_id=me["householdId"],
        token=token,
        repos=None,  # type: ignore[arg-type]
    )


def test_deleting_a_user_deletes_their_rows(api_client: TestClient, admin_token: dict, unique_user: TestUser):
    group = api_client.get(api_routes.groups_self, headers=unique_user.token).json()
    household = api_client.get(api_routes.households_self, headers=unique_user.token).json()
    user = _create_user(api_client, admin_token, group["name"], household["name"])

    # a client the user created stays with the group
    client = create_client(api_client, user)
    tokens = connect(api_client, user, client)
    get_code(api_client, user, client)
    token_id, api_token = _api_token(api_client, user)
    api_client.put(
        api_routes.users_self_mcp_api_tokens_token_id(token_id), json={"allowWrites": True}, headers=user.token
    )
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None
    assert verify_mcp_token(api_token, MCP_URL) is not None
    assert _count(McpOAuthCode, McpOAuthCode.user_id == user.user_id) == 2  # one used, one not
    assert _count(McpApiTokenGrant, McpApiTokenGrant.long_live_token_id == token_id) == 1

    response = api_client.delete(api_routes.admin_users_item_id(user.user_id), headers=admin_token)
    assert response.status_code == 200, response.text

    assert verify_mcp_token(tokens["access_token"], MCP_URL) is None
    assert verify_mcp_token(api_token, MCP_URL) is None
    assert _count(McpOAuthToken, McpOAuthToken.user_id == user.user_id) == 0
    assert _count(McpOAuthCode, McpOAuthCode.user_id == user.user_id) == 0
    assert _count(McpApiTokenGrant, McpApiTokenGrant.long_live_token_id == token_id) == 0

    response = api_client.get(api_routes.groups_mcp_clients_item_id(client["id"]), headers=unique_user.token)
    assert response.status_code == 200
    assert response.json()["createdBy"] is None


def test_deleting_an_api_token_deletes_its_grant(api_client: TestClient, unique_user: TestUser):
    token_id, token = _api_token(api_client, unique_user)
    url = api_routes.users_self_mcp_api_tokens_token_id(token_id)
    api_client.put(url, json={"allowWrites": True}, headers=unique_user.token)
    principal = verify_mcp_token(token, MCP_URL)
    assert principal is not None and principal.can_write

    response = api_client.delete(api_routes.users_api_tokens_token_id(token_id), headers=unique_user.token)
    assert response.status_code == 200

    assert verify_mcp_token(token, MCP_URL) is None
    assert _count(McpApiTokenGrant, McpApiTokenGrant.long_live_token_id == token_id) == 0
    assert api_client.get(url, headers=unique_user.token).status_code == 404


def test_deleting_a_group_deletes_its_clients(api_client: TestClient, admin_token: dict, unique_user: TestUser):
    response = api_client.post(api_routes.admin_groups, json={"name": random_string()}, headers=admin_token)
    assert response.status_code == 201
    group = response.json()

    # created through the service: a group needs a manager for the API, and a group with users can't be deleted
    with session_context() as session:
        client = McpClientService(session, group["id"]).create(
            McpClientCreate(name="Orphan", redirect_uris=[HA_REDIRECT_URI], pkce_optional=True), created_by=None
        )
    consent_handle(authorize(api_client, authorize_params(client.model_dump(by_alias=True))))
    assert _count(McpOAuthRequest, McpOAuthRequest.oauth_client_id == client.id) == 1

    response = api_client.delete(api_routes.admin_groups_item_id(group["id"]), headers=admin_token)
    assert response.status_code == 200, response.text

    assert _count(McpOAuthClient, McpOAuthClient.id == client.id) == 0
    assert _count(McpOAuthRequest, McpOAuthRequest.oauth_client_id == client.id) == 0


# ==========================================
# 401 header


def test_www_authenticate():
    assert mcp_www_authenticate("https://mealie.example") == (
        'Bearer error="invalid_token", '
        'resource_metadata="https://mealie.example/.well-known/oauth-protected-resource/api/mcp", '
        'scope="mcp:read mcp:write"'
    )
    assert mcp_www_authenticate("http://testserver", error=None) == (
        'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource/api/mcp", '
        'scope="mcp:read mcp:write"'
    )


# ==========================================
# Home Assistant, end to end


def test_home_assistant_flow(api_client: TestClient, unique_user: TestUser):
    """
    Home Assistant's client (homeassistant/components/mcp, LocalOAuth2Implementation): a confidential client with
    no PKCE and no resource, client_secret_post, refresh with rotation, and reauthorization after a revocation
    """
    # a manager adds the preset; the user enters the client ID and secret in HA's Application Credentials
    preset = api_client.get(api_routes.groups_mcp_presets_home_assistant, headers=unique_user.token).json()
    client = create_client(api_client, unique_user, **preset)

    # 1. authorize: HA's exact parameters, scope from the 401's hint
    params = {
        "response_type": "code",
        "client_id": client["clientId"],
        "redirect_uri": HA_REDIRECT_URI,
        "state": HA_STATE,
        "access_type": "offline",
        "prompt": "consent",
        "scope": "mcp:read mcp:write",
    }
    handle = consent_handle(authorize(api_client, params))

    # 2. consent
    request = api_client.get(api_routes.oauth_requests_handle(handle), headers=unique_user.token).json()
    assert (request["clientName"], request["writesOffered"]) == ("Home Assistant", False)
    redirect_to = decide(api_client, unique_user, handle).json()["redirectTo"]
    callback = query(redirect_to)
    assert callback["state"] == HA_STATE
    assert callback["iss"] == ORIGIN

    # 3. code exchange, client_secret_post with grant_type, code, redirect_uri
    response = exchange(api_client, client, callback["code"])
    assert response.status_code == 200
    tokens = response.json()
    assert tokens["expires_in"] == 3600 and tokens["refresh_token"]

    # 4. refresh (HA merges the response into what it has)
    response = refresh(api_client, client, tokens["refresh_token"])
    assert response.status_code == 200
    tokens = {**tokens, **response.json()}

    # 5. the MCP endpoint accepts the access token
    principal = verify_mcp_token(tokens["access_token"], MCP_URL)
    assert principal is not None
    assert (principal.user.id, principal.client_name, principal.can_write) == (
        unique_user.user_id,
        "Home Assistant",
        False,
    )

    # 6. revoked (e.g. disconnected): the endpoint refuses it, and HA's refresh gets a 4xx, so it reauthorizes
    response = api_client.post(
        api_routes.oauth_revoke,
        data={
            "token": tokens["refresh_token"],
            "client_id": client["clientId"],
            "client_secret": client["clientSecret"],
        },
    )
    assert response.status_code == 200
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is None
    assert refresh(api_client, client, tokens["refresh_token"]).status_code == 400

    tokens = connect(api_client, unique_user, client)
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None
