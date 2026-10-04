"""
"Recipe cards ready" notifications (docs/ai/PHASE2.md §8, §18 Events): only notifiers that opted in, the Apprise URL
params, one notification when two cards finish together, auto-seal, failed-only batches, the 24-hour cutoff, counts
and a link only, and never through `EventBusService.dispatch`.
"""

import json
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.household.group_events import GroupEventNotifierSave
from mealie.schema.recipe_ingest import IngestSource, IngestStatus
from mealie.services.ai.ingest import events, limits
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
    thread: threading.Thread

    @property
    def data(self) -> events.EventIngestionReadyData:
        assert isinstance(self.event.document_data, events.EventIngestionReadyData)
        return self.event.document_data


@pytest.fixture()
def published(monkeypatch: pytest.MonkeyPatch) -> list[Published]:
    """What Apprise was asked to send (nothing is sent); `EventBusService.dispatch` must never be used"""
    sent: list[Published] = []

    def publish(self: ApprisePublisher, event: Any, notification_urls: list[str]) -> None:
        sent.append(Published(event, list(notification_urls), threading.current_thread()))

    def dispatch(self: EventBusService, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("AI events never go through EventBusService.dispatch")

    monkeypatch.setattr(ApprisePublisher, "publish", publish)
    monkeypatch.setattr(EventBusService, "dispatch", dispatch)
    return sent


def for_batch(sent: list[Published], batch_id: UUID) -> list[Published]:
    """Only this batch's notifications (housekeeping also finds other tests' due batches)"""
    return [p for p in sent if p.data.batch_id == batch_id]


def notifier(user: TestUser, url: str, *, ready: bool = True, enabled: bool = True) -> UUID:
    saved = user.repos.group_event_notifier.create(
        GroupEventNotifierSave(
            name=random_string(),
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
    idle: timedelta | None = None,
    source: IngestSource = IngestSource.app,
    locale: str | None = "en-US",
) -> UUID:
    """
    A batch created `age` ago with one job per card: a status (`ready`, `failed`, `processing`, `committed`), or
    `ready!` for a ready card with something to check. Every card carries `CARD_TITLE`.
    """
    now = utcnow()
    with session_context() as session:
        repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
        batch_id = repos.batches.create(source=source, created_by=user.user_id, locale=locale, now=now - age)
        for position, card in enumerate(cards):
            needs_attention = card.endswith("!")
            repos.jobs.create(
                {
                    "batch_id": batch_id,
                    "position": position,
                    "source": source.value,
                    "status": IngestStatus(card.rstrip("!")).value,
                    "title": CARD_TITLE,
                    "source_sha256": uuid4().hex * 2,
                    "warning_count": 1 if needs_attention else 0,
                }
            )

        values: dict[str, Any] = {}
        if sealed:
            values["sealed_at"] = now
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


def set_status(batch_id: UUID, status: IngestStatus) -> None:
    with session_context() as session:
        session.execute(
            sa.update(RecipeIngestionJob).where(RecipeIngestionJob.batch_id == batch_id).values(status=status.value)
        )
        session.commit()


def group_slug(api_client: TestClient, user: TestUser) -> str:
    return api_client.get(api_routes.groups_self, headers=user.token).json()["slug"]


def custom_params(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


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
    ("cards", "body"),
    [
        (["ready"], "1 card is ready to review."),
        (["ready", "ready", "ready!"], "3 cards are ready to review (1 needs a look)."),
        (["ready!", "ready!", "failed", "failed"], "2 cards are ready to review (2 need a look, 2 failed)."),
        # a batch whose every card failed still says so
        (["failed", "failed"], "No cards are ready to review (2 failed)."),
    ],
)
def test_the_message_counts(unique_user_fn_scoped: TestUser, published: list[Published], cards: list[str], body: str):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    batch_id = make_batch(unique_user_fn_scoped, *cards)

    assert events.maybe_notify_batch(batch_id) is True
    [sent] = published
    assert sent.event.message.body == body
    assert sent.data.failed_count == cards.count("failed")


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
    assert events.maybe_notify_batch(batch_id) is False  # at most once
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


def test_batches_created_over_24_hours_ago_never_notify(unique_user_fn_scoped: TestUser, published: list[Published]):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    old = make_batch(unique_user_fn_scoped, "ready", age=timedelta(seconds=limits.NOTIFY_CUTOFF + 3600))
    recent = make_batch(unique_user_fn_scoped, "ready", age=timedelta(seconds=limits.NOTIFY_CUTOFF - 3600))

    assert events.maybe_notify_batch(old) is False
    events.housekeeping(utcnow())

    assert batch_row(old)["notified_at"] is None
    assert for_batch(published, old) == []
    assert len(for_batch(published, recent)) == 1


def test_a_batch_with_nothing_to_look_at_isnt_sent(unique_user_fn_scoped: TestUser, published: list[Published]):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    reviewed = make_batch(unique_user_fn_scoped, "committed", "committed")
    empty = make_batch(unique_user_fn_scoped)

    assert events.maybe_notify_batch(reviewed) is False
    assert events.maybe_notify_batch(empty) is False
    assert published == []
    # both are finished: housekeeping doesn't look at them again
    assert batch_row(reviewed)["notified_at"] is not None
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
    unique_user_fn_scoped: TestUser, published: list[Published], monkeypatch: pytest.MonkeyPatch
):
    notifier(unique_user_fn_scoped, "json://ha.local/hook")
    first = make_batch(unique_user_fn_scoped, "ready")
    second = make_batch(unique_user_fn_scoped, "ready")
    send = ApprisePublisher.publish

    def publish(self: ApprisePublisher, event: Any, notification_urls: list[str]) -> None:
        if event.document_data.batch_id == first:
            raise RuntimeError("json://secret@ha.local is down")
        send(self, event, notification_urls)

    monkeypatch.setattr(ApprisePublisher, "publish", publish)
    events.housekeeping(utcnow())

    assert batch_row(first)["notified_at"] is not None  # at most once: it isn't retried
    assert len(for_batch(published, second)) == 1


def test_apprise_reads_the_event_data_and_the_notifiers_own_fields_back():
    """
    Home Assistant parses `document_data` with `from_json` (§8): its JSON must reach Apprise intact, and the user's own
    `:field` and `+header` values (a literal `+` included) must arrive as written
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
