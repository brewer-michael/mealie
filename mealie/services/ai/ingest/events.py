"""
"Recipe cards ready" notifications (docs/ai/PHASE2.md §8): one per finished batch, through the household's Apprise
notifiers that opted in, never through `EventBusService.dispatch` (its listeners only know upstream's event types).

**Once per batch, at most once.** `maybe_notify_batch` claims the batch's notification with one conditional
`UPDATE ... SET notified_at` (sealed, not yet notified, created in the last 24 hours, no card still processing) and
publishes only when that update matched the row. Two processes finishing the last two cards at the same moment can't
both win, and `notified_at` is written before anything is sent, so a crash loses a notification rather than doubling
it. A batch created more than 24 hours ago never notifies, so restoring a backup doesn't replay old notifications.

**Counts and a link only:** no card names or text, since notifications leave the server.

Apprise blocks: everything here runs in the caller's thread (a task thread, the dispatcher's thread limiter, or a
route's threadpool), never on the event loop, and no database transaction stays open while Apprise sends.
"""

from datetime import datetime, timedelta
from enum import Enum
from urllib.parse import parse_qsl, quote, unquote_plus, urlencode, urlsplit, urlunsplit
from uuid import UUID

import sqlalchemy as sa
from pydantic import UUID4
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from mealie.core.config import get_app_settings
from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.db.models.group import Group
from mealie.db.models.household.events import GroupEventNotifierModel
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.lang.providers import Translator
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.recipe_ingest import IngestStatus, RecipeIngestionJobCounts
from mealie.services.event_bus_service.event_bus_listeners import AppriseEventListener
from mealie.services.event_bus_service.event_types import (
    INTERNAL_INTEGRATION_ID,
    Event,
    EventBusMessage,
    EventDocumentDataBase,
    EventDocumentType,
    EventOperation,
)

from . import limits
from .batches import seal_idle_batches
from .i18n import translator_for

logger = get_logger(__name__)

Batch = RecipeIngestionBatch
Job = RecipeIngestionJob

TEST_INTEGRATION_ID = "test_event"
"""The integration id of a test notification, as upstream's notifier test sends it"""


class AIEventTypes(Enum):
    recipe_ingestion_ready = "recipe_ingestion_ready"


class AIEvent(Event):
    """A fork event. Only `AIEventAppriseListener` sends it: `EventBusService.dispatch` would raise on its type."""

    event_type: AIEventTypes  # type: ignore[assignment]


class EventIngestionReadyData(EventDocumentDataBase):
    document_type: EventDocumentType = EventDocumentType.generic
    operation: EventOperation = EventOperation.info
    batch_id: UUID4 | None
    """None only in a test notification"""
    job_ids: list[UUID4]
    """The batch's cards, in capture order"""
    ready_count: int
    needs_attention_count: int
    """Ready cards with an unresolved error or warning"""
    failed_count: int
    review_url: str
    """`BASE_URL/g/<group-slug>/recipes/cards/review?batch=<id>`: the batch's first card to review"""


class AIEventAppriseListener(AppriseEventListener):
    """
    Sends AI events to the household's enabled Apprise notifiers whose fork option for the event is on, with the
    event's data added to the URLs that take custom values (`json://`, `form://`, `xml://`) as upstream does.
    """

    def __init__(self, group_id: UUID4, household_id: UUID4, session: Session | None = None) -> None:
        super().__init__(group_id, household_id)
        self._session = session

    def get_subscribers(self, event: Event) -> list[str]:
        if not isinstance(event, AIEvent) or event.event_type is not AIEventTypes.recipe_ingestion_ready:
            return []

        with self.ensure_session() as session:
            notifier_ids = IngestRepos(
                session, self.group_id, self.household_id
            ).notifier_options.enabled_notifier_ids()
            urls = notifier_urls(session, self.group_id, self.household_id, notifier_ids)
            if session.in_transaction():
                session.commit()  # Apprise may take a while; no transaction stays open meanwhile

        return self.update_urls_with_event_data(urls, event)

    @staticmethod
    def update_urls_with_event_data(urls: list[str], event: Event) -> list[str]:
        """
        Upstream's, with the event's fields percent-encoded and the notifier's own query left as the user wrote it.
        Upstream's `urlencode` writes a space as `+`, which Apprise doesn't read back (it decodes `:key` values with
        `unquote`), so Home Assistant got a `document_data` with `+` between its JSON tokens, which `from_json` can't
        parse (§8). Re-encoding the whole query instead would turn a literal `+` in the user's own values into a space.
        """
        updated: list[str] = []
        for url, merged in zip(urls, AppriseEventListener.update_urls_with_event_data(urls, event), strict=True):
            if not AppriseEventListener.is_custom_url(url):
                updated.append(url)
                continue

            # upstream wrote the event's fields with `quote_plus`, so reading `+` as a space here is right
            fields = [(k, v) for k, v in parse_qsl(urlsplit(merged).query, keep_blank_values=True) if k in _EVENT_KEYS]
            parts = urlsplit(url)
            own = [
                part
                for part in parts.query.split("&")
                if part and unquote_plus(part.split("=", 1)[0]) not in _EVENT_KEYS
            ]
            query = "&".join([*own, urlencode(fields, quote_via=quote)])
            updated.append(urlunsplit(parts._replace(query=query)))

        return updated


_EVENT_KEYS = {":event_type", ":integration_id", ":document_data", ":event_id", ":timestamp"}
"""The fields upstream's `AppriseEventListener.update_urls_with_event_data` adds to a custom (form, json, xml) URL"""


def notifier_urls(session: Session, group_id: UUID, household_id: UUID, notifier_ids: list[UUID]) -> list[str]:
    """The Apprise URLs of the household's notifiers among `notifier_ids`"""
    if not notifier_ids:
        return []

    stmt = (
        sa.select(GroupEventNotifierModel.apprise_url)
        .where(
            GroupEventNotifierModel.id.in_(notifier_ids),
            GroupEventNotifierModel.group_id == group_id,
            GroupEventNotifierModel.household_id == household_id,
        )
        .order_by(GroupEventNotifierModel.id)
    )
    return [url for url in session.execute(stmt).scalars() if url]


# ==================================================================================================================
# The message


def ready_message(counts: RecipeIngestionJobCounts, translator: Translator) -> EventBusMessage:
    """'Recipe cards ready' / '10 cards are ready to review (2 need a look, 1 failed).'"""
    ready = translator.t("recipe-ingest.notification-ready", count=counts.ready)
    details: list[str] = []
    if counts.needs_attention:
        details.append(translator.t("recipe-ingest.notification-needs-attention", count=counts.needs_attention))
    if counts.failed:
        details.append(translator.t("recipe-ingest.notification-failed", count=counts.failed))

    if details:
        separator = translator.t("recipe-ingest.notification-details-separator")
        body = translator.t(
            "recipe-ingest.notification-body-with-details", ready=ready, details=separator.join(details)
        )
    else:
        body = translator.t("recipe-ingest.notification-body", ready=ready)
    return EventBusMessage(title=translator.t("recipe-ingest.notification-title"), body=body)


def cards_url(slug: str, batch_id: UUID | None = None) -> str:
    """The review start of a batch (`…/recipes/cards/review?batch=<id>`), or the cards page without one"""
    base = f"{get_app_settings().BASE_URL.rstrip('/')}/g/{quote(slug, safe='')}/recipes/cards"
    return f"{base}/review?batch={batch_id}" if batch_id else base


def group_slug(session: Session, group_id: UUID) -> str:
    slug = session.execute(sa.select(Group.slug).where(Group.id == group_id)).scalar_one_or_none()
    return slug or str(group_id)


# ==================================================================================================================
# Sending


def _due(batch_id: UUID | None, now: datetime) -> list[sa.ColumnElement[bool]]:
    """
    A batch whose notification is due: sealed, not yet notified, created in the last 24 hours and with no card still
    processing. Every check is in the `WHERE` of the one statement that claims it (§3.3).
    """
    processing = sa.exists().where(Job.batch_id == Batch.id, Job.status == IngestStatus.processing.value)
    conditions = [
        Batch.sealed_at.is_not(None),
        Batch.notified_at.is_(None),
        Batch.created_at > now - timedelta(seconds=limits.NOTIFY_CUTOFF),
        ~processing,
    ]
    if batch_id is not None:
        conditions.insert(0, Batch.id == batch_id)
    return conditions


def _claim(session: Session, batch_id: UUID, now: datetime) -> bool:
    """Marks the batch notified if its notification is due; whether this call did (and so must send it)"""
    stmt = sa.update(Batch).where(*_due(batch_id, now)).values(notified_at=now)
    try:
        result = session.execute(stmt, execution_options={"synchronize_session": False})
        claimed = isinstance(result, CursorResult) and result.rowcount == 1
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return claimed


def _ready_event(session: Session, batch: Batch) -> AIEvent | None:
    """The batch's notification; None when it has nothing to look at (no ready or failed card)"""
    repos = IngestRepos(session, batch.group_id, batch.household_id)
    counts = repos.batches.counts(batch.id)
    if not counts.ready and not counts.failed:
        return None

    job_ids = session.execute(
        sa.select(Job.id)
        .where(Job.batch_id == batch.id, *repos.jobs.scope)
        .order_by(Job.position, Job.created_at, Job.id)
    ).scalars()
    return AIEvent(
        message=ready_message(counts, translator_for(batch.locale)),
        event_type=AIEventTypes.recipe_ingestion_ready,
        integration_id=INTERNAL_INTEGRATION_ID,
        document_data=EventIngestionReadyData(
            batch_id=batch.id,
            job_ids=list(job_ids),
            ready_count=counts.ready,
            needs_attention_count=counts.needs_attention,
            failed_count=counts.failed,
            review_url=cards_url(group_slug(session, batch.group_id), batch.id),
        ),
    )


def maybe_notify_batch(batch_id: UUID) -> bool:
    """
    Sends the batch's notification if it's due: sealed, not yet notified, created in the last 24 hours, and none of
    its cards still processing. The conditional `notified_at` update decides, so it's sent at most once across
    processes. Whether this call sent it (to every notifier that opted in, if any): False when it wasn't due, or when
    the finished batch has nothing to look at (every card committed or discarded already). Blocking (Apprise).
    """
    with session_context() as session:
        if not _claim(session, batch_id, utcnow()):
            return False

        batch = session.get(Batch, batch_id)
        event = _ready_event(session, batch) if batch is not None else None
        if batch is None or event is None:
            return False

        listener = AIEventAppriseListener(batch.group_id, batch.household_id, session)
        urls = listener.get_subscribers(event)  # ends the session's transaction before anything is sent

    if urls:
        listener.publish_to_subscribers(event, urls)
    return True


def due_batches(session: Session, now: datetime) -> list[UUID]:
    """Every household's batches whose notification is due, oldest first"""
    stmt = sa.select(Batch.id).where(*_due(None, now)).order_by(Batch.created_at, Batch.id)
    due = list(session.execute(stmt).scalars())
    if session.in_transaction():
        session.commit()
    return due


def housekeeping(now: datetime) -> None:
    """Seals idle batches and sends the notifications that became due (the dispatcher, every minute)"""
    with session_context() as session:
        seal_idle_batches(session, now)
        due = due_batches(session, now)

    for batch_id in due:
        try:
            maybe_notify_batch(batch_id)
        except Exception as e:
            # one notifier's failure doesn't hold up the other batches; its message could hold an Apprise URL
            logger.error(f"Recipe card batch {batch_id}: its ready notification failed ({type(e).__qualname__})")


def send_test_notification(
    session: Session, group_id: UUID, household_id: UUID, apprise_url: str, translator: Translator
) -> None:
    """
    A test of the "recipe cards ready" event through one notifier, whatever its toggle says: the same event type and
    data shape (with no batch, and the household's current counts), so a Home Assistant automation can be tried out.
    """
    counts = IngestRepos(session, group_id, household_id).jobs.counts()
    slug = group_slug(session, group_id)
    if session.in_transaction():
        session.commit()  # nothing held open while Apprise sends

    event = AIEvent(
        message=EventBusMessage(
            title=translator.t("recipe-ingest.notification-test-title"),
            body=translator.t("recipe-ingest.notification-test-body", count=counts.ready),
        ),
        event_type=AIEventTypes.recipe_ingestion_ready,
        integration_id=TEST_INTEGRATION_ID,
        document_data=EventIngestionReadyData(
            batch_id=None,
            job_ids=[],
            ready_count=counts.ready,
            needs_attention_count=counts.needs_attention,
            failed_count=counts.failed,
            review_url=cards_url(slug),
        ),
    )
    listener = AIEventAppriseListener(group_id, household_id, session)
    listener.publish_to_subscribers(event, listener.update_urls_with_event_data([apprise_url], event))
