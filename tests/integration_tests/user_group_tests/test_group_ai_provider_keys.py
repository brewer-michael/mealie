"""
Fork changes to upstream's AI provider endpoints (docs/ai/PHASE1.md): whether a saved key is readable,
where a saved key may be sent, and input checks.
"""

from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.db.models._model_utils.guid import GUID
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut
from mealie.schema.openai.general import OpenAIText
from mealie.services.openai import OpenAIService
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser


def _create_provider(user: TestUser, api_key: str = "saved-key", name: str = "", **kwargs: Any) -> AIProviderOut:
    return user.repos.group_ai_providers.create(
        AIProviderCreate(name=name or random_string(), model="gpt-4o", api_key=api_key, **kwargs)
    )


def _set_raw_api_key(provider_id: UUID, value: str) -> None:
    with session_context() as session:
        guid = GUID.convert_value_to_guid(provider_id, session.get_bind().dialect)
        session.execute(
            sa.text("UPDATE ai_providers SET api_key = :value WHERE id = :id"), {"value": value, "id": guid}
        )
        session.commit()


def _record_pings(monkeypatch: pytest.MonkeyPatch) -> list[AIProviderOut]:
    """Stands in for `OpenAIService.ping`, recording the provider each connection test used"""
    seen: list[AIProviderOut] = []

    async def fake_ping(self: OpenAIService, provider: AIProviderOut, message: str, images: Any = None) -> OpenAIText:
        seen.append(provider)
        return OpenAIText(text="Tomato & Egg Stir-Fry")

    monkeypatch.setattr(OpenAIService, "ping", fake_ping)
    return seen


# ==========================================
# api_key_set


def test_api_key_set_shows_whether_the_saved_key_is_readable(api_client: TestClient, unique_user: TestUser):
    provider = _create_provider(unique_user, api_key="sk-readable")

    def read() -> tuple[dict, dict]:
        one = api_client.get(
            api_routes.groups_ai_providers_providers_provider_id(provider.id), headers=unique_user.token
        )
        listed = api_client.get(api_routes.groups_ai_providers_providers, headers=unique_user.token)
        assert one.status_code == listed.status_code == 200
        assert "sk-readable" not in one.text + listed.text
        assert "apiKey" not in one.json()

        (listed_provider,) = [p for p in listed.json() if p["id"] == str(provider.id)]
        assert listed_provider == one.json()
        return one.json(), listed_provider

    try:
        one, listed = read()
        assert one["apiKeySet"] is listed["apiKeySet"] is True

        # e.g. after `.secret` changed: the key can't be decrypted and reads as ""
        _set_raw_api_key(provider.id, "enc:v1:not-a-valid-token")
        one, listed = read()
        assert one["apiKeySet"] is listed["apiKeySet"] is False

        # Every group member can read /groups/self, so it doesn't say
        response = api_client.get(api_routes.groups_self, headers=unique_user.token)
        (summary,) = [p for p in response.json()["aiProviderSettings"]["providers"] if p["id"] == str(provider.id)]
        assert set(summary) == {"id", "name"}
    finally:
        unique_user.repos.group_ai_providers.delete(provider.id)


def test_provider_list(api_client: TestClient, unique_user: TestUser, g2_user: TestUser):
    names = ["b-provider", "A-provider", "c-provider"]
    providers = [_create_provider(unique_user, name=name) for name in names]
    foreign = _create_provider(g2_user)

    try:
        response = api_client.get(api_routes.groups_ai_providers_providers, headers=unique_user.token)
        assert response.status_code == 200
        assert [p["name"] for p in response.json()] == ["A-provider", "b-provider", "c-provider"]
        assert str(foreign.id) not in response.text
    finally:
        for provider in providers:
            unique_user.repos.group_ai_providers.delete(provider.id)
        g2_user.repos.group_ai_providers.delete(foreign.id)


def test_provider_list_requires_can_manage(api_client: TestClient, user_tuple: list[TestUser]):
    usr, _ = user_tuple
    db_user = usr.repos.users.get_one(usr.user_id)
    assert db_user
    db_user.can_manage = False
    usr.repos.users.update(db_user.id, db_user)

    response = api_client.get(api_routes.groups_ai_providers_providers, headers=usr.token)
    assert response.status_code == 403


# ==========================================
# Saved-provider connection test: where the saved key may go


def test_test_saved_provider_needs_the_key_again_for_a_new_destination(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    seen = _record_pings(monkeypatch)
    provider = _create_provider(unique_user, base_url="https://saved.example.test/v1", request_headers={"X-A": "1"})
    route = api_routes.groups_ai_providers_providers_provider_id_test(provider.id)
    unchanged = {
        "name": provider.name,
        "model": "gpt-4o-mini",
        "baseUrl": provider.base_url,
        "requestHeaders": {"X-A": "1"},
    }

    try:
        for changes in [
            {"baseUrl": "https://elsewhere.example.test/v1"},
            {"baseUrl": None},
            {"protocol": "anthropic"},
            {"requestHeaders": {}},
        ]:
            response = api_client.post(route, json={**unchanged, **changes}, headers=unique_user.token)
            assert response.status_code == 400, changes
            assert "API key again" in response.json()["detail"]["message"]
        assert seen == []

        # Same destination: the saved key is used
        response = api_client.post(route, json=unchanged, headers=unique_user.token)
        assert response.status_code == 200
        assert {(p.base_url, p.api_key, p.model) for p in seen} == {(provider.base_url, "saved-key", "gpt-4o-mini")}

        # A new key can go anywhere
        seen.clear()
        response = api_client.post(
            route,
            json={**unchanged, "baseUrl": "https://elsewhere.example.test/v1", "apiKey": "sk-new"},
            headers=unique_user.token,
        )
        assert response.status_code == 200
        assert {(p.base_url, p.api_key) for p in seen} == {("https://elsewhere.example.test/v1", "sk-new")}
    finally:
        unique_user.repos.group_ai_providers.delete(provider.id)


# ==========================================
# Input checks


@pytest.mark.parametrize("base_url", ["https://internal.test/admin?q=", "https://internal.test/v1#x"])
def test_base_url_cannot_have_a_query_or_fragment(api_client: TestClient, unique_user: TestUser, base_url: str):
    provider = _create_provider(unique_user)
    data = {"name": random_string(), "model": "gpt-4o", "apiKey": "sk-test", "baseUrl": base_url}

    try:
        for method, route in [
            ("post", api_routes.groups_ai_providers_providers),
            ("post", api_routes.groups_ai_providers_providers_test),
            ("put", api_routes.groups_ai_providers_providers_provider_id(provider.id)),
            ("post", api_routes.groups_ai_providers_providers_provider_id_test(provider.id)),
        ]:
            response = getattr(api_client, method)(route, json=data, headers=unique_user.token)
            assert response.status_code == 422, route
    finally:
        unique_user.repos.group_ai_providers.delete(provider.id)


def test_monthly_token_limit_fits_the_column(api_client: TestClient, unique_user: TestUser):
    """The column is a 32-bit INTEGER on PostgreSQL; a larger value must be a 422, not a 500"""
    data = {"name": random_string(), "model": "gpt-4o", "apiKey": "sk-test"}

    response = api_client.post(
        api_routes.groups_ai_providers_providers,
        json={**data, "monthlyTokenLimit": 3_000_000_000},
        headers=unique_user.token,
    )
    assert response.status_code == 422

    response = api_client.post(
        api_routes.groups_ai_providers_providers,
        json={**data, "monthlyTokenLimit": 2_147_483_647},
        headers=unique_user.token,
    )
    assert response.status_code == 200
    provider_id = response.json()["id"]
    try:
        response = api_client.get(
            api_routes.groups_ai_providers_providers_provider_id(provider_id), headers=unique_user.token
        )
        assert response.json()["monthlyTokenLimit"] == 2_147_483_647
    finally:
        unique_user.repos.group_ai_providers.delete(provider_id)
