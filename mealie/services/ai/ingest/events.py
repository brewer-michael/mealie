"""
"Recipe cards ready" notifications (docs/ai/PHASE2.md §8): one per finished batch, through the household's Apprise
notifiers that opted in, never through `EventBusService.dispatch` (its listeners only know upstream's event types).
The same notifiers get "Recipe cards not added" (`recipe_ingestion_rejected`): one per inbox scan burst that refused
files.

**Once per batch, at least once per notifier.** A batch's notification is due once it's sealed, none of its cards is
still processing and one was written in the last 24 hours. `maybe_notify_batch` claims it with one conditional
`UPDATE` that takes a 5-minute lease (`notify_claimed_at`) and counts the attempt (`notify_attempts`), so two
processes finishing the last two cards at the same moment can't both send it. It then sends to each notifier on its
own, checks Apprise's answer, and records each notifier that got it (a hash in `notify_delivered`) before the next
send; `notified_at` is set once every notifier has it. A notifier that failed, or a process that died part way, is
tried again by housekeeping once the lease has passed, skipping the notifiers that already have it; after 5 attempts
the batch is given up on (`notified_at` set, an error logged). A notifier gets it twice only if the process dies
between sending to it and recording that. The 24 hours count from the cards' last activity, not the batch's creation:
a batch read over days still notifies, while a batch whose cards were last written over 24 hours ago (a restored
backup's) never does, and housekeeping settles it so a later edit doesn't either.

**Counts and a link only:** no card names or text, since notifications leave the server. Logs name a notifier by its
id and name, never by its URL, which holds its secrets.

Apprise blocks: everything here runs in the caller's thread (a task thread, the dispatcher's thread limiter, or a
route's threadpool), never on the event loop, and no database transaction stays open while Apprise sends.
"""

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, NamedTuple
from urllib.parse import quote
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
from mealie.schema.recipe_ingest import IngestRejectReason, IngestStatus, RecipeIngestionJobCounts
from mealie.services.event_bus_service.event_bus_listeners import AppriseEventListener
from mealie.services.event_bus_service.event_types import (
    INTERNAL_INTEGRATION_ID,
    Event,
    EventBusMessage,
    EventDocumentDataBase,
    EventDocumentType,
    EventOperation,
)
from mealie.services.event_bus_service.publisher import ApprisePublisher

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
    recipe_ingestion_rejected = "recipe_ingestion_rejected"
    """Files in the household's inbox folder that weren't added"""


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


OTHER_REASON = "other"
"""The reason code of a refusal that has none of its own (a link, an empty folder)"""


class EventIngestionRejectedData(EventDocumentDataBase):
    document_type: EventDocumentType = EventDocumentType.generic
    operation: EventOperation = EventOperation.info
    count: int
    """Files (or card folders) of the scan burst that weren't added"""
    reasons: dict[str, int]
    """How many for each reason: an `IngestRejectReason` value, or `other`"""
    review_url: str
    """`BASE_URL/g/<group-slug>/recipes/cards`: the cards page"""


class NotifierURL(NamedTuple):
    """A household notifier that the fork's events go to"""

    notifier_id: UUID
    name: str
    url: str
    """Its Apprise URL as the user wrote it. It holds the notifier's secrets, so it's never logged."""

    @property
    def label(self) -> str:
        """How logs name it"""
        return f"{self.name!r} ({self.notifier_id})"

    @property
    def delivery_key(self) -> str:
        """
        What a batch's `notify_delivered` records once the notifier has its notification: a hash, so the row holds no
        URL. A notifier whose URL was changed since counts as a new one.
        """
        return hashlib.sha256(f"apprise:{self.notifier_id}:{self.url}".encode()).hexdigest()


class _AnsweringApprise:
    """
    An `ApprisePublisher`'s `Apprise` object that remembers what `notify` answered: upstream's `publish` drops it, so a
    notifier that was down, or a URL Apprise couldn't read, went unnoticed
    """

    def __init__(self, apprise: Any) -> None:
        self._apprise = apprise
        self.answer: bool | None = None
        """True once every URL took the notification; False when one didn't; None when there was no URL to send to"""

    def add(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self._apprise.add(*args, **kwargs))

    def notify(self, *args: Any, **kwargs: Any) -> bool | None:
        self.answer = self._apprise.notify(*args, **kwargs)
        return self.answer


def deliver(event: Event, url: str) -> bool:
    """
    Sends `event` to one Apprise URL (with the event's data already in it) through upstream's `ApprisePublisher`;
    whether it was delivered: Apprise could read the URL and its service took the notification. Raises what Apprise
    raises. Blocking.
    """
    publisher = ApprisePublisher()
    answering = _AnsweringApprise(publisher.apprise)
    publisher.apprise = answering
    publisher.publish(event, [url])  # upstream's: adds the URL, tagged with the event's id, then notifies that tag
    return answering.answer is True


class AIEventAppriseListener(AppriseEventListener):
    """
    Sends AI events to the household's enabled Apprise notifiers whose fork option for the event is on, one notifier
    at a time so each one's delivery is known. Upstream's `update_urls_with_event_data` adds the event's data to the
    URLs that take custom values (`json://`, `form://`, `xml://`).
    """

    def __init__(self, group_id: UUID4, household_id: UUID4, session: Session | None = None) -> None:
        super().__init__(group_id, household_id)
        self._session = session

    def get_subscribers(self, event: Event) -> list[str]:
        """Upstream's interface: the notifiers' URLs, with the event's data"""
        return self.update_urls_with_event_data([target.url for target in self.targets(event)], event)

    def targets(self, event: Event) -> list[NotifierURL]:
        """The notifiers that get `event`. With a session of the caller's, its transaction is ended."""
        # both events go to the notifiers with the recipe cards option on
        if not isinstance(event, AIEvent):
            return []

        with self.ensure_session() as session:
            targets = household_targets(session, self.group_id, self.household_id)
            if session.in_transaction():
                session.commit()  # Apprise may take a while; no transaction stays open meanwhile
        return targets

    def send(self, event: Event, target: NotifierURL) -> str | None:
        """
        Sends `event` to one notifier, with the event's data in its URL. None once it's delivered, else why not (never
        the URL). Never raises. Blocking (Apprise).
        """
        [url] = self.update_urls_with_event_data([target.url], event)
        try:
            return None if deliver(event, url) else "Apprise couldn't deliver it"
        except Exception as e:
            return type(e).__qualname__  # its message could hold the URL


def household_targets(session: Session, group_id: UUID, household_id: UUID) -> list[NotifierURL]:
    """The household's enabled notifiers, with a URL, that send its recipe card events"""
    notifier_ids = IngestRepos(session, group_id, household_id).notifier_options.enabled_notifier_ids()
    return notifier_urls(session, group_id, household_id, notifier_ids)


def household_notifies(session: Session, group_id: UUID, household_id: UUID) -> bool:
    """
    Whether the household hears about its recipe cards: it has an enabled notifier, with a URL, that sends "recipe
    cards ready"
    """
    return bool(household_targets(session, group_id, household_id))


def notifier_urls(session: Session, group_id: UUID, household_id: UUID, notifier_ids: list[UUID]) -> list[NotifierURL]:
    """The household's notifiers among `notifier_ids` that have a URL"""
    if not notifier_ids:
        return []

    stmt = (
        sa.select(GroupEventNotifierModel.id, GroupEventNotifierModel.name, GroupEventNotifierModel.apprise_url)
        .where(
            GroupEventNotifierModel.id.in_(notifier_ids),
            GroupEventNotifierModel.group_id == group_id,
            GroupEventNotifierModel.household_id == household_id,
        )
        .order_by(GroupEventNotifierModel.id)
    )
    return [NotifierURL(row.id, row.name, row.apprise_url) for row in session.execute(stmt) if row.apprise_url]


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


def rejected_message(reasons: Mapping[str, int], translator: Translator) -> EventBusMessage:
    """
    'Recipe cards not added' / '3 recipe cards from the inbox weren't added (2 already scanned, 1 too large). They're
    in the inbox's failed folder.' The most frequent reason first; a code without words of its own is another reason.
    """
    words = "recipe-ingest.notification-rejected"
    details: list[str] = []
    other = 0
    for code, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
        key = f"{words}.reasons.{code}"
        text = translator.t(key, count=count) if code != OTHER_REASON else key
        if text == key:
            other += count
        else:
            details.append(text)
    if other:
        details.append(translator.t(f"{words}.reasons.{OTHER_REASON}", count=other))

    separator = translator.t("recipe-ingest.notification-details-separator")
    body = translator.t(f"{words}.body", count=sum(reasons.values()), details=separator.join(details))
    return EventBusMessage(title=translator.t(f"{words}.title"), body=body)


def cards_url(slug: str, batch_id: UUID | None = None) -> str:
    """The review start of a batch (`…/recipes/cards/review?batch=<id>`), or the cards page without one"""
    base = f"{get_app_settings().BASE_URL.rstrip('/')}/g/{quote(slug, safe='')}/recipes/cards"
    return f"{base}/review?batch={batch_id}" if batch_id else base


def group_slug(session: Session, group_id: UUID) -> str:
    slug = session.execute(sa.select(Group.slug).where(Group.id == group_id)).scalar_one_or_none()
    return slug or str(group_id)


# ==================================================================================================================
# Sending


def _finished(batch_id: UUID | None) -> list[sa.ColumnElement[bool]]:
    """A batch that's sealed, not yet notified, and with no card still processing"""
    processing = sa.exists().where(Job.batch_id == Batch.id, Job.status == IngestStatus.processing.value)
    conditions = [Batch.sealed_at.is_not(None), Batch.notified_at.is_(None), ~processing]
    if batch_id is not None:
        conditions.insert(0, Batch.id == batch_id)
    return conditions


def _recently_active(now: datetime) -> sa.ColumnElement[bool]:
    """
    A card of the batch was written in the last 24 hours: read, failed, retried or edited. A batch read over days
    (a slow reader, an outage, rate limits) is still new when its last card finishes, while a restored backup's
    batch keeps its cards' old times.
    """
    cutoff = now - timedelta(seconds=limits.NOTIFY_CUTOFF)
    return sa.exists().where(Job.batch_id == Batch.id, sa.func.coalesce(Job.update_at, Job.created_at) > cutoff)


def _due(batch_id: UUID | None, now: datetime) -> list[sa.ColumnElement[bool]]:
    """
    A batch whose notification is due: finished (sealed, not yet notified, no card still processing) and with a card
    written in the last 24 hours. Every check is in the `WHERE` of the one statement that claims it (§3.3).
    """
    return [*_finished(batch_id), _recently_active(now)]


def _lease_free(now: datetime) -> sa.ColumnElement[bool]:
    """No process holds the batch's notification: it was never claimed, or its lease has passed"""
    expired = now - timedelta(seconds=limits.NOTIFY_LEASE)
    return sa.or_(Batch.notify_claimed_at.is_(None), Batch.notify_claimed_at <= expired)


def _claimable(batch_id: UUID | None, now: datetime) -> list[sa.ColumnElement[bool]]:
    """Due, held by no process, and with attempts left"""
    return [*_due(batch_id, now), _lease_free(now), Batch.notify_attempts < limits.NOTIFY_ATTEMPTS]


@dataclass(frozen=True)
class _Claim:
    attempt: int
    """The batch's `notify_attempts` after this claim: only this attempt's writes match it"""
    delivered: frozenset[str]
    """The notifiers earlier attempts reached (`NotifierURL.delivery_key`)"""


def _claim(session: Session, batch_id: UUID, now: datetime) -> _Claim | None:
    """
    Takes the batch's notification for `limits.NOTIFY_LEASE` seconds if it's due, free and has attempts left, in one
    conditional update that no two processes can both win; this attempt, or None
    """
    stmt = (
        sa.update(Batch)
        .where(*_claimable(batch_id, now))
        .values(notify_claimed_at=now, notify_attempts=Batch.notify_attempts + 1)
    )
    try:
        result = session.execute(stmt, execution_options={"synchronize_session": False})
        claim = None
        if isinstance(result, CursorResult) and result.rowcount == 1:
            # read before the commit: until then the row is this transaction's
            attempt, delivered = session.execute(
                sa.select(Batch.notify_attempts, Batch.notify_delivered).where(Batch.id == batch_id)
            ).one()
            claim = _Claim(attempt, frozenset(delivered or ()))
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return claim


def _record(
    session: Session, batch_id: UUID, attempt: int, delivered: Iterable[str], *, notified_at: datetime | None = None
) -> bool:
    """
    Records the notifiers that have the batch's notification and, with `notified_at`, that it's done, if `attempt`
    still holds it (no later attempt took it over, nothing settled it); whether it did
    """
    values: dict[str, Any] = {"notify_delivered": sorted(delivered)}
    if notified_at is not None:
        values["notified_at"] = notified_at
    stmt = (
        sa.update(Batch)
        .where(Batch.id == batch_id, Batch.notified_at.is_(None), Batch.notify_attempts == attempt)
        .values(**values)
    )
    try:
        result = session.execute(stmt, execution_options={"synchronize_session": False})
        recorded = isinstance(result, CursorResult) and result.rowcount == 1
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return recorded


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
    Sends the batch's notification if it's due (sealed, not yet notified, none of its cards still processing, and one
    written in the last 24 hours) and no other process is sending it. Whether this call finished it: every notifier
    that opted in has it (or there's none). False when it wasn't due or was being sent elsewhere, when a notifier
    didn't get it (housekeeping tries again), and when the finished batch has nothing to look at (every card
    committed or discarded already). Blocking (Apprise).
    """
    return _notify_batch(batch_id, utcnow())


def _notify_batch(batch_id: UUID, now: datetime) -> bool:
    """`maybe_notify_batch` at `now`"""
    with session_context() as session:
        claim = _claim(session, batch_id, now)
        if claim is None:
            return False

        # anything raised from here leaves the claim: housekeeping tries again once its lease has passed
        batch = session.get(Batch, batch_id)
        if batch is None:
            return False
        event = _ready_event(session, batch)
        if event is None:
            _record(session, batch_id, claim.attempt, claim.delivered, notified_at=utcnow())
            return False

        listener = AIEventAppriseListener(batch.group_id, batch.household_id, session)
        targets = listener.targets(event)  # ends the session's transaction before anything is sent
        delivered = set(claim.delivered)
        missed: list[NotifierURL] = []
        for target in targets:
            if target.delivery_key in delivered:
                continue  # an earlier attempt reached it
            if (reason := listener.send(event, target)) is not None:
                last = claim.attempt >= limits.NOTIFY_ATTEMPTS
                again = "" if last else f", tried again in {limits.NOTIFY_LEASE // 60} minutes"
                logger.warning(
                    f"Recipe card batch {batch_id}: notifier {target.label} didn't get the ready notification "
                    f"({reason}; attempt {claim.attempt} of {limits.NOTIFY_ATTEMPTS}{again})"
                )
                missed.append(target)
                continue
            delivered.add(target.delivery_key)
            if not _record(session, batch_id, claim.attempt, delivered):
                return False  # this attempt outlived its lease and another took over, or the batch was settled

        if not missed:
            return _record(session, batch_id, claim.attempt, delivered, notified_at=utcnow())
        if claim.attempt >= limits.NOTIFY_ATTEMPTS and _record(
            session, batch_id, claim.attempt, delivered, notified_at=utcnow()
        ):
            logger.error(
                f"Recipe card batch {batch_id}: gave up on the ready notification after {claim.attempt} attempts; "
                f"never delivered to {', '.join(target.label for target in missed)}"
            )
        return False


def due_batches(session: Session, now: datetime) -> list[UUID]:
    """Every household's batches whose notification is due and free to send, oldest first"""
    stmt = sa.select(Batch.id).where(*_claimable(None, now)).order_by(Batch.created_at, Batch.id)
    due = list(session.execute(stmt).scalars())
    if session.in_transaction():
        session.commit()
    return due


def settle_stale_batches(session: Session, now: datetime) -> int:
    """
    Marks finished batches whose cards were last written over 24 hours ago (a restored backup's, or one that was
    never due) as notified without sending anything, so a later edit of one of their cards doesn't make them due.
    How many it settled.
    """
    stmt = sa.update(Batch).where(*_finished(None), ~_recently_active(now)).values(notified_at=now)
    try:
        result = session.execute(stmt, execution_options={"synchronize_session": False})
        settled = result.rowcount if isinstance(result, CursorResult) else 0
        session.commit()
    except BaseException:
        session.rollback()
        raise
    if settled:
        logger.info(f"Recipe card batches finished with no card activity in 24 hours, not notified: {settled}")
    return settled


def give_up_batches(session: Session, now: datetime) -> list[UUID]:
    """
    Marks as notified the batches whose last attempt didn't finish (its process died part way) once its lease has
    passed: no attempt is left to finish them. The batches it gave up on.
    """
    conditions = [Batch.notified_at.is_(None), Batch.notify_attempts >= limits.NOTIFY_ATTEMPTS, _lease_free(now)]
    given_up: list[UUID] = []
    try:
        for batch_id in list(session.execute(sa.select(Batch.id).where(*conditions)).scalars()):
            stmt = sa.update(Batch).where(Batch.id == batch_id, *conditions).values(notified_at=now)
            result = session.execute(stmt, execution_options={"synchronize_session": False})
            if isinstance(result, CursorResult) and result.rowcount == 1:
                given_up.append(batch_id)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    for batch_id in given_up:
        logger.error(
            f"Recipe card batch {batch_id}: gave up on the ready notification after {limits.NOTIFY_ATTEMPTS} attempts; "
            "the last one never finished"
        )
    return given_up


def housekeeping(now: datetime) -> None:
    """
    Seals idle batches, settles stale ones, gives up on those out of attempts, and sends the notifications that
    became due or whose last attempt's lease has passed (the dispatcher, every minute)
    """
    with session_context() as session:
        seal_idle_batches(session, now)
        settle_stale_batches(session, now)
        give_up_batches(session, now)
        due = due_batches(session, now)

    for batch_id in due:
        try:
            _notify_batch(batch_id, now)
        except Exception as e:
            # one batch's failure doesn't hold up the others; its message could hold an Apprise URL
            logger.error(
                f"Recipe card batch {batch_id}: its ready notification failed ({type(e).__qualname__}); "
                "tried again once its lease has passed"
            )


def notify_inbox_rejections(
    group_id: UUID,
    household_id: UUID,
    reasons: Iterable[IngestRejectReason | str | None],
    *,
    locale: str | None = None,
    session: Session | None = None,
) -> bool:
    """
    Tells the household that files of one inbox scan burst weren't added: one "Recipe cards not added" event for the
    burst, with counts by reason (one entry of `reasons` per refused file or card folder; None for a refusal without a
    code), in `locale` (the language the inbox's cards take; en-US for a text it doesn't have). Counts and a link only:
    no file names, which can be card text too.

    Sent to each of the household's notifiers with the recipe cards option on; whether any of them got it. Never
    raises: the refused files are in `failed/` with a note either way, and a notifier that didn't get it is logged
    (by id and name, never its URL). With `session`, its transaction is ended before anything is sent. Blocking
    (Apprise).
    """
    counts = Counter(str(reason) if reason else OTHER_REASON for reason in reasons)
    if not counts:
        return False

    try:
        with session_context() if session is None else nullcontext(session) as db:
            slug = group_slug(db, group_id)
            event = AIEvent(
                message=rejected_message(counts, translator_for(locale)),
                event_type=AIEventTypes.recipe_ingestion_rejected,
                integration_id=INTERNAL_INTEGRATION_ID,
                document_data=EventIngestionRejectedData(
                    count=counts.total(), reasons=dict(counts), review_url=cards_url(slug)
                ),
            )
            listener = AIEventAppriseListener(group_id, household_id, db)
            targets = listener.targets(event)  # ends the session's transaction before anything is sent

        delivered = False
        for target in targets:
            if (reason := listener.send(event, target)) is None:
                delivered = True
            else:
                logger.warning(
                    f"Recipe card inbox of household {household_id}: notifier {target.label} didn't get the "
                    f"'not added' notification ({reason})"
                )
        return delivered
    except Exception as e:
        if session is not None and session.in_transaction():
            session.rollback()
        # its message could hold an Apprise URL
        logger.warning(
            f"Recipe card inbox of household {household_id}: "
            f"the 'not added' notification failed ({type(e).__qualname__})"
        )
        return False


def send_test_notification(
    session: Session, group_id: UUID, household_id: UUID, notifier: NotifierURL, translator: Translator
) -> bool:
    """
    A test of the "recipe cards ready" event through one notifier, whatever its toggle says: the same event type and
    data shape (with no batch, and the household's current counts), so a Home Assistant automation can be tried out.
    Whether it was delivered; a failure is logged (by the notifier's id and name, never its URL). Blocking (Apprise).
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
    reason = AIEventAppriseListener(group_id, household_id, session).send(event, notifier)
    if reason is not None:
        logger.warning(f"Notifier {notifier.label} didn't get the test recipe card notification ({reason})")
    return reason is None
