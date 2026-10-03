"""Integration tests for AI provider fallback routes, the usage summary and model lists (docs/ai/PHASE1.md)"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from mealie.db.db_setup import session_context
from mealie.db.models.group.ai_routing import AIProviderRoute, AIUsageLog
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_ai_routing import month_range
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.services.openai import OpenAIService
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

ALL_SLOTS = {slot.value for slot in AIProviderSlot}


def _create_provider(user: TestUser, name: str | None = None, **kwargs: Any) -> AIProviderOut:
    return user.repos.group_ai_providers.create(
        AIProviderCreate(name=name or random_string(), model="gpt-4o", api_key="saved-key", **kwargs)
    )


def _put_routes(api_client: TestClient, user: TestUser, routes: dict[str, list[Any]]) -> httpx.Response:
    return api_client.put(
        api_routes.groups_ai_providers_routes,
        json={"routes": {slot: [str(x) for x in ids] for slot, ids in routes.items()}},
        headers=user.token,
    )


def _log_usage(
    repos: AllRepositories,
    provider: AIProviderOut | None,
    created_at: datetime,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    success: bool = True,
    name: str | None = None,
    model: str | None = None,
) -> None:
    entry = AIUsageLogCreate(
        provider_id=provider.id if provider else None,
        provider_name=name or (provider.name if provider else "deleted"),
        model=model or (provider.model if provider else "unknown"),
        protocol="openai",
        slot=AIProviderSlot.default,
        feature="OpenAIRecipe",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=100,
        success=success,
        error_type=None if success else "AuthenticationError",
    )
    repos.group_ai_usage.create({**entry.model_dump(), "created_at": created_at})


def _revoke_can_manage(user: TestUser) -> None:
    db_user = user.repos.users.get_one(user.user_id)
    assert db_user
    db_user.can_manage = False
    user.repos.users.update(db_user.id, db_user)


# ==========================================
# Routes


def test_get_routes_lists_every_slot(api_client: TestClient, unique_user_fn_scoped: TestUser):
    response = api_client.get(api_routes.groups_ai_providers_routes, headers=unique_user_fn_scoped.token)
    assert response.status_code == 200
    assert response.json() == {"routes": dict.fromkeys(ALL_SLOTS, [])}


def test_put_routes_replaces_only_the_slots_given(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    p1, p2, p3 = (_create_provider(user) for _ in range(3))

    response = _put_routes(api_client, user, {"default": [p2.id, p1.id], "fast": [p3.id], "embedding": [p1.id]})
    assert response.status_code == 200
    routes = response.json()["routes"]
    assert set(routes) == ALL_SLOTS
    assert routes["default"] == [str(p2.id), str(p1.id)]
    assert routes["fast"] == [str(p3.id)]
    assert routes["embedding"] == [str(p1.id)]
    assert routes["image"] == []

    # Reorder one slot and clear another; the rest stay as they were
    response = _put_routes(api_client, user, {"default": [p1.id, p3.id, p2.id], "fast": []})
    assert response.status_code == 200

    routes = api_client.get(api_routes.groups_ai_providers_routes, headers=user.token).json()["routes"]
    assert routes["default"] == [str(p1.id), str(p3.id), str(p2.id)]
    assert routes["fast"] == []
    assert routes["embedding"] == [str(p1.id)]

    # The repository returns the same thing for the router to use
    assert user.repos.group_ai_provider_routes.get_routes()[AIProviderSlot.default] == [p1.id, p3.id, p2.id]


def test_put_routes_drops_duplicates(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    p1, p2 = _create_provider(user), _create_provider(user)

    response = _put_routes(api_client, user, {"image": [p1.id, p2.id, p1.id, p2.id]})
    assert response.status_code == 200
    assert response.json()["routes"]["image"] == [str(p1.id), str(p2.id)]


def test_put_routes_rejects_another_groups_provider(
    api_client: TestClient, unique_user_fn_scoped: TestUser, g2_user: TestUser
):
    user = unique_user_fn_scoped
    own = _create_provider(user)
    foreign = _create_provider(g2_user)

    try:
        assert _put_routes(api_client, user, {"default": [own.id]}).status_code == 200

        response = _put_routes(api_client, user, {"default": [foreign.id], "fast": [own.id]})
        assert response.status_code == 400

        response = _put_routes(api_client, user, {"default": [uuid4()]})
        assert response.status_code == 400

        # Nothing changed
        routes = api_client.get(api_routes.groups_ai_providers_routes, headers=user.token).json()["routes"]
        assert routes["default"] == [str(own.id)]
        assert routes["fast"] == []
    finally:
        g2_user.repos.group_ai_providers.delete(foreign.id)


def test_put_routes_rejects_unknown_slot(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    provider = _create_provider(user)

    response = _put_routes(api_client, user, {"not-a-slot": [provider.id]})
    assert response.status_code == 422


def test_deleting_provider_removes_its_routes(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    p1, p2, p3 = (_create_provider(user) for _ in range(3))
    assert _put_routes(api_client, user, {"default": [p1.id, p2.id, p3.id], "image": [p2.id]}).status_code == 200

    response = api_client.delete(api_routes.groups_ai_providers_providers_provider_id(p2.id), headers=user.token)
    assert response.status_code == 200

    routes = api_client.get(api_routes.groups_ai_providers_routes, headers=user.token).json()["routes"]
    assert routes["default"] == [str(p1.id), str(p3.id)]
    assert routes["image"] == []

    # The remaining providers can still be routed, including at the freed positions
    assert _put_routes(api_client, user, {"default": [p3.id, p1.id]}).status_code == 200


def test_deleting_provider_keeps_its_usage_history(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    provider = _create_provider(user, name="doomed", monthly_token_limit=100)
    _log_usage(user.repos, provider, datetime.now(UTC), prompt_tokens=7, completion_tokens=3)

    response = api_client.delete(api_routes.groups_ai_providers_providers_provider_id(provider.id), headers=user.token)
    assert response.status_code == 200

    with session_context() as session:
        rows = session.execute(
            sa.select(AIUsageLog.provider_id, AIUsageLog.provider_name).where(
                AIUsageLog.group_id == UUID(user.group_id)
            )
        ).all()
    assert [tuple(row) for row in rows] == [(None, "doomed")]

    summary = api_client.get(api_routes.groups_ai_providers_usage, headers=user.token).json()
    assert summary["byProvider"] == [
        {
            "providerId": None,
            "providerName": "doomed",
            "model": "gpt-4o",
            "requests": 1,
            "failures": 0,
            "promptTokens": 7,
            "completionTokens": 3,
            "monthlyTokenLimit": None,
            "lastUsedAt": summary["byProvider"][0]["lastUsedAt"],
        }
    ]


def test_deleting_a_group_removes_its_routes_and_usage(api_client: TestClient, admin_token: dict):
    response = api_client.post(api_routes.admin_groups, json={"name": random_string()}, headers=admin_token)
    assert response.status_code == 201
    group_id = UUID(response.json()["id"])

    with session_context() as session:
        repos = get_repositories(session, group_id=group_id, household_id=None)
        provider = repos.group_ai_providers.create(
            AIProviderCreate(name=random_string(), model="gpt-4o", api_key="key")
        )
        repos.group_ai_provider_routes.replace_routes({AIProviderSlot.default: [provider.id]})
        _log_usage(repos, provider, datetime.now(UTC), prompt_tokens=1)

    response = api_client.delete(api_routes.admin_groups_item_id(group_id), headers=admin_token)
    assert response.status_code == 200

    with session_context() as session:
        usage = session.execute(sa.select(AIUsageLog.id).where(AIUsageLog.group_id == group_id)).all()
        routes = session.execute(sa.select(AIProviderRoute.id).where(AIProviderRoute.provider_id == provider.id)).all()
    assert usage == []
    assert routes == []


# ==========================================
# Usage


def test_usage_summary_aggregates(api_client: TestClient, unique_user_fn_scoped: TestUser, g2_user: TestUser):
    user = unique_user_fn_scoped
    p1 = _create_provider(user, name=f"a-{random_string()}", monthly_token_limit=1000)
    p2 = _create_provider(user, name=f"b-{random_string()}")
    unused = _create_provider(user, name=f"c-{random_string()}")

    day_1 = datetime(2026, 1, 10, 9, 30, tzinfo=UTC)
    day_2 = datetime(2026, 1, 11, 23, 59, tzinfo=UTC)
    _log_usage(user.repos, p1, day_1, prompt_tokens=10, completion_tokens=20)
    _log_usage(user.repos, p1, day_1 + timedelta(hours=1), prompt_tokens=5, success=False)
    _log_usage(user.repos, p1, day_2, prompt_tokens=1, completion_tokens=2)
    _log_usage(user.repos, p2, day_2, prompt_tokens=100, completion_tokens=200)
    _log_usage(user.repos, None, day_1, prompt_tokens=3, completion_tokens=4, name="gone", model="old-model")
    _log_usage(user.repos, None, day_2, prompt_tokens=3, completion_tokens=4, name="gone", model="old-model")
    # Outside the range, and another group's usage
    _log_usage(user.repos, p1, datetime(2026, 1, 9, 23, 59, tzinfo=UTC), prompt_tokens=999)
    _log_usage(user.repos, p1, datetime(2026, 1, 12, tzinfo=UTC), prompt_tokens=999)
    g2_provider = _create_provider(g2_user)
    try:
        _log_usage(g2_user.repos, g2_provider, day_1, prompt_tokens=999)

        response = api_client.get(
            api_routes.groups_ai_providers_usage,
            params={"start": "2026-01-10T00:00:00Z", "end": "2026-01-12T00:00:00Z"},
            headers=user.token,
        )
        assert response.status_code == 200
        summary = response.json()
    finally:
        g2_user.repos.group_ai_providers.delete(g2_provider.id)

    assert summary["start"].startswith("2026-01-10T00:00:00")
    assert summary["end"].startswith("2026-01-12T00:00:00")

    by_provider = summary["byProvider"]
    assert [(row["providerId"], row["providerName"]) for row in by_provider] == [
        (str(p1.id), p1.name),
        (str(p2.id), p2.name),
        (str(unused.id), unused.name),
        (None, "gone"),
    ]
    p1_row, p2_row, unused_row, gone_row = by_provider
    assert p1_row | {"lastUsedAt": None} == {
        "providerId": str(p1.id),
        "providerName": p1.name,
        "model": "gpt-4o",
        "requests": 3,
        "failures": 1,
        "promptTokens": 16,
        "completionTokens": 22,
        "monthlyTokenLimit": 1000,
        "lastUsedAt": None,
    }
    assert datetime.fromisoformat(p1_row["lastUsedAt"]) == day_2
    assert (p2_row["requests"], p2_row["promptTokens"], p2_row["completionTokens"]) == (1, 100, 200)
    assert (unused_row["requests"], unused_row["promptTokens"], unused_row["lastUsedAt"]) == (0, 0, None)
    assert (gone_row["model"], gone_row["requests"], gone_row["promptTokens"]) == ("old-model", 2, 6)

    assert summary["byDay"] == [
        {"date": "2026-01-10", "requests": 3, "promptTokens": 18, "completionTokens": 24},
        {"date": "2026-01-11", "requests": 3, "promptTokens": 104, "completionTokens": 206},
    ]


def test_usage_defaults_to_the_current_month(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    provider = _create_provider(user)
    month_start, month_end = month_range()
    _log_usage(user.repos, provider, datetime.now(UTC), prompt_tokens=10, completion_tokens=5)
    _log_usage(user.repos, provider, month_start - timedelta(seconds=1), prompt_tokens=1000)

    response = api_client.get(api_routes.groups_ai_providers_usage, headers=user.token)
    assert response.status_code == 200
    summary = response.json()

    assert datetime.fromisoformat(summary["start"]) == month_start
    assert datetime.fromisoformat(summary["end"]) == month_end
    assert [(row["requests"], row["promptTokens"]) for row in summary["byProvider"]] == [(1, 10)]


def test_usage_with_only_start_covers_that_month(api_client: TestClient, unique_user_fn_scoped: TestUser):
    response = api_client.get(
        api_routes.groups_ai_providers_usage, params={"start": "2026-02-01"}, headers=unique_user_fn_scoped.token
    )
    assert response.status_code == 200
    summary = response.json()
    assert datetime.fromisoformat(summary["start"]) == datetime(2026, 2, 1, tzinfo=UTC)
    assert datetime.fromisoformat(summary["end"]) == datetime(2026, 3, 1, tzinfo=UTC)

    response = api_client.get(
        api_routes.groups_ai_providers_usage, params={"end": "2026-03-01"}, headers=unique_user_fn_scoped.token
    )
    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["start"]) == datetime(2026, 2, 1, tzinfo=UTC)


def test_usage_rejects_reversed_range(api_client: TestClient, unique_user_fn_scoped: TestUser):
    response = api_client.get(
        api_routes.groups_ai_providers_usage,
        params={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
        headers=unique_user_fn_scoped.token,
    )
    assert response.status_code == 400


# ==========================================
# Model lists


class _FakeModelsAPI:
    """Stands in for an OpenAI-compatible /models endpoint, recording what it was sent"""

    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self.body = body or {
            "object": "list",
            "data": [
                {"id": "model-b", "object": "model", "created": 0, "owned_by": "test"},
                {"id": "model-a", "object": "model", "created": 0, "owned_by": "test"},
            ],
        }
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.body)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def get_client(service: OpenAIService, provider: AIProviderOut) -> AsyncOpenAI:
            return AsyncOpenAI(
                base_url=provider.base_url or None,
                api_key=provider.api_key,
                timeout=provider.timeout,
                default_headers=provider.request_headers or None,
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            )

        monkeypatch.setattr(OpenAIService, "get_client", get_client)


def test_list_models_for_unsaved_provider(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    api = _FakeModelsAPI()
    api.install(monkeypatch)

    # No name or model yet: the dialog loads models before one is picked
    response = api_client.post(
        api_routes.groups_ai_providers_providers_models,
        json={"apiKey": "sk-unsaved", "baseUrl": "https://models.example.test/v1", "requestHeaders": {"X-A": "1"}},
        headers=unique_user.token,
    )
    assert response.status_code == 200
    assert response.json() == [
        {"id": "model-a", "displayName": None, "supportsImages": None},
        {"id": "model-b", "displayName": None, "supportsImages": None},
    ]

    (request,) = api.requests
    assert str(request.url) == "https://models.example.test/v1/models"
    assert request.headers["Authorization"] == "Bearer sk-unsaved"
    assert request.headers["X-A"] == "1"


def test_list_models_accepts_a_full_provider_body(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    _FakeModelsAPI().install(monkeypatch)

    data = {"name": random_string(), "model": "gpt-4o", "apiKey": "sk-unsaved", "baseUrl": "https://x.test/v1"}
    response = api_client.post(api_routes.groups_ai_providers_providers_models, json=data, headers=unique_user.token)
    assert response.status_code == 200
    assert [m["id"] for m in response.json()] == ["model-a", "model-b"]


def test_list_models_requires_an_api_key(api_client: TestClient, unique_user: TestUser):
    response = api_client.post(
        api_routes.groups_ai_providers_providers_models, json={"apiKey": ""}, headers=unique_user.token
    )
    assert response.status_code == 400


def test_list_models_error_never_returns_the_providers_response_body(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Group managers can point base_url anywhere; the error names the failure without relaying the reply"""
    api = _FakeModelsAPI(status_code=401, body={"error": {"message": "top secret internal detail"}})
    api.install(monkeypatch)

    response = api_client.post(
        api_routes.groups_ai_providers_providers_models,
        json={"apiKey": "sk-wrong", "baseUrl": "https://internal-host.test/v1"},
        headers=unique_user.token,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "AuthenticationError (HTTP 401)"
    assert "secret" not in response.text
    assert "internal-host" not in response.text


def test_list_models_for_anthropic_is_not_available_yet(api_client: TestClient, unique_user: TestUser):
    response = api_client.post(
        api_routes.groups_ai_providers_providers_models,
        json={"apiKey": "sk-ant", "protocol": "anthropic"},
        headers=unique_user.token,
    )
    assert response.status_code == 400
    assert "Anthropic" in response.json()["detail"]["message"]


def test_list_models_for_saved_provider_uses_the_saved_key(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    api = _FakeModelsAPI()
    api.install(monkeypatch)
    provider = _create_provider(unique_user, base_url="https://saved.example.test/v1")

    try:
        route = api_routes.groups_ai_providers_providers_provider_id_models(provider.id)
        response = api_client.post(route, headers=unique_user.token)
        assert response.status_code == 200
        assert [m["id"] for m in response.json()] == ["model-a", "model-b"]

        # Unsaved edits: a blank key keeps the saved one
        response = api_client.post(
            route, json={"apiKey": "", "baseUrl": "https://edited.example.test/v1"}, headers=unique_user.token
        )
        assert response.status_code == 200

        response = api_client.post(
            route, json={"apiKey": "sk-new", "baseUrl": "https://edited.example.test/v1"}, headers=unique_user.token
        )
        assert response.status_code == 200

        sent = [(str(r.url), r.headers["Authorization"]) for r in api.requests]
        assert sent == [
            ("https://saved.example.test/v1/models", "Bearer saved-key"),
            ("https://edited.example.test/v1/models", "Bearer saved-key"),
            ("https://edited.example.test/v1/models", "Bearer sk-new"),
        ]
    finally:
        unique_user.repos.group_ai_providers.delete(provider.id)


def test_list_models_for_saved_provider_not_found(
    api_client: TestClient, unique_user: TestUser, g2_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    api = _FakeModelsAPI()
    api.install(monkeypatch)
    foreign = _create_provider(g2_user)

    try:
        for provider_id in [uuid4(), foreign.id]:
            response = api_client.post(
                api_routes.groups_ai_providers_providers_provider_id_models(provider_id), headers=unique_user.token
            )
            assert response.status_code == 404
        assert api.requests == []
    finally:
        g2_user.repos.group_ai_providers.delete(foreign.id)


# ==========================================
# Permissions


def test_routing_endpoints_require_can_manage(api_client: TestClient, user_tuple: list[TestUser]):
    usr, _ = user_tuple
    provider = _create_provider(usr)
    _revoke_can_manage(usr)

    try:
        requests = [
            ("get", api_routes.groups_ai_providers_routes, None),
            ("put", api_routes.groups_ai_providers_routes, {"routes": {"default": [str(provider.id)]}}),
            ("get", api_routes.groups_ai_providers_usage, None),
            ("post", api_routes.groups_ai_providers_providers_models, {"apiKey": "k"}),
            ("post", api_routes.groups_ai_providers_providers_provider_id_models(provider.id), None),
        ]
        for method, route, body in requests:
            kwargs: dict[str, Any] = {"headers": usr.token}
            if body is not None:
                kwargs["content"] = json.dumps(body)
                kwargs["headers"] = {**usr.token, "Content-Type": "application/json"}
            response = api_client.request(method, route, **kwargs)
            assert response.status_code == 403, f"{method} {route}"
    finally:
        usr.repos.group_ai_providers.delete(provider.id)
