"""
Fork: upstream's Apprise listener adds each event's fields to custom (json, form, xml) URLs. They must reach Apprise
intact, so Home Assistant can parse `document_data` with `from_json`, and the notifier's own query must arrive as the
user wrote it (docs/ai/PHASE2.md §8).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import apprise
import pytest
from fastapi.encoders import jsonable_encoder

from mealie.services.event_bus_service.event_bus_listeners import AppriseEventListener
from mealie.services.event_bus_service.event_types import (
    Event,
    EventBusMessage,
    EventOperation,
    EventRecipeData,
    EventTypes,
)


def recipe_created(slug: str = "banana-mug-cake", integration_id: str = "recipe card") -> Event:
    return Event(
        message=EventBusMessage(title="New Recipe", body="Banana Mug Cake & more"),
        event_type=EventTypes.recipe_created,
        integration_id=integration_id,
        document_data=EventRecipeData(operation=EventOperation.create, recipe_slug=slug),
    )


def extras_of(url: str) -> dict[str, str]:
    notifier = apprise.Apprise.instantiate(url)
    assert notifier is not None
    return notifier.payload_extras


def test_recipe_created_document_data_reaches_apprise_as_json():
    event = recipe_created()
    [url] = AppriseEventListener.update_urls_with_event_data(["json://ha.local:8123/api/webhook/abc"], event)

    extras = extras_of(url)
    assert json.loads(extras["document_data"]) == jsonable_encoder(event.document_data)
    assert extras["event_type"] == "recipe_created"
    assert extras["integration_id"] == "recipe card"
    assert extras["event_id"] == str(event.event_id)
    assert event.timestamp is not None
    assert extras["timestamp"] == event.timestamp.isoformat()


@pytest.mark.parametrize("scheme", ["json", "jsons", "form", "xml"])
def test_event_fields_with_reserved_characters_round_trip(scheme: str):
    """Apprise decodes these values twice, so a `%20` or `%AB` in the event's data must survive both decodes"""
    slug = "a+b&c=d;e é/f %20 100%AB #x"
    event = recipe_created(slug=slug, integration_id="x+y z%2B")
    [url] = AppriseEventListener.update_urls_with_event_data([f"{scheme}://ha.local/api/webhook/abc"], event)

    extras = extras_of(url)
    assert json.loads(extras["document_data"])["recipeSlug"] == slug
    assert extras["integration_id"] == "x+y z%2B"


def test_notifiers_own_query_is_left_as_written():
    own = "json://ha.local:8123/api/webhook/abc?:token=a+b%2Bc&+X-Key=d+e&:room=living%20room&-Remove=&flag"
    [url] = AppriseEventListener.update_urls_with_event_data([own], recipe_created())

    assert url.startswith(own + "&")
    notifier = apprise.Apprise.instantiate(url)
    assert notifier is not None
    assert notifier.payload_extras["token"] == "a+b+c"
    assert notifier.payload_extras["room"] == "living room"
    assert notifier.headers["X-Key"] == "d+e"


def test_event_fields_replace_ones_the_notifier_wrote():
    own = "form://example.com/hook?:event_type=mine&%3Aevent_id=mine&:keep=yes"
    event = recipe_created()
    [url] = AppriseEventListener.update_urls_with_event_data([own], event)

    assert url.count("event_type=") == 1
    assert url.count("event_id=") == 1
    extras = extras_of(url)
    assert extras["event_type"] == "recipe_created"
    assert extras["event_id"] == str(event.event_id)
    assert extras["keep"] == "yes"


def test_other_urls_are_unchanged():
    urls = ["mailto://user:pass@example.com?to=a+b@example.com", "ntfy://topic?priority=high", "pbul://abc/def"]
    assert AppriseEventListener.update_urls_with_event_data(urls, recipe_created()) == urls


def test_a_notification_posts_the_events_fields_exactly():
    """What a json:// notifier (Home Assistant's webhook) receives, sent by Apprise to a local server"""
    received: list[dict[str, Any]] = []

    class Webhook(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Webhook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        event = recipe_created(slug="banana-mug-cake 100%AB", integration_id="recipe card")
        own = f"json://127.0.0.1:{server.server_address[1]}/api/webhook/abc?:token=a+b%2Bc"
        [url] = AppriseEventListener.update_urls_with_event_data([own], event)
        notifier = apprise.Apprise()
        assert notifier.add(url)
        assert notifier.notify(title=event.message.title, body=event.message.body)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    [payload] = received
    assert json.loads(payload["document_data"]) == jsonable_encoder(event.document_data)
    assert payload["event_type"] == "recipe_created"
    assert payload["integration_id"] == "recipe card"
    assert payload["token"] == "a+b+c"
