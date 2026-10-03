"""
Group managers' MCP OAuth clients, users' connected apps and API token write grants (docs/ai/PHASE3.md §3-4, §6):
permissions and isolation
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from mealie.services.ai.mcp.auth import verify_mcp_token
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser
from tests.utils.mcp_oauth import (
    HA_LOCAL_REDIRECT_URI,
    HA_REDIRECT_URI,
    MCP_URL,
    connect,
    create_client,
    exchange,
    get_code,
    refresh,
)

CLIENT_FIELDS = {
    "id",
    "groupId",
    "name",
    "clientId",
    "isConfidential",
    "pkceOptional",
    "allowWriteScope",
    "redirectUris",
    "createdBy",
    "createdAt",
    "lastUsedAt",
}


def _client_update(client: dict[str, Any], **changes: Any) -> dict[str, Any]:
    fields = ("name", "redirectUris", "pkceOptional", "allowWriteScope")
    return {**{k: client[k] for k in fields}, **changes}


# ==========================================
# Clients


def test_client_crud(api_client: TestClient, unique_user: TestUser):
    created = create_client(api_client, unique_user, name="  Kitchen HA  ")
    assert set(created) == CLIENT_FIELDS | {"clientSecret"}
    assert created["name"] == "Kitchen HA"
    assert created["groupId"] == unique_user.group_id
    assert created["createdBy"] == str(unique_user.user_id)
    assert created["clientId"].startswith("mmcp_")
    assert created["clientSecret"].startswith("mmcp_cs_")
    assert created["redirectUris"] == [HA_REDIRECT_URI, HA_LOCAL_REDIRECT_URI]

    # the secret is shown once
    response = api_client.get(api_routes.groups_mcp_clients_item_id(created["id"]), headers=unique_user.token)
    assert response.status_code == 200
    one = response.json()
    assert set(one) == CLIENT_FIELDS
    assert created["clientSecret"] not in response.text

    listed = api_client.get(api_routes.groups_mcp_clients, headers=unique_user.token)
    assert listed.status_code == 200
    assert one in listed.json()
    assert created["clientSecret"] not in listed.text

    update = _client_update(one, name="Renamed", allowWriteScope=True, redirectUris=[HA_REDIRECT_URI, HA_REDIRECT_URI])
    response = api_client.put(api_routes.groups_mcp_clients_item_id(one["id"]), json=update, headers=unique_user.token)
    assert response.status_code == 200
    updated = response.json()
    assert (updated["name"], updated["allowWriteScope"], updated["redirectUris"]) == (
        "Renamed",
        True,
        [HA_REDIRECT_URI],
    )
    assert (updated["clientId"], updated["isConfidential"]) == (one["clientId"], True)

    response = api_client.delete(api_routes.groups_mcp_clients_item_id(one["id"]), headers=unique_user.token)
    assert response.status_code == 200
    assert response.json()["id"] == one["id"]
    assert (
        api_client.get(api_routes.groups_mcp_clients_item_id(one["id"]), headers=unique_user.token).status_code == 404
    )
    assert (
        api_client.delete(api_routes.groups_mcp_clients_item_id(one["id"]), headers=unique_user.token).status_code
        == 404
    )  # noqa: E501


@pytest.mark.parametrize(
    "changes",
    [
        {"redirectUris": ["http://example.com/cb"]},  # plain http to a public host
        {"redirectUris": ["https://example.com/cb#fragment"]},
        {"redirectUris": ["myapp://callback"]},
        {"redirectUris": ["/relative"]},
        {"redirectUris": []},
        {"redirectUris": [f"https://example.com/cb{i}" for i in range(11)]},
        {"name": "   "},
        {"name": "x" * 101},
        {"isConfidential": False, "pkceOptional": True},
    ],
)
def test_client_validation(api_client: TestClient, unique_user: TestUser, changes: dict[str, Any]):
    data = {
        "name": "Client",
        "redirectUris": [HA_REDIRECT_URI],
        "isConfidential": True,
        "pkceOptional": False,
        **changes,
    }
    response = api_client.post(api_routes.groups_mcp_clients, json=data, headers=unique_user.token)
    assert response.status_code == 422


def test_rotate_secret(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)

    response = api_client.post(
        api_routes.groups_mcp_clients_item_id_rotate_secret(client["id"]), headers=unique_user.token
    )
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["clientId"] == client["clientId"]
    assert rotated["clientSecret"].startswith("mmcp_cs_") and rotated["clientSecret"] != client["clientSecret"]

    # the old secret stops working, issued tokens don't
    assert refresh(api_client, client, tokens["refresh_token"]).json()["error"] == "invalid_client"
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None
    assert refresh(api_client, {**client, **rotated}, tokens["refresh_token"]).status_code == 200

    public = create_client(api_client, unique_user, isConfidential=False, pkceOptional=False)
    response = api_client.post(
        api_routes.groups_mcp_clients_item_id_rotate_secret(public["id"]), headers=unique_user.token
    )
    assert response.status_code == 400


def test_deleting_a_client_revokes_its_tokens(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user)
    tokens = connect(api_client, unique_user, client)
    code = get_code(api_client, unique_user, client)
    assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None  # cached now

    response = api_client.delete(api_routes.groups_mcp_clients_item_id(client["id"]), headers=unique_user.token)
    assert response.status_code == 200

    assert verify_mcp_token(tokens["access_token"], MCP_URL) is None
    assert refresh(api_client, client, tokens["refresh_token"]).status_code == 401
    assert exchange(api_client, client, code).status_code == 401


def test_taking_away_writes_applies_to_issued_tokens(api_client: TestClient, unique_user: TestUser):
    client = create_client(api_client, unique_user, allowWriteScope=True)
    tokens = connect(api_client, unique_user, client, allow_writes=True)
    principal = verify_mcp_token(tokens["access_token"], MCP_URL)
    assert principal is not None and principal.can_write

    update = _client_update(client, allowWriteScope=False)
    response = api_client.put(
        api_routes.groups_mcp_clients_item_id(client["id"]), json=update, headers=unique_user.token
    )
    assert response.status_code == 200

    principal = verify_mcp_token(tokens["access_token"], MCP_URL)
    assert principal is not None and not principal.can_write
    assert "mcp:write" in principal.scopes


def test_home_assistant_preset(api_client: TestClient, unique_user: TestUser):
    response = api_client.get(api_routes.groups_mcp_presets_home_assistant, headers=unique_user.token)
    assert response.status_code == 200
    preset = response.json()
    assert preset == {
        "name": "Home Assistant",
        "redirectUris": [
            "https://my.home-assistant.io/redirect/oauth",
            "http://homeassistant.local:8123/auth/external/callback",
        ],
        "pkceOptional": True,
        "allowWriteScope": False,
        "isConfidential": True,
    }

    response = api_client.get(
        api_routes.groups_mcp_presets_home_assistant,
        params={"homeAssistantUrl": "http://192.168.1.20:8123/"},
        headers=unique_user.token,
    )
    assert response.json()["redirectUris"][1] == "http://192.168.1.20:8123/auth/external/callback"

    response = api_client.get(
        api_routes.groups_mcp_presets_home_assistant,
        params={"homeAssistantUrl": "http://ha.example.com"},
        headers=unique_user.token,
    )
    assert response.status_code == 400
    assert "https://" in response.json()["detail"]["message"]

    # the preset is what Home Assistant needs: it works as is
    client = create_client(api_client, unique_user, **preset)
    assert connect(api_client, unique_user, client)["scope"] == "mcp:read"


def test_clients_are_for_managers(api_client: TestClient, unique_user: TestUser, h2_user: TestUser):
    client = create_client(api_client, unique_user)
    item = api_routes.groups_mcp_clients_item_id(client["id"])

    # h2_user is in the same group, but isn't a manager
    for response in [
        api_client.get(api_routes.groups_mcp_clients, headers=h2_user.token),
        api_client.post(
            api_routes.groups_mcp_clients, json={"name": "x", "redirectUris": [HA_REDIRECT_URI]}, headers=h2_user.token
        ),  # noqa: E501
        api_client.get(item, headers=h2_user.token),
        api_client.put(item, json=_client_update(client), headers=h2_user.token),
        api_client.delete(item, headers=h2_user.token),
        api_client.post(api_routes.groups_mcp_clients_item_id_rotate_secret(client["id"]), headers=h2_user.token),
        api_client.get(api_routes.groups_mcp_presets_home_assistant, headers=h2_user.token),
    ]:
        assert response.status_code == 403

    assert api_client.get(api_routes.groups_mcp_clients).status_code == 401
    assert api_client.get(item, headers=unique_user.token).status_code == 200


def test_clients_are_isolated_by_group(api_client: TestClient, unique_user: TestUser, unique_user_fn_scoped: TestUser):
    # another group's manager
    g2_user = unique_user_fn_scoped
    assert g2_user.group_id != unique_user.group_id
    client = create_client(api_client, unique_user)
    other = create_client(api_client, g2_user)
    item = api_routes.groups_mcp_clients_item_id(client["id"])

    listed = api_client.get(api_routes.groups_mcp_clients, headers=g2_user.token).json()
    assert other["id"] in [c["id"] for c in listed]
    assert client["id"] not in [c["id"] for c in listed]

    assert api_client.get(item, headers=g2_user.token).status_code == 404
    assert api_client.put(item, json=_client_update(client), headers=g2_user.token).status_code == 404
    assert (
        api_client.post(
            api_routes.groups_mcp_clients_item_id_rotate_secret(client["id"]), headers=g2_user.token
        ).status_code
        == 404
    )
    assert api_client.delete(item, headers=g2_user.token).status_code == 404
    assert api_client.get(item, headers=unique_user.token).status_code == 200


# ==========================================
# Connected apps


def test_connections(api_client: TestClient, unique_user: TestUser, h2_user: TestUser):
    client = create_client(api_client, unique_user, name="Kitchen", allowWriteScope=True)
    first = connect(api_client, unique_user, client)
    second = connect(api_client, unique_user, client, allow_writes=True)
    other_user = connect(api_client, h2_user, client)

    response = api_client.get(api_routes.users_self_mcp_connections, headers=unique_user.token)
    assert response.status_code == 200
    (connection,) = [c for c in response.json() if c["clientId"] == client["id"]]
    assert connection["clientName"] == "Kitchen"
    assert connection["scopes"] == ["mcp:read", "mcp:write"]
    assert connection["createdAt"] and connection["lastUsedAt"]

    # h2_user only sees their own
    (theirs,) = [
        c
        for c in api_client.get(api_routes.users_self_mcp_connections, headers=h2_user.token).json()
        if c["clientId"] == client["id"]
    ]
    assert theirs["scopes"] == ["mcp:read"]

    # disconnecting revokes all of the user's tokens for the client, and only theirs
    for tokens in (first, second):
        assert verify_mcp_token(tokens["access_token"], MCP_URL) is not None
    response = api_client.delete(
        api_routes.users_self_mcp_connections_client_id(client["id"]), headers=unique_user.token
    )  # noqa: E501
    assert response.status_code == 200
    # (checking the tokens above recorded a later use)
    assert {**response.json(), "lastUsedAt": None} == {**connection, "lastUsedAt": None}

    for tokens in (first, second):
        assert verify_mcp_token(tokens["access_token"], MCP_URL) is None
        assert refresh(api_client, client, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert verify_mcp_token(other_user["access_token"], MCP_URL) is not None

    listed = api_client.get(api_routes.users_self_mcp_connections, headers=unique_user.token).json()
    assert client["id"] not in [c["clientId"] for c in listed]
    assert (
        api_client.delete(
            api_routes.users_self_mcp_connections_client_id(client["id"]), headers=unique_user.token
        ).status_code
        == 404
    )  # noqa: E501

    # nor can anyone disconnect someone else's connection
    assert api_client.get(api_routes.users_self_mcp_connections).status_code == 401


# ==========================================
# API token write grants


def _api_token(api_client: TestClient, user: TestUser) -> tuple[int, str]:
    response = api_client.post(api_routes.users_api_tokens, json={"name": "Claude"}, headers=user.token)
    assert response.status_code == 201
    return response.json()["id"], response.json()["token"]


def test_api_token_write_grants(api_client: TestClient, unique_user: TestUser):
    token_id, token = _api_token(api_client, unique_user)
    url = api_routes.users_self_mcp_api_tokens_token_id(token_id)

    response = api_client.get(url, headers=unique_user.token)
    assert response.status_code == 200
    assert response.json() == {"tokenId": token_id, "allowWrites": False}
    principal = verify_mcp_token(token, MCP_URL)
    assert principal is not None
    assert (principal.can_write, principal.scopes, principal.client_name) == (False, {"mcp:read"}, "API token")
    assert (principal.api_token_id, principal.client_id) == (token_id, None)

    response = api_client.put(url, json={"allowWrites": True}, headers=unique_user.token)
    assert response.status_code == 200
    assert response.json() == {"tokenId": token_id, "allowWrites": True}
    principal = verify_mcp_token(token, MCP_URL)
    assert principal is not None
    assert (principal.can_write, principal.scopes) == (True, {"mcp:read", "mcp:write"})

    listed = api_client.get(api_routes.users_self_mcp_api_tokens, headers=unique_user.token).json()
    assert {"tokenId": token_id, "allowWrites": True} in listed

    response = api_client.put(url, json={"allowWrites": False}, headers=unique_user.token)
    assert response.json()["allowWrites"] is False
    principal = verify_mcp_token(token, MCP_URL)
    assert principal is not None and not principal.can_write


def test_api_token_grants_are_the_owners(
    api_client: TestClient, unique_user: TestUser, h2_user: TestUser, g2_user: TestUser
):
    token_id, token = _api_token(api_client, unique_user)
    url = api_routes.users_self_mcp_api_tokens_token_id(token_id)

    for user in (h2_user, g2_user):
        assert api_client.get(url, headers=user.token).status_code == 404
        assert api_client.put(url, json={"allowWrites": True}, headers=user.token).status_code == 404
        listed = api_client.get(api_routes.users_self_mcp_api_tokens, headers=user.token).json()
        assert token_id not in [grant["tokenId"] for grant in listed]

    assert (
        api_client.get(api_routes.users_self_mcp_api_tokens_token_id(10**9), headers=unique_user.token).status_code
        == 404
    )  # noqa: E501
    principal = verify_mcp_token(token, MCP_URL)
    assert principal is not None and not principal.can_write
