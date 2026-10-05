"""
"Recipe cards ready" notifications (docs/ai/PHASE2.md §8, §18 Events): only notifiers that opted in, the Apprise URL
params, one notification when two cards finish together, auto-seal, failed-only batches, the 24-hour cutoff, counts
and a link only, and never through `EventBusService.dispatch`. Delivery is checked per notifier and retried after the
claim's lease: at least once per notifier, given up on after `NOTIFY_ATTEMPTS`.
"""

import calendar
import json
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mealie.db import db_setup
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestQueue, IngestRepos, LimitWait, utcnow
from mealie.schema.household.group_events import GroupEventNotifierSave
from mealie.schema.recipe_ingest import IngestErrorCode, IngestRejectReason, IngestSource, IngestStatus
from mealie.services.ai.ingest import events, limits
from mealie.services.ai.ingest.i18n import translator_for
from mealie.services.ai.ingest.runner import retries
from mealie.services.ai.ingest.runner.finalize import next_limit_reset
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.publisher import ApprisePublisher
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

CARD_TITLE = "Grandma Jo's Secret Fudge"
"""Card text that must never leave the server"""


@dataclass
class Published:
    event: events.AIEvent
    urls: list[str]
    """The URLs that took it"""
    thread: threading.Thread

    @property
    def data(self) -> events.EventIngestionReadyData:
        assert isinstance(self.event.document_data, events.EventIngestionReadyData)
        return self.event.document_data


class Outbox(list[Published]):
    """
    What Apprise was asked to deliver (nothing is sent): one entry per event, with the URLs that took it. A URL that
    holds one of the strings in `down` doesn't take it; `tries` has every URL tried, delivered or not.
    """

    def __init__(self) -> None:
        super().__init__()
        self.down: set[str] = set()
        self.tries: list[tuple[events.AIEvent, str]] = []
        self._lock = threading.Lock()

    def deliver(self, event: events.AIEvent, url: str) -> bool:
        with self._lock:
            self.tries.append((event, url))
            if any(part in url for part in self.down):
                return False
            for published in self:
                if published.event.event_id == event.event_id:
                    published.urls.append(url)
                    break
            else:
                self.append(Published(event, [url], threading.current_thread()))
            return True

    def delivered_to(self, batch_id: UUID, host: str) -> int:
        """How many times the batch's notification reached the URLs with `host`"""
        return sum(1 for p in for_batch(self, batch_id) for url in p.urls if host in url)

    def tried(self, batch_id: UUID, host: str) -> int:
        """How many times the batch's notification was sent to the URLs with `host`, delivered or not"""
        return sum(
            1
            for event, url in self.tries
            if isinstance(event.document_data, events.EventIngestionReadyData)
            and event.document_data.batch_id == batch_id
            and host in url
        )


@pytest.fixture()
def published(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    """What Apprise was asked to deliver, one notifier at a time; `EventBusService.dispatch` must never be used"""
    outbox = Outbox()

    def dispatch(self: EventBusService, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("AI events never go through EventBusService.dispatch")

    monkeypatch.setattr(events, "deliver", outbox.deliver)
    monkeypatch.setattr(EventBusService, "dispatch", dispatch)
    return outbox


def for_batch(sent: list[Published], batch_id: UUID) -> list[Published]:
    """Only this batch's notifications (housekeeping also finds other tests' due batches)"""
    return [
        p
        for p in sent
        if isinstance(p.event.document_data, events.EventIngestionReadyData) and p.data.batch_id == batch_id
    ]


def notifier(user: TestUser, url: str, *, ready: bool = True, enabled: bool = True, name: str | None = None) -> UUID:
    saved = user.repos.group_event_notifier.create(
        GroupEventNotifierSave(
            name=name or random_string(),
            apprise_url=url,
            enabled=enabled,
            group_id=user.group_id,
            household_id=user.household_id,
        )
    )
    with session_context() as session:
        repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
        assert repos.notifier_options.set(saved.id, recipe_ingestion_ready=ready) is not None
    return saved.id


def make_batch(
    user: TestUser,
    *cards: str,
    sealed: bool = True,
    age: timedelta = timedelta(0),
    active: timedelta = timedelta(0),
    idle: timedelta | None = None,
    source: IngestSource = IngestSource.app,
    locale: str | None = "en-US",
) -> UUID:
    """
    A batch created `age` ago with one job per card: a status (`ready`, `failed`, `processing`, `committed`),
    `ready!` for a ready card with something to check, or `waiting` for a card that failed `limit_reached` and waits
    for the next reset. Every card carries `CARD_TITLE`. Its cards were last written `active` ago, when it was also
    sealed.
    """
    now = utcnow()
    waits = {
        "status": IngestStatus.failed.value,
        "error_code": IngestErrorCode.limit_reached.value,
        "auto_retry_at": next_limit_reset(),
    }
    with session_context() as session:
        repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
        batch_id = repos.batches.create(source=source, created_by=user.user_id, locale=locale, now=now - age)
        for position, card in enumerate(cards):
            needs_attention = card.endswith("!")
            status = {"status": IngestStatus(card.rstrip("!")).value} if card != "waiting" else waits
            repos.jobs.create(
                {
                    "batch_id": batch_id,
                    "position": position,
                    "source": source.value,
                    **status,
                    "title": CARD_TITLE,
                    "source_sha256": uuid4().hex * 2,
                    "warning_count": 1 if needs_attention else 0,
                    "created_at": now - age,
                    "update_at": now - active,
                }
            )

        values: dict[str, Any] = {}
        if sealed:
            values["sealed_at"] = now - active
        if idle is not None:
            values["last_upload_at"] = now - idle
        if values:
            session.execute(sa.update(RecipeIngestionBatch).where(RecipeIngestionBatch.id == batch_id).values(**values))
            session.commit()
    return batch_id


def batch_row(batch_id: UUID) -> dict[str, Any]:
    with session_context() as session:
        row = session.execute(
            sa.select(RecipeIngestionBatch.sealed_at, RecipeIngestionBatch.notified_at).where(
                RecipeIngestionBatch.id == batch_id
            )
        ).one()
        return dict(row._mapping)


def notify_state(batch_id: UUID) -> dict[str, Any]:
    """The batch's notification columns"""
    with session_context() as session:
        row = session.execute(
            sa.select(
                RecipeIngestionBatch.notified_at,
                RecipeIngestionBatch.notify_claimed_at,
                RecipeIngestionBatch.notify_attempts,
                RecipeIngestionBatch.notify_delivered,
            ).where(RecipeIngestionBatch.id == batch_id)
        ).one()
        return dict(row._mapping)


def set_status(batch_id: UUID, status: IngestStatus) -> None:
    with session_context() as session:
        session.execute(
            sa.update(RecipeIngestionJob).where(RecipeIngestionJob.batch_id == batch_id).values(status=status.value)
        )
        session.commit()


def group_slug(api_client: TestClient, user: TestUser) -> str:
    return api_client.get(api_routes.groups_self, headers=user.token).json()["slug"]


def custom_params(url: str) -> dict[str, str]:
    """The event's `:field` values as Apprise's json notifier reads them back, and so as Home Assistant gets them"""
    import apprise

    plugin = apprise.Apprise.instantiate(url)
    assert plugin is not None
    return {f":{key}": value for key, value in plugin.payload_extras.items()}


# ==================================================================================================================
# Who gets it, and what it says


def test_only_the_households_enabled_notifiers_that_opted_in(
    api_client: TestClient, unique_user: TestUser, h2_user: TestUser, published: list[Published]
):
    home_assistant = "jsons://homeassistant.local:8123/api/webhook/mealie_cards"
    notifier(unique_user, home_assistant)
    notifier(unique_user, "mailto://cards@example.com", ready=False)
    notifier(unique_user, "json://disabled.local/hook", enabled=False)
    notifier(unique_user, "pover://user@token")  # opted in, and takes no custom values
    notifier(h2_user, "json://other-household.local/hook")  # another household of the same group

    batch_id = make_batch(unique_user, "ready", "ready!", "failed", "committed", "ready")
    assert events.maybe_notify_batch(batch_id) is True

    [sent] = published
    assert sent.thread is threading.current_thread()  # Apprise runs in the caller's thread
    assert len(sent.urls) == 2
    pushover = next(url for url in sent.urls if url.startswith("pover://"))
    assert pushover == "pover://user@token"  # untouched: only json/form/xml URLs carry the event's data

    ha = next(url for url in sent.urls if url.startswith("jsons://"))
    assert ha.startswith(home_assistant + "?")
    params = custom_params(ha)
    assert params[":event_type"] == "recipe_ingestion_ready"
    assert params[":integration_id"] == events.INTERNAL_INTEGRATION_ID
    assert params[":event_id"] == str(sent.event.event_id)
    document = json.loads(params[":document_data"])
    assert document == {
        "documentType": "generic",
        "operation": "info",
        "batchId": str(batch_id),
        "jobIds": [str(job_id) for job_id in sent.data.job_ids],
        "readyCount": 3,
        "needsAttentionCount": 1,
        "failedCount": 1,
        "waitingCount": 0,
        "reviewUrl": f"http://localhost:8080/g/{group_slug(api_client, unique_user)}/recipes/cards/review"
        f"?batch={batch_id}",
    }
    assert len(document["jobIds"]) == 5

    assert sent.event.message.title == "Recipe cards ready"
    assert sent.event.message.body == "3 cards are ready to review (1 needs a look, 1 failed)."
    # counts and a link only: card text never leaves the server
    assert CARD_TITLE not in sent.event.model_dump_json()
    assert all(CARD_TITLE not in url and "Fudge" not in url for url in sent.urls)


@pytest.mark.parametrize(
    ("cards", "title", "body"),
    [
        (["ready"], "Recipe cards ready", "1 card is ready to review."),
        (["ready", "ready", "ready!"], "Recipe cards ready", "3 cards are ready to review (1 needs a look)."),
        (
            ["ready!", "ready!", "failed", "failed"],
            "Recipe cards ready",
            "2 cards are ready to review (2 need a look, 2 failed).",
        ),
        # a batch whose every card failed still says so, and its title doesn't say they're ready
        (["failed", "failed"], "Recipe cards not read", "No cards are ready to review (2 failed)."),
        (["failed"], "Recipe cards not read", "No cards are ready to review (1 failed)."),
        # cards waiting for the monthly limit wait: they haven't failed, and are read again on their own
        (
            ["waiting", "waiting"],
            "Recipe cards waiting",
            "2 cards are waiting for the monthly limit. They'll be read when it resets on {reset}, or sooner if "
            "it's raised.",
        ),
        (
            ["ready", "failed", "waiting"],
            "Recipe cards ready",
            "1 card is ready to review (1 failed). 1 card is waiting for the monthly limit. It'll be read when it "
            "resets on {reset}, or sooner if it's raised.",
        ),
        (
            ["failed", "waiting", "committed"],
            "Recipe cards not read",
            "No cards are ready to review (1 failed). 1 card is waiting for the monthly limit. It'll be read when it "
            "resets on {reset}, or sooner if it's raised.",
        ),
    ],
)
def test_the_message_counts(
    unique_user_fn_scoped: TestUser, published: list[Published], cards: list[str], title: str, body: str
):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    batch_id = make_batch(unique_user_fn_scoped, *cards)

    assert events.maybe_notify_batch(batch_id) is True
    [sent] = published
    reset = next_limit_reset()
    body = body.format(reset=f"{calendar.month_abbr[reset.month]} {reset.day}")
    assert (sent.event.message.title, sent.event.message.body) == (title, body)
    assert sent.data.failed_count == cards.count("failed")
    assert sent.data.waiting_count == cards.count("waiting")
    # the same event type either way, so a Home Assistant automation matches it
    assert sent.event.event_type == events.AIEventTypes.recipe_ingestion_ready


def test_the_test_notification_counts_waiting_cards_apart(unique_user_fn_scoped: TestUser, published: Outbox):
    """As a batch's notification does: a card waiting for the monthly limit hasn't failed"""
    user = unique_user_fn_scoped
    make_batch(user, "ready", "failed", "waiting", "waiting")
    target = events.NotifierURL(uuid4(), "Kitchen HA", "json://ha.local/hook")
    with session_context() as session:
        sent = events.send_test_notification(
            session, UUID(user.group_id), UUID(user.household_id), target, translator_for("en-US")
        )
    assert sent is True
    [test] = published
    assert (test.data.ready_count, test.data.failed_count, test.data.waiting_count) == (1, 1, 2)


def test_a_language_without_the_texts_falls_back_to_english(
    unique_user_fn_scoped: TestUser, published: list[Published]
):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    batch_id = make_batch(unique_user_fn_scoped, "ready", "ready", locale="de-DE")

    assert events.maybe_notify_batch(batch_id) is True
    [sent] = published
    assert "recipe-ingest" not in sent.event.message.title
    assert "recipe-ingest" not in sent.event.message.body
    assert "2" in sent.event.message.body


def test_a_household_without_notifiers_still_marks_the_batch(
    unique_user_fn_scoped: TestUser, published: list[Published]
):
    notifier(unique_user_fn_scoped, "json://ha.local/hook", ready=False)
    batch_id = make_batch(unique_user_fn_scoped, "ready")

    assert events.maybe_notify_batch(batch_id) is True
    assert published == []
    assert batch_row(batch_id)["notified_at"] is not None
    assert events.maybe_notify_batch(batch_id) is False


# ==================================================================================================================
# When it's sent: once per finished batch


def test_sent_once_the_batch_is_sealed_and_nothing_is_processing(
    unique_user_fn_scoped: TestUser, published: list[Published]
):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    batch_id = make_batch(unique_user_fn_scoped, "ready", "processing", sealed=False)

    assert events.maybe_notify_batch(batch_id) is False  # not sealed
    with session_context() as session:
        repos = IngestRepos(session, UUID(unique_user_fn_scoped.group_id), UUID(unique_user_fn_scoped.household_id))
        assert repos.batches.seal(batch_id, utcnow())
    assert events.maybe_notify_batch(batch_id) is False  # a card is still being read

    set_status(batch_id, IngestStatus.ready)
    assert events.maybe_notify_batch(batch_id) is True
    assert events.maybe_notify_batch(batch_id) is False  # once
    assert len(published) == 1
    assert batch_row(batch_id)["notified_at"] is not None


def finish_together(batch_id: UUID) -> list[bool]:
    """
    Every job of the batch finishes at the same moment, each in its own thread and session, as task threads finalize;
    then each checks the batch, as a task does after a first extraction. What each check returned.
    """
    with session_context() as session:
        job_ids = list(
            session.execute(sa.select(RecipeIngestionJob.id).where(RecipeIngestionJob.batch_id == batch_id)).scalars()
        )

    barrier = threading.Barrier(len(job_ids))
    results: list[bool] = []
    errors: list[BaseException] = []

    def finish(job_id: UUID) -> None:
        try:
            with session_context() as session:
                session.execute(
                    sa.update(RecipeIngestionJob)
                    .where(RecipeIngestionJob.id == job_id)
                    .values(status=IngestStatus.ready.value)
                )
                session.commit()
            barrier.wait(timeout=10)
            results.append(events.maybe_notify_batch(batch_id))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=finish, args=(job_id,)) for job_id in job_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    return results


def test_one_notification_when_two_cards_finish_together(unique_user_fn_scoped: TestUser, published: list[Published]):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")

    for _ in range(4):
        batch_id = make_batch(unique_user_fn_scoped, "processing", "processing")
        assert sorted(finish_together(batch_id)) == [False, True]
        assert len(for_batch(published, batch_id)) == 1


CUTOFF = timedelta(seconds=limits.NOTIFY_CUTOFF)


def test_a_batch_read_over_days_notifies_when_its_last_card_finishes(
    unique_user_fn_scoped: TestUser, published: list[Published]
):
    """A slow reader, a long outage or rate limits: what counts is when its cards were last read, not its age"""
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    three_days = timedelta(days=3)
    by_task = make_batch(unique_user_fn_scoped, "ready", "processing", age=three_days, active=three_days)
    by_housekeeping = make_batch(unique_user_fn_scoped, "ready", "processing", age=three_days, active=three_days)

    events.housekeeping(utcnow())
    assert events.maybe_notify_batch(by_task) is False  # its last card is still being read
    assert batch_row(by_task)["notified_at"] is None
    assert batch_row(by_housekeeping)["notified_at"] is None

    # the last cards finish now: a plain status update, as the runner writes it, records when (`update_at`)
    set_status(by_task, IngestStatus.ready)
    set_status(by_housekeeping, IngestStatus.ready)

    assert events.maybe_notify_batch(by_task) is True
    events.housekeeping(utcnow())
    for batch_id in (by_task, by_housekeeping):
        [sent] = for_batch(published, batch_id)
        assert sent.data.ready_count == 2


def test_a_batch_whose_cards_were_last_active_over_24_hours_ago_never_notifies(
    unique_user_fn_scoped: TestUser, published: list[Published]
):
    """A restored backup's batch, finished long ago but never notified, doesn't send old news"""
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    restored = make_batch(user, "ready", "failed", age=CUTOFF * 3, active=CUTOFF + timedelta(hours=1))
    recent = make_batch(user, "ready", age=CUTOFF * 3, active=CUTOFF - timedelta(hours=1))

    assert events.maybe_notify_batch(restored) is False
    events.housekeeping(utcnow())

    assert for_batch(published, restored) == []
    assert len(for_batch(published, recent)) == 1
    # settled, so opening and editing one of its cards later doesn't make it due
    assert batch_row(restored)["notified_at"] is not None
    with session_context() as session:
        session.execute(
            sa.update(RecipeIngestionJob).where(RecipeIngestionJob.batch_id == restored).values(title="Edited")
        )
        session.commit()
    assert events.maybe_notify_batch(restored) is False
    events.housekeeping(utcnow())
    assert for_batch(published, restored) == []


def test_housekeeping_leaves_old_batches_that_are_still_being_read(
    unique_user_fn_scoped: TestUser, published: list[Published]
):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    waiting = make_batch(unique_user_fn_scoped, "processing", age=CUTOFF * 2, active=CUTOFF * 2)

    events.housekeeping(utcnow())
    assert batch_row(waiting)["notified_at"] is None  # not settled: its card may still be read

    set_status(waiting, IngestStatus.failed)
    events.housekeeping(utcnow())
    [sent] = for_batch(published, waiting)
    assert sent.event.message.body == "No cards are ready to review (1 failed)."


def test_a_batch_with_nothing_to_look_at_isnt_sent(unique_user_fn_scoped: TestUser, published: list[Published]):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    reviewed = make_batch(unique_user_fn_scoped, "committed", "committed")
    empty = make_batch(unique_user_fn_scoped)

    assert events.maybe_notify_batch(reviewed) is False
    assert events.maybe_notify_batch(empty) is False
    assert batch_row(reviewed)["notified_at"] is not None
    # a batch without cards (Done before any capture) has no card activity: housekeeping settles it
    events.housekeeping(utcnow())
    assert for_batch(published, reviewed) == [] and for_batch(published, empty) == []
    # both are finished: housekeeping doesn't look at them again
    assert batch_row(empty)["notified_at"] is not None


def test_housekeeping_seals_idle_batches_and_sends_what_became_due(
    unique_user_fn_scoped: TestUser, published: list[Published]
):
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    three_minutes = timedelta(minutes=3)
    api_batch = make_batch(user, "ready", sealed=False, idle=three_minutes, source=IngestSource.api)
    inbox_batch = make_batch(user, "failed", sealed=False, idle=three_minutes, source=IngestSource.inbox, locale=None)
    app_batch = make_batch(user, "ready", sealed=False, idle=three_minutes)  # app batches wait 10 minutes
    busy_batch = make_batch(user, "processing", sealed=False, idle=three_minutes, source=IngestSource.api)

    events.housekeeping(utcnow())

    for batch_id in (api_batch, inbox_batch):
        row = batch_row(batch_id)
        assert row["sealed_at"] is not None and row["notified_at"] is not None
        assert len(for_batch(published, batch_id)) == 1
    assert for_batch(published, inbox_batch)[0].event.message.body == "No cards are ready to review (1 failed)."

    assert batch_row(app_batch) == {"sealed_at": None, "notified_at": None}
    # sealed, but its card is still being read: the next run (or the card's task) sends it
    assert batch_row(busy_batch)["sealed_at"] is not None
    assert batch_row(busy_batch)["notified_at"] is None

    set_status(busy_batch, IngestStatus.ready)
    events.housekeeping(utcnow() + timedelta(seconds=limits.APP_BATCH_IDLE + 60))
    assert len(for_batch(published, busy_batch)) == 1
    assert len(for_batch(published, app_batch)) == 1
    assert len(for_batch(published, api_batch)) == 1  # never twice


def test_housekeeping_carries_on_past_a_failing_notifier(
    unique_user_fn_scoped: TestUser,
    published: Outbox,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    notifier(unique_user_fn_scoped, "json://secret-token@ha.local/hook")
    first = make_batch(unique_user_fn_scoped, "ready")
    second = make_batch(unique_user_fn_scoped, "ready")

    def deliver(event: events.AIEvent, url: str) -> bool:
        assert isinstance(event.document_data, events.EventIngestionReadyData)
        if event.document_data.batch_id == first:
            raise RuntimeError(f"{url} is down")
        return published.deliver(event, url)

    monkeypatch.setattr(events, "deliver", deliver)
    with caplog.at_level("WARNING"):
        events.housekeeping(utcnow())

    assert len(for_batch(published, second)) == 1
    assert batch_row(first)["notified_at"] is None  # tried again once the lease has passed
    assert f"Recipe card batch {first}" in caplog.text and "RuntimeError" in caplog.text
    assert "secret-token" not in caplog.text

    monkeypatch.setattr(events, "deliver", published.deliver)
    events.housekeeping(utcnow() + LEASE)
    assert len(for_batch(published, first)) == 1
    assert batch_row(first)["notified_at"] is not None


# ==================================================================================================================
# Delivery: checked per notifier, retried after the lease, at least once each

LEASE = timedelta(seconds=limits.NOTIFY_LEASE + 1)
"""Long enough for a claim's lease to have passed"""


def test_a_notifier_that_didnt_get_it_gets_it_after_the_lease(
    unique_user_fn_scoped: TestUser, published: Outbox, caplog: pytest.LogCaptureFixture
):
    user = unique_user_fn_scoped
    ha_id = notifier(user, "jsons://secret-token@ha.local/api/webhook/cards", name="Kitchen HA")
    notifier(user, "pover://user@token")
    batch_id = make_batch(user, "ready", "failed")
    published.down.add("ha.local")  # Home Assistant is down

    with caplog.at_level("WARNING"):
        assert events.maybe_notify_batch(batch_id) is False

    assert published.delivered_to(batch_id, "pover://") == 1
    assert published.delivered_to(batch_id, "ha.local") == 0
    state = notify_state(batch_id)
    assert state["notified_at"] is None and state["notify_attempts"] == 1
    assert len(state["notify_delivered"]) == 1  # Pushover's hash, no URL
    assert all(len(key) == 64 and "pover" not in key for key in state["notify_delivered"])
    # the failure names the batch and the notifier, never its URL
    assert f"Recipe card batch {batch_id}" in caplog.text
    assert "'Kitchen HA'" in caplog.text and str(ha_id) in caplog.text
    assert "secret-token" not in caplog.text and "ha.local" not in caplog.text

    # nothing is tried again while the claim's lease lasts, by a card's task or by housekeeping
    assert events.maybe_notify_batch(batch_id) is False
    events.housekeeping(utcnow())
    assert published.tried(batch_id, "ha.local") == 1

    published.down.clear()  # it's back
    events.housekeeping(utcnow() + LEASE)
    assert published.delivered_to(batch_id, "ha.local") == 1
    assert published.delivered_to(batch_id, "pover://") == 1  # only the one that missed it is sent to again
    state = notify_state(batch_id)
    assert state["notified_at"] is not None and state["notify_attempts"] == 2
    assert len(state["notify_delivered"]) == 2

    events.housekeeping(utcnow() + LEASE * 2)
    assert published.tried(batch_id, "ha.local") == 2 and published.tried(batch_id, "pover://") == 1


def test_an_error_after_the_claim_is_tried_again_after_the_lease(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """A crash (or an error) between claiming the notification and sending it no longer loses it"""
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    batch_id = make_batch(unique_user_fn_scoped, "ready")
    ready_event = events._ready_event

    def crash(*args: Any) -> None:
        raise RuntimeError("the process died here")

    monkeypatch.setattr(events, "_ready_event", crash)
    with pytest.raises(RuntimeError):
        events.maybe_notify_batch(batch_id)
    assert notify_state(batch_id)["notify_claimed_at"] is not None
    assert notify_state(batch_id)["notified_at"] is None

    monkeypatch.setattr(events, "_ready_event", ready_event)
    events.housekeeping(utcnow())
    assert for_batch(published, batch_id) == []  # its lease still holds

    events.housekeeping(utcnow() + LEASE)
    [sent] = for_batch(published, batch_id)
    assert sent.urls[0].startswith("json://ha.local/hook?")
    assert notify_state(batch_id)["notified_at"] is not None


def test_apprise_failing_then_succeeding_delivers_once_per_notifier(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Through the real `deliver`: Apprise's own answer decides, and a notifier that took it isn't sent it again"""
    import apprise

    user = unique_user_fn_scoped
    first, second = (f"{random_string().lower()}.local" for _ in range(2))
    notifier(user, f"json://{first}/hook")
    notifier(user, f"json://{second}/hook")
    batch_id = make_batch(user, "ready")
    up = {first}
    notified: list[str] = []

    def notify(self: apprise.Apprise, *args: Any, **kwargs: Any) -> bool:
        [server] = list(self)  # one notifier at a time
        if server.host not in up:
            return False
        notified.append(server.host)
        return True

    monkeypatch.setattr(apprise.Apprise, "notify", notify)
    assert events.maybe_notify_batch(batch_id) is False
    assert notified == [first]

    up.add(second)
    events.housekeeping(utcnow() + LEASE)
    assert notified == [first, second]
    assert notify_state(batch_id)["notified_at"] is not None


def test_two_processes_send_once_per_notifier(unique_user_fn_scoped: TestUser, published: Outbox):
    """
    While one process sends the notification, another (finishing a card, or its housekeeping) finds it taken; once
    the first has recorded each notifier, nothing is sent twice
    """
    user = unique_user_fn_scoped
    notifier(user, "json://first.local/hook")
    notifier(user, "json://second.local/hook")
    batch_id = make_batch(user, "ready")

    sending = threading.Event()
    go_on = threading.Event()
    deliver = published.deliver

    def slow(event: events.AIEvent, url: str) -> bool:
        sending.set()
        assert go_on.wait(10)
        return deliver(event, url)

    results: list[bool] = []
    first = threading.Thread(target=lambda: results.append(events.maybe_notify_batch(batch_id)))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(events, "deliver", slow)
        first.start()
        assert sending.wait(10)
        # the other process, while the first is sending
        assert events.maybe_notify_batch(batch_id) is False
        events.housekeeping(utcnow())
        go_on.set()
        first.join(10)

    assert results == [True]
    assert published.tried(batch_id, "first.local") == 1
    assert published.tried(batch_id, "second.local") == 1
    events.housekeeping(utcnow() + LEASE)
    assert published.tried(batch_id, "first.local") + published.tried(batch_id, "second.local") == 2


def test_an_attempt_that_outlives_its_lease_stops_when_another_takes_over(
    unique_user_fn_scoped: TestUser, published: Outbox
):
    """
    A process stuck sending past the lease: housekeeping elsewhere takes the notification over and sends it; the
    stuck one records nothing more and sends to no other notifier
    """
    user = unique_user_fn_scoped
    notifier(user, "json://first.local/hook")
    notifier(user, "json://second.local/hook")
    batch_id = make_batch(user, "ready")

    stuck = threading.Event()
    go_on = threading.Event()
    deliver = published.deliver

    def deliver_stuck_once(event: events.AIEvent, url: str) -> bool:
        if threading.current_thread() is not threading.main_thread() and not stuck.is_set():
            stuck.set()
            assert go_on.wait(10)
        return deliver(event, url)

    results: list[bool] = []
    slow = threading.Thread(target=lambda: results.append(events.maybe_notify_batch(batch_id)))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(events, "deliver", deliver_stuck_once)
        slow.start()
        assert stuck.wait(10)
        events.housekeeping(utcnow() + LEASE)  # takes over: attempt 2 sends to both
        go_on.set()
        slow.join(10)

    assert results == [False]
    assert notify_state(batch_id)["notify_attempts"] == 2
    assert notify_state(batch_id)["notified_at"] is not None
    # the stuck attempt finished its one send (which a crash at that moment would also double), then stopped
    tried = [published.tried(batch_id, host) for host in ("first.local", "second.local")]
    assert sorted(tried) == [1, 2]


def test_a_send_that_takes_longer_than_the_lease_keeps_its_claim(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """
    A live process whose notifier takes longer than the lease renews its claim while it sends, so housekeeping running
    meanwhile doesn't start the same notification over: each notifier gets it once
    """
    monkeypatch.setattr(limits, "NOTIFY_LEASE", 0.6)
    user = unique_user_fn_scoped
    notifier(user, "json://first.local/hook")
    notifier(user, "json://second.local/hook")
    batch_id = make_batch(user, "ready")

    sending = threading.Event()
    go_on = threading.Event()
    deliver = published.deliver

    def deliver_slowly(event: events.AIEvent, url: str) -> bool:
        if threading.current_thread() is not threading.main_thread() and not sending.is_set():
            sending.set()
            assert go_on.wait(10)
        return deliver(event, url)

    results: list[bool] = []
    slow = threading.Thread(target=lambda: results.append(events.maybe_notify_batch(batch_id)))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(events, "deliver", deliver_slowly)
        slow.start()
        assert sending.wait(10)
        for _ in range(3):  # well past the lease, with the first notifier still busy
            time.sleep(0.5)
            events.housekeeping(utcnow())
        go_on.set()
        slow.join(10)

    assert results == [True]
    assert notify_state(batch_id)["notify_attempts"] == 1
    assert [published.tried(batch_id, host) for host in ("first.local", "second.local")] == [1, 1]


def _job_ids(batch_id: UUID) -> list[UUID]:
    with session_context() as session:
        stmt = sa.select(RecipeIngestionJob.id).where(RecipeIngestionJob.batch_id == batch_id)
        return list(session.execute(stmt.order_by(RecipeIngestionJob.position)).scalars())


def _arm(job_id: UUID, *, reset: bool = False) -> None:
    """
    What the automatic retry does as it queues a card that waited for a monthly limit (the queueing commits): `reset`
    when its limit's reset came, else a lift queued it
    """
    with session_context() as session:
        events.arm_limit_wave(session, {job_id: reset})
        session.commit()


def _set_notify(batch_id: UUID, **values: Any) -> None:
    with session_context() as session:
        session.execute(sa.update(RecipeIngestionBatch).where(RecipeIngestionBatch.id == batch_id).values(**values))
        session.commit()


def test_queueing_a_card_that_waited_arms_its_batch_again(unique_user_fn_scoped: TestUser, published: Outbox):
    user = unique_user_fn_scoped
    notifier(user, "json://first.local/hook")
    batch_id = make_batch(user, "ready", "failed", "failed", "failed")
    _, first, second, third = _job_ids(batch_id)
    assert events.maybe_notify_batch(batch_id) is True
    hashes = notify_state(batch_id)["notify_delivered"]

    # the batch's notification went out: it's due again for the card, once it's read
    _arm(first)
    state = notify_state(batch_id)
    assert (state["notified_at"], state["notify_claimed_at"], state["notify_attempts"]) == (None, None, 0)
    assert state["notify_delivered"] == [f"limit-wave:{first}"]

    # another card joins the wave still to come; its entry says its reset queued it (a lift queued the first)
    _arm(second, reset=True)
    wave = sorted([f"limit-wave:{first}", f"limit-reset:{second}"])
    assert notify_state(batch_id)["notify_delivered"] == wave

    # a wave being sent goes on for its own cards: one queued meanwhile is its next wave, not told of in this one
    with session_context() as session:
        sending = events._claim(session, batch_id, utcnow())
    assert sending is not None and sending.attempt == 1
    claimed = notify_state(batch_id)
    _arm(third)
    state = notify_state(batch_id)
    assert (state["notify_claimed_at"], state["notify_attempts"]) == (
        claimed["notify_claimed_at"],
        claimed["notify_attempts"],
    )
    assert state["notify_delivered"] == sorted([*wave, f"next:limit-wave:{third}"])
    # the attempt still holds it: its records keep the next wave, and its last one starts it
    with session_context() as session:
        assert events._renew_claim(sending.lease) is True
        assert events._record(session, sending.lease, [*wave, *hashes]) is True
        assert notify_state(batch_id)["notify_delivered"] == sorted([*wave, *hashes, f"next:limit-wave:{third}"])
        assert events._record(session, sending.lease, [*wave, *hashes], notified_at=utcnow()) is True
    assert sending.lease.next_wave is True
    state = notify_state(batch_id)
    assert (state["notified_at"], state["notify_claimed_at"], state["notify_attempts"]) == (None, None, 0)
    assert state["notify_delivered"] == [f"limit-wave:{third}"]
    # and that attempt writes nothing more
    with session_context() as session:
        assert events._renew_claim(sending.lease) is False
        assert events._record(session, sending.lease, hashes, notified_at=utcnow()) is False

    # a batch whose own notification is still to come counts the card as it is once read: nothing changes
    other = make_batch(user, "failed")
    [card] = _job_ids(other)
    before = notify_state(other)
    _arm(card)
    assert notify_state(other) == before

    # one being sent (or waiting to be tried again) goes on as it is, and the card is its next wave
    with session_context() as session:
        own = events._claim(session, other, utcnow())
    assert own is not None
    _arm(card, reset=True)
    assert notify_state(other)["notify_delivered"] == [f"next:limit-reset:{card}"]
    with session_context() as session:
        assert events._record(session, own.lease, [], notified_at=utcnow()) is True
    state = notify_state(other)
    assert (state["notified_at"], state["notify_claimed_at"], state["notify_attempts"], state["notify_delivered"]) == (
        None,
        None,
        0,
        [f"limit-reset:{card}"],
    )


def test_a_notification_given_up_on_starts_its_next_wave(unique_user_fn_scoped: TestUser, published: Outbox):
    """The last attempt's process died while a card was queued: giving up on it still tells of that card once read"""
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    batch_id = make_batch(unique_user_fn_scoped, "ready", "failed")
    _, card = _job_ids(batch_id)
    now = utcnow()
    _set_notify(batch_id, notify_attempts=limits.NOTIFY_ATTEMPTS, notify_claimed_at=now)  # the last attempt, dying
    _arm(card)
    assert notify_state(batch_id)["notify_delivered"] == [f"next:limit-wave:{card}"]

    with session_context() as session:
        assert batch_id in events.give_up_batches(session, now + LEASE)
    state = notify_state(batch_id)
    assert (state["notified_at"], state["notify_claimed_at"], state["notify_attempts"], state["notify_delivered"]) == (
        None,
        None,
        0,
        [f"limit-wave:{card}"],
    )
    _read(card)
    assert events.maybe_notify_batch(batch_id) is True
    [wave] = for_batch(published, batch_id)
    assert wave.data.job_ids == [card]
    assert wave.event.message.body.startswith("1 card that waited for the monthly limit was read.")


# ==================================================================================================================
# A card queued again while its batch's notification is being claimed, by another process at the same moment

CLAIM_STATEMENT = "UPDATE recipe_ingestion_batches SET notify_claimed_at"
ARM_STATEMENT = "SELECT recipe_ingestion_batches.notified_at"


def _postgres() -> bool:
    return db_setup.engine.dialect.name == "postgresql"


def _waits_for_a_lock(statement: str) -> bool:
    """PostgreSQL: whether another session's statement starting with `statement` waits for a lock (False on SQLite)"""
    if not _postgres():
        return False
    with session_context() as session:
        waiting = session.execute(
            sa.text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE :query"),
            {"query": f"{statement}%"},
        ).scalar_one()
        session.commit()
    return waiting > 0


def _until(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def _set_job(job_id: UUID, **values: Any) -> None:
    with session_context() as session:
        values = {"update_at": utcnow(), **values}
        session.execute(sa.update(RecipeIngestionJob).where(RecipeIngestionJob.id == job_id).values(**values))
        session.commit()


def _waits_for_its_reset(job_id: UUID) -> None:
    """The card failed `limit_reached`, and its retry time has come"""
    _set_job(
        job_id,
        status=IngestStatus.failed.value,
        error_code=IngestErrorCode.limit_reached.value,
        auto_retry_at=utcnow() - timedelta(seconds=5),
        task_kind=None,
        task_state=None,
    )


def _read(job_id: UUID) -> None:
    """The card was read: its first extraction's finalize"""
    _set_job(
        job_id, status=IngestStatus.ready.value, error_code=None, auto_retry_at=None, task_kind=None, task_state=None
    )


def _only_the_batchs_waiting_cards(monkeypatch: pytest.MonkeyPatch, batch_id: UUID) -> None:
    """The retry phase reads every group's waiting cards: keep it to the batch's"""
    ids = set(_job_ids(batch_id))
    waiting_for_limit = IngestQueue.waiting_for_limit

    def own(self: IngestQueue) -> list[LimitWait]:
        return [wait for wait in waiting_for_limit(self) if wait.job_id in ids]

    monkeypatch.setattr(IngestQueue, "waiting_for_limit", own)


class _Running:
    """A call in a thread of its own (another process's), with what it returned or raised"""

    def __init__(self, call: Callable[[], Any]) -> None:
        self.result: Any = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                self.result = call()
            except BaseException as e:
                self.error = e

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def join(self) -> Any:
        self.thread.join(30)
        assert not self.thread.is_alive()
        if self.error is not None:
            raise self.error
        return self.result


def test_a_card_queued_while_its_wave_is_claimed_is_in_the_wave(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """
    The retry phase queues a card that waited (arming its batch's wave) while another card of the wave finishes and
    claims it. The claim waits for the queueing's commit and then finds the card being read, so it leaves the wave to
    the card's own finalize: once read, the card is in it. (PostgreSQL's claim checked the cards as they were when its
    statement began, before the queueing committed, and sent the wave without the card, for good.)
    """
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    batch_id = make_batch(user, "ready", "waiting", "waiting")
    _, first, second = _job_ids(batch_id)
    _only_the_batchs_waiting_cards(monkeypatch, batch_id)
    assert events.maybe_notify_batch(batch_id) is True  # the batch's own notification

    _waits_for_its_reset(first)
    assert retries.retry_waiting(utcnow()) == 1  # queued, its batch's wave armed
    _read(first)
    _waits_for_its_reset(second)

    # the retry phase's transaction stops before its commit, with the second card queued and the wave armed
    held, go = threading.Event(), threading.Event()
    arming: dict[str, threading.Thread] = {}
    arm = events.arm_limit_wave

    def arm_and_mark(session: Session, queued: dict[UUID, bool]) -> None:
        arm(session, queued)
        arming["thread"] = threading.current_thread()

    def before_commit(session: Session) -> None:
        if arming.get("thread") is threading.current_thread() and not held.is_set():
            held.set()
            assert go.wait(30)

    def claim() -> bool:
        return events.maybe_notify_batch(batch_id)  # the first card's finalize

    monkeypatch.setattr(events, "arm_limit_wave", arm_and_mark)
    sa.event.listen(Session, "before_commit", before_commit)
    try:
        queueing = _Running(lambda: retries.retry_waiting(utcnow()))
        assert held.wait(30)
        if _postgres():
            claiming = _Running(claim)
            assert _until(lambda: _waits_for_a_lock(CLAIM_STATEMENT), 10)  # for the batch's row
        go.set()
        assert queueing.join() == 1
        if not _postgres():
            claiming = _Running(claim)  # SQLite serializes the two writers: the claim comes after the commit
        assert claiming.join() is False  # the second card is being read: the wave is its finalize's
    finally:
        go.set()
        sa.event.remove(Session, "before_commit", before_commit)

    _read(second)
    assert events.maybe_notify_batch(batch_id) is True
    [wave] = [sent for sent in for_batch(published, batch_id) if "waited" in sent.event.message.body]
    assert wave.data.job_ids == [first, second]
    assert wave.event.message.body.startswith("2 cards that waited for the monthly limit were read.")


def test_a_card_queued_while_its_batch_is_claimed_is_counted_once_read(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """
    The retry phase queues a card that waited while the batch's last card finishes and claims the batch's own
    notification. The queueing waits for the claim's commit and finds it: the notification goes on as it is, and the
    card is the batch's next wave, told of once it's read. (On PostgreSQL the queueing read the batch's state without
    waiting for the claim, found nothing claimed and left it: the card was never counted.)
    """
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    batch_id = make_batch(user, "ready", "waiting", "processing")
    _, waiting, last = _job_ids(batch_id)
    _only_the_batchs_waiting_cards(monkeypatch, batch_id)
    _waits_for_its_reset(waiting)
    assert events.maybe_notify_batch(batch_id) is False  # the last card is still being read

    claimed, queued = threading.Event(), threading.Event()
    finishing: dict[str, threading.Thread] = {}

    def after_execute(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if finishing.get("thread") is threading.current_thread() and statement.startswith(CLAIM_STATEMENT):
            if not claimed.is_set() and _postgres():
                claimed.set()
                # the claim holds the batch's row, uncommitted, while the retry phase queues the card: until the
                # queueing is done, or waits for the row
                _until(lambda: queued.is_set() or _waits_for_a_lock(ARM_STATEMENT), 10)

    def finish_the_last_card() -> bool:
        finishing["thread"] = threading.current_thread()
        _read(last)
        return events.maybe_notify_batch(batch_id)

    def queue_the_waiting_card() -> int:
        try:
            return retries.retry_waiting(utcnow())
        finally:
            queued.set()

    sa.event.listen(db_setup.engine, "after_cursor_execute", after_execute)
    try:
        finishing_card = _Running(finish_the_last_card)
        if _postgres():
            assert claimed.wait(30)
        else:
            finishing_card.join()  # SQLite serializes the two writers: the queueing comes after the claim's commit
        queueing = _Running(queue_the_waiting_card)
        assert queueing.join() == 1
        finishing_card.join()
    finally:
        sa.event.remove(db_setup.engine, "after_cursor_execute", after_execute)

    # the batch's notification never counts the card as read: it waited still, or was queued (and isn't counted) by
    # the time the notification was put together
    [own] = for_batch(published, batch_id)
    assert (own.data.ready_count, own.data.failed_count) == (2, 0) and own.data.waiting_count in (0, 1)
    _read(waiting)
    assert events.maybe_notify_batch(batch_id) is True  # the card is told once read, in a wave of its own
    [_, sent] = for_batch(published, batch_id)
    assert sent.data.job_ids == [waiting]
    assert sent.event.message.body.startswith("1 card that waited for the monthly limit was read.")


def _waiting_list(monkeypatch: pytest.MonkeyPatch, *order: UUID) -> list[UUID]:
    """
    The retry phase's list of waiting cards: those of `order` that wait, in that order. The list returned is `order`:
    change it for the next run.
    """
    listing = list(order)
    waiting_for_limit = IngestQueue.waiting_for_limit

    def listed(self: IngestQueue) -> list[LimitWait]:
        waits = {wait.job_id: wait for wait in waiting_for_limit(self) if wait.job_id in listing}
        return [waits[job_id] for job_id in listing if job_id in waits]

    monkeypatch.setattr(IngestQueue, "waiting_for_limit", listed)
    return listing


def _status(job_id: UUID) -> str:
    with session_context() as session:
        stmt = sa.select(RecipeIngestionJob.status).where(RecipeIngestionJob.id == job_id)
        return session.execute(stmt).scalar_one()


def test_the_waiting_cards_are_listed_batch_by_batch(unique_user_fn_scoped: TestUser):
    """On a reset day every card has the same retry time: each batch's cards come together, in capture order"""
    user = unique_user_fn_scoped
    batches = [make_batch(user, "waiting", "waiting", "waiting") for _ in range(3)]
    cards = {job_id: batch_id for batch_id in batches for job_id in _job_ids(batch_id)}
    with session_context() as session:
        listed = [wait for wait in IngestQueue(session).waiting_for_limit() if wait.job_id in cards]

    assert [wait.batch_id for wait in listed] == [cards[wait.job_id] for wait in listed]
    order = [wait.batch_id for wait in listed]
    assert order == sorted(order, key=order.index)  # one run of each batch
    for batch_id in batches:
        assert [wait.job_id for wait in listed if wait.batch_id == batch_id] == _job_ids(batch_id)


def test_a_batchs_waiting_cards_are_queued_together(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """
    One retry run queues every household's due cards, another batch's between this one's (by their list): each batch's
    are queued in one transaction, so a dispatcher that reads one of them right away finds its batch-mate being read
    too, and the batch gets one wave for both. (Queued one by one, the first was read and told of in a wave of its own
    before the run reached the second.)
    """
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    batch_id = make_batch(user, "ready", "waiting", "waiting")
    other = make_batch(user, "waiting")
    _, first, second = _job_ids(batch_id)
    [between] = _job_ids(other)
    for batch in (batch_id, other):
        assert events.maybe_notify_batch(batch) is True
    told = len(for_batch(published, batch_id))
    for job_id in (first, between, second):
        _waits_for_its_reset(job_id)
    _waiting_list(monkeypatch, first, between, second)

    batch_mate: list[str] = []
    """The second card's status when a dispatcher read the first"""

    def after_commit(session: Session) -> None:
        # a dispatcher reads the first card as soon as it's queued, and its finalize notifies the batch
        if not batch_mate and _status(first) == IngestStatus.processing.value:
            batch_mate.append(_status(second))
            _read(first)
            events.maybe_notify_batch(batch_id)

    sa.event.listen(Session, "after_commit", after_commit)
    try:
        assert retries.retry_waiting(utcnow()) == 3
    finally:
        sa.event.remove(Session, "after_commit", after_commit)
    assert batch_mate == [IngestStatus.processing.value]  # queued with it, so it's still being read

    _read(second)
    assert events.maybe_notify_batch(batch_id) is True
    [wave] = for_batch(published, batch_id)[told:]
    assert wave.data.job_ids == [first, second]
    assert wave.event.message.body.startswith("2 cards that waited for the monthly limit were read.")


def test_a_card_queued_while_its_wave_is_being_sent_is_the_next_wave(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """
    Wave [C1] is claimed and being sent when the retry phase queues C2. The send goes on for C1, whose counts it has
    right, and C2 is the batch's next wave, told of once it's read: each card is told of once. (The wave used to start
    over with both, after its notifiers already had it: they heard of C1 twice.)
    """
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    batch_id = make_batch(user, "ready", "waiting", "waiting")
    _, c1, c2 = _job_ids(batch_id)
    assert events.maybe_notify_batch(batch_id) is True
    told = len(for_batch(published, batch_id))
    listing = _waiting_list(monkeypatch, c1)
    _waits_for_its_reset(c1)
    assert retries.retry_waiting(utcnow()) == 1
    _read(c1)

    sending, go_on = threading.Event(), threading.Event()
    deliver = published.deliver

    def deliver_slowly(event: events.AIEvent, url: str) -> bool:
        if not sending.is_set():
            sending.set()
            assert go_on.wait(30)
        return deliver(event, url)

    monkeypatch.setattr(events, "deliver", deliver_slowly)
    try:
        finalize = _Running(lambda: events.maybe_notify_batch(batch_id))  # C1's finalize sends its wave
        assert sending.wait(30)
        claimed = notify_state(batch_id)
        listing[:] = [c2]
        _waits_for_its_reset(c2)
        assert retries.retry_waiting(utcnow()) == 1
        state = notify_state(batch_id)
        assert (state["notify_claimed_at"], state["notify_attempts"]) == (
            claimed["notify_claimed_at"],
            claimed["notify_attempts"],
        )
        assert f"next:limit-reset:{c2}" in state["notify_delivered"]
    finally:
        go_on.set()
    assert finalize.join() is True

    state = notify_state(batch_id)
    assert (state["notified_at"], state["notify_claimed_at"], state["notify_delivered"]) == (
        None,
        None,
        [f"limit-reset:{c2}"],
    )
    _read(c2)
    assert events.maybe_notify_batch(batch_id) is True
    waves = for_batch(published, batch_id)[told:]
    assert [wave.data.job_ids for wave in waves] == [[c1], [c2]]
    assert all(w.event.message.body.startswith("1 card that waited for the monthly limit was read.") for w in waves)
    assert published.delivered_to(batch_id, "ha.local") == told + 2


def test_a_card_read_while_its_wave_was_being_sent_is_told_of_right_after(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """The next wave's card was read before the wave in flight was done: its finalize found it claimed, so the
    attempt that starts the next wave sends it too"""
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    batch_id = make_batch(user, "ready", "waiting", "waiting")
    _, c1, c2 = _job_ids(batch_id)
    assert events.maybe_notify_batch(batch_id) is True
    told = len(for_batch(published, batch_id))
    listing = _waiting_list(monkeypatch, c1)
    _waits_for_its_reset(c1)
    assert retries.retry_waiting(utcnow()) == 1
    _read(c1)

    sending, go_on = threading.Event(), threading.Event()
    deliver = published.deliver

    def deliver_slowly(event: events.AIEvent, url: str) -> bool:
        if not sending.is_set():
            sending.set()
            assert go_on.wait(30)
        return deliver(event, url)

    monkeypatch.setattr(events, "deliver", deliver_slowly)
    try:
        finalize = _Running(lambda: events.maybe_notify_batch(batch_id))
        assert sending.wait(30)
        listing[:] = [c2]
        _waits_for_its_reset(c2)
        assert retries.retry_waiting(utcnow()) == 1
        _read(c2)
        assert events.maybe_notify_batch(batch_id) is False  # C2's finalize: the wave is being sent
    finally:
        go_on.set()
    assert finalize.join() is True

    waves = for_batch(published, batch_id)[told:]
    assert [wave.data.job_ids for wave in waves] == [[c1], [c2]]
    assert notify_state(batch_id)["notified_at"] is not None


def test_no_card_is_told_of_twice_when_its_queueing_races_its_wave(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """
    C1's read and its wave run at the same time as C2's queueing, in every interleaving the two threads happen to
    take: each card is told of once, as read, whether in one wave or two
    """
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    allowed = _waiting_list(monkeypatch)
    rng = random.Random(6)

    for _ in range(20):
        batch_id = make_batch(user, "ready", "waiting", "waiting")
        _, c1, c2 = _job_ids(batch_id)
        assert events.maybe_notify_batch(batch_id) is True
        told = len(for_batch(published, batch_id))
        allowed[:] = [c1]
        _waits_for_its_reset(c1)
        assert retries.retry_waiting(utcnow()) == 1
        allowed[:] = [c2]
        _waits_for_its_reset(c2)
        delays = (rng.random() * 0.01, rng.random() * 0.01)

        def finish_c1(delay: float = delays[0], batch_id: UUID = batch_id, c1: UUID = c1) -> bool:
            time.sleep(delay)
            _read(c1)
            return events.maybe_notify_batch(batch_id)

        def queue_c2(delay: float = delays[1]) -> int:
            time.sleep(delay)
            return retries.retry_waiting(utcnow())

        finishing, queueing = _Running(finish_c1), _Running(queue_c2)
        finishing.join()
        assert queueing.join() == 1
        _read(c2)
        events.maybe_notify_batch(batch_id)
        events.housekeeping(utcnow() + LEASE)  # anything left claimed

        waves = for_batch(published, batch_id)[told:]
        told_of = [job_id for wave in waves for job_id in wave.data.job_ids]
        assert sorted(told_of) == sorted([c1, c2]), [w.event.message.body for w in waves]
        assert all("waited for the monthly limit" in wave.event.message.body for wave in waves)
        assert notify_state(batch_id)["notified_at"] is not None


def test_a_batch_housekeeping_reaches_late_gets_a_full_lease(
    unique_user_fn_scoped: TestUser, published: Outbox, monkeypatch: pytest.MonkeyPatch
):
    """
    A housekeeping run that reaches a batch long after it began (earlier batches' notifiers were slow) takes its
    lease from the time it claims it, so another process's housekeeping doesn't take the batch over and send it again
    while the first is still sending
    """
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")
    batch_id = make_batch(user, "ready")
    other_process: list[bool] = []
    deliver = published.deliver

    def deliver_while_another_process_runs(event: events.AIEvent, url: str) -> bool:
        data = event.document_data
        if isinstance(data, events.EventIngestionReadyData) and data.batch_id == batch_id and not other_process:
            other_process.append(False)
            other_process[0] = events._notify_batch(batch_id, utcnow())
        return deliver(event, url)

    monkeypatch.setattr(events, "deliver", deliver_while_another_process_runs)
    events.housekeeping(utcnow() - LEASE)  # the run began a lease ago

    assert other_process == [False]
    assert published.tried(batch_id, "ha.local") == 1
    state = notify_state(batch_id)
    assert state["notified_at"] is not None and state["notify_attempts"] == 1


def test_given_up_after_the_last_attempt(
    unique_user_fn_scoped: TestUser, published: Outbox, caplog: pytest.LogCaptureFixture
):
    user = unique_user_fn_scoped
    notifier(user, "json://secret-token@ha.local/hook", name="Kitchen HA")
    batch_id = make_batch(user, "ready")
    published.down.add("ha.local")

    with caplog.at_level("WARNING"):
        events.maybe_notify_batch(batch_id)
        for attempt in range(1, limits.NOTIFY_ATTEMPTS):
            assert notify_state(batch_id)["notified_at"] is None
            events.housekeeping(utcnow() + LEASE * attempt)

    assert published.tried(batch_id, "ha.local") == limits.NOTIFY_ATTEMPTS
    state = notify_state(batch_id)
    assert state["notified_at"] is not None and state["notify_attempts"] == limits.NOTIFY_ATTEMPTS
    [given_up] = [r for r in caplog.records if r.levelname == "ERROR" and str(batch_id) in r.message]
    assert f"Recipe card batch {batch_id}" in given_up.message and "'Kitchen HA'" in given_up.message
    assert "secret-token" not in caplog.text

    published.down.clear()
    events.housekeeping(utcnow() + LEASE * limits.NOTIFY_ATTEMPTS)
    assert published.tried(batch_id, "ha.local") == limits.NOTIFY_ATTEMPTS


def test_a_last_attempt_that_never_finished_is_given_up_after_its_lease(
    unique_user_fn_scoped: TestUser, published: Outbox, caplog: pytest.LogCaptureFixture
):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    batch_id = make_batch(unique_user_fn_scoped, "ready")
    now = utcnow()
    with session_context() as session:  # the last attempt was claimed, and its process died
        session.execute(
            sa.update(RecipeIngestionBatch)
            .where(RecipeIngestionBatch.id == batch_id)
            .values(notify_attempts=limits.NOTIFY_ATTEMPTS, notify_claimed_at=now)
        )
        session.commit()

    events.housekeeping(now)
    assert notify_state(batch_id)["notified_at"] is None  # its lease still holds
    with caplog.at_level("ERROR"):
        events.housekeeping(now + LEASE)

    assert notify_state(batch_id)["notified_at"] is not None
    assert f"Recipe card batch {batch_id}: gave up" in caplog.text
    assert for_batch(published, batch_id) == []


def test_deliver_reports_what_apprise_answered(monkeypatch: pytest.MonkeyPatch):
    """Upstream's `ApprisePublisher.publish` drops `Apprise.notify`'s answer; `deliver` doesn't"""
    import apprise

    event = events.AIEvent(
        message=events.EventBusMessage(title="Recipe cards ready", body="1 card is ready to review."),
        event_type=events.AIEventTypes.recipe_ingestion_ready,
        integration_id=events.INTERNAL_INTEGRATION_ID,
        document_data=events.EventIngestionReadyData(
            batch_id=uuid4(),
            job_ids=[],
            ready_count=1,
            needs_attention_count=0,
            failed_count=0,
            review_url="http://mealie.local/g/home/recipes/cards",
        ),
    )
    # a URL Apprise can't read: nothing to send to (Apprise's own `notify`, which answers None)
    assert events.deliver(event, "nosuchservice://ha.local/hook") is False

    answers = [True, False]
    sent_to: list[list[str]] = []

    def notify(self: apprise.Apprise, *args: Any, **kwargs: Any) -> bool:
        sent_to.append([server.host for server in self])
        return answers.pop(0)

    monkeypatch.setattr(apprise.Apprise, "notify", notify)
    assert events.deliver(event, "json://ha.local/hook") is True
    assert events.deliver(event, "json://other.local/hook") is False
    assert sent_to == [["ha.local"], ["other.local"]]  # each URL on its own

    def broken(self: apprise.Apprise, *args: Any, **kwargs: Any) -> bool:
        raise OSError("json://secret@ha.local unreachable")

    monkeypatch.setattr(apprise.Apprise, "notify", broken)
    with pytest.raises(OSError):
        events.deliver(event, "json://ha.local/hook")


def test_apprise_reads_the_event_data_and_the_notifiers_own_fields_back():
    """
    Home Assistant parses `document_data` with `from_json` (§8): its JSON must reach Apprise intact, and the user's own
    `:field` and `+header` values (a literal `+` included) must arrive as written. The fork's listener uses upstream's
    `update_urls_with_event_data`, which encodes this way since its fork hook (UH-01).
    """
    import apprise

    event = events.AIEvent(
        message=events.EventBusMessage(title="Recipe cards ready", body="2 cards are ready to review."),
        event_type=events.AIEventTypes.recipe_ingestion_ready,
        integration_id=events.INTERNAL_INTEGRATION_ID,
        document_data=events.EventIngestionReadyData(
            batch_id=uuid4(),
            job_ids=[uuid4()],
            ready_count=2,
            needs_attention_count=1,
            failed_count=0,
            review_url="http://mealie.local/g/home/recipes/cards/review?batch=x",
        ),
    )
    own = "json://ha.local:8123/api/webhook/abc?:token=a+b%2Bc&+X-Key=d+e&:room=living%20room"
    [url, other] = events.AIEventAppriseListener.update_urls_with_event_data([own, "mailto://user@example.com"], event)

    assert other == "mailto://user@example.com"
    notifier = apprise.Apprise.instantiate(url)
    assert notifier is not None
    extras = notifier.payload_extras
    assert json.loads(extras["document_data"]) == json.loads(event.document_data.model_dump_json(by_alias=True))
    assert extras["event_type"] == "recipe_ingestion_ready"
    assert extras["token"] == "a+b+c"
    assert extras["room"] == "living room"
    assert notifier.headers["X-Key"] == "d+e"


# ==================================================================================================================
# Inbox files that weren't added


def test_inbox_rejections_send_one_notification_with_counts_by_reason(
    api_client: TestClient, unique_user_fn_scoped: TestUser, h2_user: TestUser, published: list[Published]
):
    user = unique_user_fn_scoped
    home_assistant = "jsons://homeassistant.local:8123/api/webhook/mealie_cards?:room=living%20room"
    notifier(user, home_assistant)
    notifier(user, "pover://user@token")
    notifier(user, "json://not-opted-in.local/hook", ready=False)
    notifier(h2_user, "json://other-household.local/hook")
    reasons = [
        IngestRejectReason.duplicate,
        IngestRejectReason.too_large,
        IngestRejectReason.duplicate,
        None,  # refused for a reason without a code: a link, an empty folder
    ]

    assert events.notify_inbox_rejections(UUID(user.group_id), UUID(user.household_id), reasons) is True

    [sent] = published  # one notification for the whole scan burst
    assert sent.event.event_type is events.AIEventTypes.recipe_ingestion_rejected
    assert sent.event.integration_id == events.INTERNAL_INTEGRATION_ID
    assert sent.event.message.title == "Recipe cards not added"
    assert sent.event.message.body == (
        "4 recipe cards from the inbox weren't added (2 already scanned, 1 too large, 1 for another reason). "
        "They're in the inbox's failed folder."
    )
    assert sorted(url.split(":", 1)[0] for url in sent.urls) == ["jsons", "pover"]

    # what Home Assistant gets, after Apprise has decoded the URL
    params = custom_params(next(url for url in sent.urls if url.startswith("jsons://")))
    assert params[":event_type"] == "recipe_ingestion_rejected"
    assert params[":room"] == "living room"
    assert json.loads(params[":document_data"]) == {
        "documentType": "generic",
        "operation": "info",
        "count": 4,
        "reasons": {"duplicate": 2, "too_large": 1, "other": 1},
        "reviewUrl": f"http://localhost:8080/g/{group_slug(api_client, user)}/recipes/cards",
    }


def test_inbox_rejections_in_one_word(unique_user_fn_scoped: TestUser, published: list[Published]):
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook")

    events.notify_inbox_rejections(UUID(user.group_id), UUID(user.household_id), [IngestRejectReason.unreadable_image])
    events.notify_inbox_rejections(
        UUID(user.group_id), UUID(user.household_id), [IngestRejectReason.too_many_pixels], locale="de-DE"
    )

    first, second = (p.event.message.body for p in published)
    assert first == "1 recipe card from the inbox wasn't added (1 unreadable). It's in the inbox's failed folder."
    # a language without the texts yet gets English, never the texts' keys
    assert second == (
        "1 recipe card from the inbox wasn't added (1 with too many pixels). It's in the inbox's failed folder."
    )


@pytest.mark.parametrize("reason", list(IngestRejectReason))
def test_every_reject_reason_has_its_words(reason: IngestRejectReason):
    message = events.rejected_message({reason.value: 2}, events.translator_for(None))
    assert "recipe-ingest" not in message.body
    assert "another reason" not in message.body
    assert "2 " in message.body


def test_an_unknown_reason_code_is_another_reason():
    message = events.rejected_message({"made_up": 1}, events.translator_for("en-US"))
    assert "(1 for another reason)" in message.body


def test_inbox_rejections_without_a_notifier_send_nothing(unique_user_fn_scoped: TestUser, published: list[Published]):
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook", ready=False)
    notifier(user, "json://disabled.local/hook", enabled=False)
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)

    assert events.notify_inbox_rejections(group_id, household_id, [IngestRejectReason.duplicate]) is False
    assert published == []

    notifier(user, "json://ha.local/hook")
    assert events.notify_inbox_rejections(group_id, household_id, []) is False  # nothing refused
    assert published == []


def test_inbox_rejections_go_to_each_notifier_on_its_own(
    unique_user_fn_scoped: TestUser, published: Outbox, caplog: pytest.LogCaptureFixture
):
    """A notifier that's down doesn't keep the others from getting it, and it's logged by name"""
    user = unique_user_fn_scoped
    notifier(user, "json://ha.local/hook", name="Kitchen HA")
    notifier(user, "pover://user@token")
    published.down.add("ha.local")

    with caplog.at_level("WARNING"):
        sent = events.notify_inbox_rejections(UUID(user.group_id), UUID(user.household_id), [None])

    assert sent is True
    [delivered] = published
    assert delivered.urls == ["pover://user@token"]
    assert "'Kitchen HA'" in caplog.text and "not added" in caplog.text


def test_inbox_rejections_never_raise_when_sending_fails(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    user = unique_user_fn_scoped
    notifier_id = notifier(user, "json://secret-token@ha.local/hook", name="Kitchen HA")

    def publish(self: ApprisePublisher, event: Any, notification_urls: list[str]) -> None:
        raise RuntimeError("json://secret-token@ha.local is down")

    monkeypatch.setattr(ApprisePublisher, "publish", publish)
    with caplog.at_level("WARNING"):
        sent = events.notify_inbox_rejections(
            UUID(user.group_id), UUID(user.household_id), [IngestRejectReason.duplicate]
        )

    assert sent is False
    assert "not added" in caplog.text and "RuntimeError" in caplog.text
    assert "'Kitchen HA'" in caplog.text and str(notifier_id) in caplog.text  # which notifier, never its URL
    assert "secret-token" not in caplog.text


# ==================================================================================================================
# Whether the household hears about its cards


def test_household_notifies(unique_user_fn_scoped: TestUser, h2_user: TestUser):
    user = unique_user_fn_scoped
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)

    def notifies() -> bool:
        with session_context() as session:
            return events.household_notifies(session, group_id, household_id)

    assert notifies() is False  # no notifier at all
    notifier(user, "json://ha.local/hook", ready=False)
    notifier(user, "json://disabled.local/hook", enabled=False)
    notifier(h2_user, "json://other-household.local/hook")
    assert notifies() is False  # not opted in, switched off, or another household's

    notifier(user, "pover://user@token")
    assert notifies() is True
