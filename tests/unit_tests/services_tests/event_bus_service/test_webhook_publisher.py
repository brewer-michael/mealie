"""
Fork: safehttp refuses a webhook's redirect from https to plain http (mealie/pkgs/safehttp/redirects.py). That webhook
isn't sent, and the household's other webhooks still are.
"""

import httpx
import pytest

from mealie.pkgs import safehttp
from mealie.pkgs.safehttp import transport as safehttp_transport
from mealie.services.event_bus_service.event_types import (
    Event,
    EventBusMessage,
    EventDocumentType,
    EventOperation,
    EventTypes,
    EventWebhookData,
)
from mealie.services.event_bus_service.publisher import WebhookPublisher


def _event() -> Event:
    return Event(
        message=EventBusMessage(title="Test", body="body"),
        event_type=EventTypes.webhook_task,
        integration_id="test",
        document_data=EventWebhookData(
            document_type=EventDocumentType.mealplan,
            operation=EventOperation.info,
            webhook_start_dt="2026-10-04T00:00:00Z",
            webhook_end_dt="2026-10-04T01:00:00Z",
            webhook_body=[],
        ),
    )


class Hooks:
    """Answers the first webhook with a redirect to plain http, every other one with 200"""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url == "https://hooks.example/downgraded":
            return httpx.Response(307, headers={"Location": "http://hooks.example/downgraded"})
        return httpx.Response(200)


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> Hooks:
    hooks = Hooks()
    monkeypatch.setattr(safehttp_transport, "SafeTransport", lambda **kwargs: httpx.MockTransport(hooks.handle))
    return hooks


def test_a_refused_redirect_doesnt_stop_the_other_webhooks(hooks: Hooks):
    urls = ["https://hooks.example/downgraded", "https://hooks.example/second", "https://hooks.example/third"]

    WebhookPublisher().publish(_event(), urls)

    # the downgrade was never followed, and the other two were sent
    assert hooks.requested == urls


def test_hard_fail_still_raises_the_refusal(hooks: Hooks):
    with pytest.raises(safehttp.UnsafeRedirectError):
        WebhookPublisher(hard_fail=True).publish(_event(), ["https://hooks.example/downgraded"])
