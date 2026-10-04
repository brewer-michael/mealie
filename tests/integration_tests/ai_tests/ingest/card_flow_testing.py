"""
Helpers for the recipe card seam tests (`test_card_flow_e2e.py`, `test_two_dispatchers.py`): cards uploaded through
the real API, read by real dispatchers whose provider is a fake answering the card schemas, and notifications sent
through the real listeners to a mocked Apprise. Not a conftest: nothing here applies to the other tests in this folder.
"""

import asyncio
import inspect
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import apprise
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import ExpiredLease, IngestQueue
from mealie.schema.household.group_events import GroupEventNotifierOptions, GroupEventNotifierSave
from mealie.services.ai.ingest import commit, inbox, retention
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.event_types import EventTypes
from mealie.services.event_bus_service.publisher import ApprisePublisher
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import card_image, configure, create_provider
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

INGEST = "/api/ai/ingest"
BATCHES = f"{INGEST}/batches"
JOBS = f"{INGEST}/jobs"
NOTIFIERS = "/api/ai/notifiers"

READY_EVENT = "recipe_ingestion_ready"
RECIPE_CREATED_EVENT = EventTypes.recipe_created.name

Job = RecipeIngestionJob
Batch = RecipeIngestionBatch


# ==================================================================================================================
# The group, its notifiers and its cards


def make_card_reader(user: TestUser) -> None:
    """Gives the user's group an image provider and a default provider, so it can read cards"""
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))


@dataclass(frozen=True)
class Notifier:
    id: UUID
    host: str
    """Unique to this notifier, so its notifications can be told apart from other tests' batches'"""


def make_notifier(
    api_client: TestClient, user: TestUser, *, cards_ready: bool, recipe_created: bool = False
) -> Notifier:
    """
    A `json://` notifier of the user's household (so Apprise gets the event's data in the URL), with upstream's
    `recipe_created` option as given and the fork's "Recipe cards ready to review" toggle switched through its route
    """
    host = f"cards-{random_string(12)}.local"
    saved = user.repos.group_event_notifier.create(
        GroupEventNotifierSave(
            name=random_string(),
            apprise_url=f"json://{host}/api/webhook/mealie_cards",
            group_id=user.group_id,
            household_id=user.household_id,
            options=GroupEventNotifierOptions(recipe_created=recipe_created),
        )
    )
    response = api_client.put(
        f"{NOTIFIERS}/{saved.id}/events", json={"recipeIngestionReady": cards_ready}, headers=user.token
    )
    assert response.status_code == 200, response.text
    return Notifier(saved.id, host)


def photo(label: str) -> bytes:
    """A card photo of its own (never a duplicate of another)"""
    return card_image(lines=(label, random_string(8)))


def card_files(*photos: bytes) -> list[tuple[str, tuple[str, bytes, str]]]:
    """A card's photos as the PWA sends them: front first, every one an `image.jpg` (as iOS names them)"""
    return [("files", ("image.jpg", data, "image/jpeg")) for data in photos]


def form(**fields: Any) -> dict[str, str]:
    return {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in fields.items()}


def upload(api_client: TestClient, user: TestUser, *photos: bytes, **fields: Any) -> dict[str, Any]:
    """`POST /api/ai/ingest` as a multipart form with the Bearer header; the 202's body"""
    assert user.token["Authorization"].startswith("Bearer ")
    response = api_client.post(INGEST, files=card_files(*photos), data=form(**fields), headers=user.token)
    assert response.status_code == 202, response.text
    return response.json()


def start_batch(api_client: TestClient, user: TestUser) -> str:
    """The PWA's first card creates the batch"""
    response = api_client.post(BATCHES, json={}, headers=user.token)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def seal(api_client: TestClient, user: TestUser, batch_id: str) -> dict[str, Any]:
    """Done (the PWA sends it once every card of the batch has uploaded)"""
    response = api_client.post(f"{BATCHES}/{batch_id}/seal", headers=user.token)
    assert response.status_code == 200, response.text
    return response.json()


class InFlightUpload:
    """
    A card upload the server has started on (auth and the checks before the body have passed, and it is reading the
    body) but whose body hasn't arrived yet: a slow phone connection. `finish()` sends the rest and returns the 202.
    """

    def __init__(self, api_client: TestClient, user: TestUser, *photos: bytes, **fields: Any) -> None:
        request = api_client.build_request("POST", INGEST, files=card_files(*photos), data=form(**fields))
        body, content_type = request.read(), request.headers["Content-Type"]
        self._reading, self._release = threading.Event(), threading.Event()
        self._response: Any = None
        self._error: BaseException | None = None

        def chunks() -> Iterable[bytes]:
            self._reading.set()  # the server asked for the body
            assert self._release.wait(60), "the in-flight upload was never released"
            yield body

        def post() -> None:
            try:
                self._response = api_client.post(
                    INGEST, content=chunks(), headers={**user.token, "Content-Type": content_type}
                )
            except BaseException as e:
                self._error = e

        self._thread = threading.Thread(target=post, name="phone-upload", daemon=True)
        self._thread.start()
        assert self._reading.wait(30), "the server never started reading the upload"

    def finish(self) -> dict[str, Any]:
        self._release.set()
        self._thread.join(60)
        assert not self._thread.is_alive(), "the upload never finished"
        if self._error is not None:
            raise self._error
        assert self._response.status_code == 202, self._response.text
        return self._response.json()


# ==================================================================================================================
# Rows


def job_status(job_id: UUID | str) -> str:
    with session_context() as session:
        return session.execute(sa.select(Job.status).where(Job.id == UUID(str(job_id)))).scalar_one()


def job_columns(job_id: UUID | str) -> dict[str, Any]:
    with session_context() as session:
        row = session.execute(sa.select(*Job.__table__.columns).where(Job.id == UUID(str(job_id)))).mappings().one()
        return dict(row)


def batch_columns(batch_id: UUID | str) -> dict[str, Any]:
    with session_context() as session:
        row = (
            session.execute(sa.select(*Batch.__table__.columns).where(Batch.id == UUID(str(batch_id)))).mappings().one()
        )
        return dict(row)


def all_settled(job_ids: Iterable[UUID | str]) -> Callable[[], bool]:
    """A condition: none of the jobs is `processing` any more"""
    ids = [UUID(str(job_id)) for job_id in job_ids]

    def settled() -> bool:
        with session_context() as session:
            stmt = sa.select(sa.func.count()).select_from(Job).where(Job.id.in_(ids), Job.status == "processing")
            return session.execute(stmt).scalar_one() == 0

    return settled


# ==================================================================================================================
# The dispatcher


def only_households(monkeypatch: pytest.MonkeyPatch, *household_ids: UUID | str) -> None:
    """
    The dispatcher claims and sweeps across households, and the shared test database holds other tests' jobs: keep
    it to these households' jobs. The claims themselves (the conditional `UPDATE`s) are untouched.
    """
    households = {UUID(str(household_id)) for household_id in household_ids}
    queued_ids = IngestQueue.queued_ids
    expired = IngestQueue.expired

    def ours(ids: list[UUID]) -> set[UUID]:
        if not ids:
            return set()
        with session_context() as session:
            stmt = sa.select(Job.id).where(Job.id.in_(ids), Job.household_id.in_(households))
            return set(session.execute(stmt).scalars())

    def own_queued_ids(self: IngestQueue, now: Any, limit: int, *, max_priority: int | None = None) -> list[UUID]:
        if limit <= 0:
            return []
        ids = queued_ids(self, now, 100_000, max_priority=max_priority)
        mine = ours(ids)
        return [job_id for job_id in ids if job_id in mine][:limit]

    def own_expired(self: IngestQueue, now: Any) -> list[ExpiredLease]:
        leases = expired(self, now)
        mine = ours([lease.job_id for lease in leases])
        return [lease for lease in leases if lease.job_id in mine]

    monkeypatch.setattr(IngestQueue, "queued_ids", own_queued_ids)
    monkeypatch.setattr(IngestQueue, "expired", own_expired)


def quiet_other_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The dispatcher phases these flows don't use, and that would act on other tests' rows: resuming stale commits, the
    inbox and the purge. Claims, heartbeats, sweeps and housekeeping (sealing and notifying) stay real.
    """
    monkeypatch.setattr(commit, "resume_stale_commits", lambda now: 0)
    monkeypatch.setattr(inbox, "scan_once", lambda: 0)
    monkeypatch.setattr(retention, "purge_once", lambda now: None)


async def run_until(dispatcher: IngestDispatcher, condition: Callable[[], bool], timeout: float = 60) -> None:
    """`run_once()` until `condition()` holds (the app's loop would tick on its own), then waits for the tasks to end"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "timed out waiting for the dispatcher"
        await dispatcher.run_once()
        await asyncio.sleep(0.02)
    assert await dispatcher.drain(30), "the dispatcher's tasks didn't finish"


def dispatch_until(condition: Callable[[], bool], *, instance: str = "e2e", timeout: float = 60) -> None:
    """A dispatcher of the test's own, on an event loop of its own, run until `condition()` holds and then stopped"""

    async def main() -> None:
        dispatcher = IngestDispatcher(concurrency=2, instance=instance)
        try:
            await run_until(dispatcher, condition, timeout)
        finally:
            await dispatcher.stop()

    asyncio.run(main())


def dispatch_once(*, instance: str = "e2e") -> None:
    """One full pass of a fresh dispatcher (every phase due: sweep, claims, housekeeping), its tasks waited for"""

    async def main() -> None:
        dispatcher = IngestDispatcher(concurrency=2, instance=instance)
        try:
            await dispatcher.run_once()
            assert await dispatcher.drain(30), "the dispatcher's tasks didn't finish"
        finally:
            await dispatcher.stop()

    asyncio.run(main())


# ==================================================================================================================
# What was sent


@dataclass(frozen=True)
class Notified:
    """One `Apprise.notify` call: the notification, and what its notifiers would have been sent"""

    title: str
    body: str
    urls: tuple[str, ...]
    """The URLs `ApprisePublisher` was given: the listener's, with the event's data in their `:key` parameters"""
    received: tuple[dict[str, str], ...]
    """
    The custom fields each JSON notifier would POST next to the title and message (`trigger.json` in Home
    Assistant): what Apprise read back out of the URL's `:key` parameters
    """
    thread: str

    def event_types(self) -> set[str]:
        return {params(url).get(":event_type", "") for url in self.urls}

    def document(self) -> dict[str, Any]:
        """The event's `document_data` as the listener put it in the URL"""
        [document] = {params(url)[":document_data"] for url in self.urls}
        return json.loads(document)

    def received_document(self) -> dict[str, Any]:
        """`document_data` as the notifier receives it, parsed as Home Assistant's `from_json` would"""
        [document] = {fields["document_data"] for fields in self.received}
        return json.loads(document)


def params(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


@pytest.fixture()
def apprise_sent(monkeypatch: pytest.MonkeyPatch) -> list[Notified]:
    """
    Apprise mocked at its edge: the real listeners pick the notifiers, and `ApprisePublisher` adds their URLs to a real
    `Apprise` object, which parses them; only `notify`, which would send, is replaced by a recorder
    """
    sent: list[Notified] = []
    publishing = threading.local()
    publish = ApprisePublisher.publish

    def recording_publish(self: ApprisePublisher, event: Any, notification_urls: list[str]) -> None:
        publishing.urls = tuple(notification_urls)
        try:
            publish(self, event, notification_urls)
        finally:
            publishing.urls = ()

    def notify(self: apprise.Apprise, body: Any, title: Any = "", *args: Any, **kwargs: Any) -> bool:
        received = tuple(dict(getattr(server, "payload_extras", {})) for server in self)
        urls = getattr(publishing, "urls", ())
        sent.append(Notified(str(title), str(body), urls, received, threading.current_thread().name))
        return True

    monkeypatch.setattr(ApprisePublisher, "publish", recording_publish)
    monkeypatch.setattr(apprise.Apprise, "notify", notify)
    return sent


def sent_to(sent: list[Notified], notifier: Notifier, event_type: str) -> list[Notified]:
    """The notifications of one event type that went to one notifier"""
    return [n for n in sent if any(notifier.host in url for url in n.urls) and event_type in n.event_types()]


@dataclass(frozen=True)
class Dispatched:
    event_type: EventTypes
    household_id: Any
    document_data: Any


@pytest.fixture()
def bus_events(monkeypatch: pytest.MonkeyPatch) -> list[Dispatched]:
    """Every event upstream's `EventBusService.dispatch` was given, which then goes on as usual"""
    events: list[Dispatched] = []
    dispatch = EventBusService.dispatch

    def recording_dispatch(self: EventBusService, *args: Any, **kwargs: Any) -> None:
        bound = inspect.signature(dispatch).bind(self, *args, **kwargs).arguments
        events.append(Dispatched(bound["event_type"], bound["household_id"], bound["document_data"]))
        dispatch(self, *args, **kwargs)

    monkeypatch.setattr(EventBusService, "dispatch", recording_dispatch)
    return events
