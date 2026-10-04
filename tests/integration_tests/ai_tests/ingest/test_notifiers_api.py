"""
A notifier's "recipe cards ready" toggle and its test notification, `/api/ai/notifiers/{id}/events`
(docs/ai/PHASE2.md §8, §9): the same permission checks as upstream's notifier routes, the household's notifiers only.
"""

import json
from typing import Any
from uuid import UUID, uuid4

import apprise
import pytest
from fastapi.testclient import TestClient

from mealie.schema.household.group_events import GroupEventNotifierSave
from mealie.services.ai.ingest import events
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.publisher import ApprisePublisher
from tests.integration_tests.ai_tests.ingest.test_jobs_api import household_member
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

NOTIFIERS = "/api/ai/notifiers"


def events_url(notifier_id: UUID | str) -> str:
    return f"{NOTIFIERS}/{notifier_id}/events"


def create_notifier(user: TestUser, url: str = "jsons://homeassistant.local:8123/api/webhook/mealie_cards") -> UUID:
    saved = user.repos.group_event_notifier.create(
        GroupEventNotifierSave(
            name=random_string(), apprise_url=url, group_id=user.group_id, household_id=user.household_id
        )
    )
    return saved.id


@pytest.fixture()
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, list[str]]]:
    sent: list[tuple[Any, list[str]]] = []

    def publish(self: ApprisePublisher, event: Any, notification_urls: list[str]) -> None:
        sent.append((event, list(notification_urls)))

    def dispatch(self: EventBusService, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("AI events never go through EventBusService.dispatch")

    monkeypatch.setattr(ApprisePublisher, "publish", publish)
    monkeypatch.setattr(EventBusService, "dispatch", dispatch)
    return sent


def test_the_toggle_is_off_until_switched_on(api_client: TestClient, unique_user: TestUser):
    notifier_id = create_notifier(unique_user)

    response = api_client.get(events_url(notifier_id), headers=unique_user.token)
    assert response.status_code == 200
    assert response.json() == {"recipeIngestionReady": False}

    response = api_client.put(events_url(notifier_id), json={"recipeIngestionReady": True}, headers=unique_user.token)
    assert response.status_code == 200
    assert response.json() == {"recipeIngestionReady": True}
    assert api_client.get(events_url(notifier_id), headers=unique_user.token).json() == {"recipeIngestionReady": True}

    api_client.put(events_url(notifier_id), json={"recipeIngestionReady": False}, headers=unique_user.token)
    assert api_client.get(events_url(notifier_id), headers=unique_user.token).json() == {"recipeIngestionReady": False}

    # upstream's own notifier options are untouched
    upstream = api_client.get(
        api_routes.households_events_notifications_item_id(notifier_id), headers=unique_user.token
    )
    assert upstream.status_code == 200
    options = upstream.json()["options"]
    assert not any(value for key, value in options.items() if key != "id")


def test_unknown_fields_are_refused(api_client: TestClient, unique_user: TestUser):
    notifier_id = create_notifier(unique_user)
    response = api_client.put(
        events_url(notifier_id), json={"recipeIngestionReady": True, "recipeCreated": True}, headers=unique_user.token
    )
    assert response.status_code == 422


def test_any_household_member_can_use_them_as_upstream_allows(
    api_client: TestClient, admin_token: dict, unique_user: TestUser, published: list
):
    """Upstream's notifier routes check nothing beyond being a member of the household, and neither do these"""
    member = household_member(api_client, admin_token, unique_user)
    notifier_id = create_notifier(unique_user)

    response = api_client.put(events_url(notifier_id), json={"recipeIngestionReady": True}, headers=member.token)
    assert response.status_code == 200
    assert api_client.get(events_url(notifier_id), headers=member.token).json() == {"recipeIngestionReady": True}
    assert api_client.post(f"{events_url(notifier_id)}/test", headers=member.token).status_code == 204
    assert len(published) == 1


def test_another_households_notifier_is_not_found(
    api_client: TestClient, unique_user: TestUser, h2_user: TestUser, g2_user: TestUser, published: list
):
    notifier_id = create_notifier(unique_user)

    for other in (h2_user, g2_user):
        for response in (
            api_client.get(events_url(notifier_id), headers=other.token),
            api_client.put(events_url(notifier_id), json={"recipeIngestionReady": True}, headers=other.token),
            api_client.post(f"{events_url(notifier_id)}/test", headers=other.token),
        ):
            assert response.status_code == 404
            assert response.json()["detail"] == {"code": "not_found"}

    assert api_client.get(events_url(uuid4()), headers=unique_user.token).status_code == 404
    assert api_client.get(events_url(notifier_id), headers=unique_user.token).json() == {"recipeIngestionReady": False}
    assert published == []


def test_login_is_required(api_client: TestClient, unique_user: TestUser):
    notifier_id = create_notifier(unique_user)
    api_client.cookies.clear()  # a login in an earlier test leaves its session cookie
    assert api_client.get(events_url(notifier_id)).status_code == 401
    assert api_client.put(events_url(notifier_id), json={"recipeIngestionReady": True}).status_code == 401
    assert api_client.post(f"{events_url(notifier_id)}/test").status_code == 401


def test_the_test_notification(api_client: TestClient, unique_user_fn_scoped: TestUser, published: list):
    """The ready event through this one notifier, switched on or not, with the household's counts and no batch"""
    user = unique_user_fn_scoped
    url = "jsons://homeassistant.local:8123/api/webhook/mealie_cards"
    notifier_id = create_notifier(user, url)
    create_notifier(user, "json://another.local/hook")

    response = api_client.post(f"{events_url(notifier_id)}/test", headers=user.token)
    assert response.status_code == 204

    [(event, urls)] = published
    assert isinstance(event, events.AIEvent)
    assert event.event_type is events.AIEventTypes.recipe_ingestion_ready
    assert event.integration_id == events.TEST_INTEGRATION_ID
    assert event.message.title == "Recipe cards ready (test)"
    assert event.message.body == "This is a test notification from Mealie. Recipe cards waiting for review: 0."

    [sent] = urls
    assert sent.startswith(url + "?")
    # what Home Assistant gets, after Apprise has decoded the URL
    plugin = apprise.Apprise.instantiate(sent)
    assert plugin is not None
    params = {f":{key}": value for key, value in plugin.payload_extras.items()}
    assert params[":event_type"] == "recipe_ingestion_ready"
    assert params[":integration_id"] == "test_event"
    slug = api_client.get(api_routes.groups_self, headers=user.token).json()["slug"]
    assert json.loads(params[":document_data"]) == {
        "documentType": "generic",
        "operation": "info",
        "batchId": None,
        "jobIds": [],
        "readyCount": 0,
        "needsAttentionCount": 0,
        "failedCount": 0,
        "reviewUrl": f"http://localhost:8080/g/{slug}/recipes/cards",
    }
