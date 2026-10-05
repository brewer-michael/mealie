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
send; `notified_at` is set once every notifier has it. The lease is renewed every third of it while the sends run
(`Heartbeat`), so a slow notifier never lets another process start the same attempt over; the lease starts when the
batch is claimed, however long the housekeeping run that reached it has been going. A notifier that failed, or
a process that died part way, is tried again by housekeeping once the lease has passed, skipping the notifiers that
already have it; after 5 attempts the batch is given up on (`notified_at` set, an error logged). A notifier gets it
twice only if the process dies between sending to it and recording that. The 24 hours count from the cards' last
activity, not the batch's creation: a batch read over days still notifies, while a batch whose cards were last written
over 24 hours ago (a restored backup's) never does, and housekeeping settles it so a later edit doesn't either.

**Its title follows what it says:** "Recipe cards ready" when a card is ready to review, else "Recipe cards not read",
or "Recipe cards waiting" when its only cards to tell of wait for a monthly limit. The event type is
`recipe_ingestion_ready` every time, so a Home Assistant automation matches them all.

**Cards that waited for a monthly limit.** A card that failed `limit_reached` is read again automatically later
(`runner/retries.py`), at the next reset or sooner once the limit is raised. Its batch's notification says it waits,
never that it failed ("2 cards are waiting for the monthly limit. They'll be read when it resets on Nov 1, or sooner if
it's raised.", after what the batch's other cards came to; `waiting_count`, apart from `failed_count`). Queueing it
again arms the batch's notification again for a "wave" (`arm_limit_wave`, in the queueing's transaction, once the card
was queued): `notified_at` and the attempt are cleared and the card's id is kept in `notify_delivered`
(`limit-wave:<job id>`, beside the delivery hashes). Once none of the batch's cards is still being read, the
notification goes out as above, at least once per notifier, counting only the wave's cards that were read ("2 cards
that waited for the monthly limit were read. 1 card is ready to review (1 failed)."), one per batch for the cards
queued together. When none of them was read (each failed `limit_reached` again, and waits on), nothing is sent. A claim
re-checks, in a statement of its own, that no card of the batch is being read: on PostgreSQL its conditional update
may have waited for a queueing's commit and checked the cards as they were before it.

**Counts and a link only:** no card names or text, since notifications leave the server. Logs name a notifier by its
id and name, never by its URL, which holds its secrets.

Apprise blocks: everything here runs in the caller's thread (a task thread, the dispatcher's thread limiter, or a
route's threadpool), never on the event loop, and no database transaction stays open while Apprise sends.
"""

import hashlib
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
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
from mealie.repos.repository_recipe_ingest import IngestRepos, naive_utc, utcnow
from mealie.schema.recipe_ingest import IngestErrorCode, IngestRejectReason, IngestStatus, RecipeIngestionJobCounts
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
from .intake import lock_household_intake

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
    """Cards that couldn't be read, not counting those waiting for a monthly limit"""
    waiting_count: int = 0
    """Cards waiting for a monthly limit: read again automatically once it resets or is raised"""
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


class Heartbeat:
    """
    Keeps a send's claim on an event while the send runs: `renew()` every `interval` seconds, on a daemon thread of its
    own (it opens its own sessions), from entering the block until it ends, or until `renew` answers False (the claim
    is no longer the sender's). A process that stops renews nothing, so the claim's lease runs out and housekeeping
    sends the event; one that is still sending keeps it, however long its notifiers take. A renewal that fails is
    logged and tried again at the next beat.
    """

    def __init__(self, renew: Callable[[], bool], interval: float, what: str) -> None:
        self._renew = renew
        self._interval = interval
        self._what = what
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ai-ingest-claim", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._renew():
                    return
            except Exception as e:
                logger.warning(f"{self._what}: couldn't renew its claim ({type(e).__name__}); tried again shortly")

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


# ==================================================================================================================
# The message


def reset_date(when: datetime, translator: Translator) -> str:
    """The day a monthly limit resets ('Nov 1'), from `auto_retry_at` (UTC), in the notification's language"""
    month = translator.t(f"recipe-ingest.months-short.{when.month}")
    return translator.t("recipe-ingest.notification-reset-date", month=month, day=when.day)


def ready_message(
    counts: RecipeIngestionJobCounts,
    translator: Translator,
    *,
    waited: int = 0,
    waiting: int = 0,
    reset: datetime | None = None,
) -> EventBusMessage:
    """
    'Recipe cards ready' / '10 cards are ready to review (2 need a look, 1 failed).', titled 'Recipe cards not read'
    when none is ready. `waited`: the cards read after waiting for a monthly limit (a wave), which the body says first:
    '2 cards that waited for the monthly limit were read. 1 card is ready to review (1 failed).' `waiting`: the cards
    waiting for a monthly limit, which aren't failed and are said last, read at `reset` at the latest: '… 2 cards are
    waiting for the monthly limit. They'll be read when it resets on Nov 1, or sooner if it's raised.'; with no card
    ready or failed, that's the whole body, titled 'Recipe cards waiting'.
    """
    waits = ""
    if waiting:
        date = reset_date(reset, translator) if reset is not None else ""
        waits = translator.t("recipe-ingest.notification-waiting", count=waiting, date=date)
        if not counts.ready and not counts.failed:
            return EventBusMessage(title=translator.t("recipe-ingest.notification-title-waiting"), body=waits)

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
    if waited:
        read = translator.t("recipe-ingest.notification-limit-wave", count=waited)
        body = translator.t("recipe-ingest.notification-limit-wave-body", waited=read, summary=body)
    if waits:
        body = translator.t("recipe-ingest.notification-waiting-body", summary=body, waiting=waits)
    title = "recipe-ingest.notification-title" if counts.ready else "recipe-ingest.notification-title-none-ready"
    return EventBusMessage(title=translator.t(title), body=body)


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


LIMIT_WAVE_PREFIX = "limit-wave:"
"""
A `notify_delivered` entry naming a card read again after waiting for a monthly limit, whose batch notifies again for
it (`arm_limit_wave`); the other entries are delivery hashes (`NotifierURL.delivery_key`), which never start so
"""


def _wave_entry(job_id: UUID) -> str:
    return f"{LIMIT_WAVE_PREFIX}{job_id}"


def _wave_ids(entries: Iterable[str]) -> set[UUID]:
    """The cards of a batch's pending wave, from its `notify_delivered`"""
    ids: set[UUID] = set()
    for entry in entries:
        if entry.startswith(LIMIT_WAVE_PREFIX):
            try:
                ids.add(UUID(entry.removeprefix(LIMIT_WAVE_PREFIX)))
            except ValueError:
                continue
    return ids


@dataclass
class _Lease:
    """
    One attempt's hold on a batch's notification: its number, and the lease's current start (`notify_claimed_at`),
    which the `Heartbeat` moves on. Every write of the attempt is fenced on both, so once the batch is armed again
    (`arm_limit_wave`, which clears them) the attempt writes nothing, even if a later one has its number.
    """

    batch_id: UUID
    attempt: int
    claimed_at: datetime
    lock: threading.Lock = field(default_factory=threading.Lock)
    """Held for each fenced write, so a renewal and a record never use the start the other is replacing"""

    def fence(self) -> list[sa.ColumnElement[bool]]:
        return [
            Batch.id == self.batch_id,
            Batch.notified_at.is_(None),
            Batch.notify_attempts == self.attempt,
            Batch.notify_claimed_at == self.claimed_at,
        ]


@dataclass(frozen=True)
class _Claim:
    lease: _Lease
    delivered: frozenset[str]
    """The notifiers earlier attempts reached (`NotifierURL.delivery_key`), and a pending wave's cards"""

    @property
    def attempt(self) -> int:
        """The batch's `notify_attempts` after this claim"""
        return self.lease.attempt


def _being_read(session: Session, batch_id: UUID) -> bool:
    """Whether a card of the batch is processing, as committed by now"""
    processing = sa.exists().where(Job.batch_id == batch_id, Job.status == IngestStatus.processing.value)
    return bool(session.execute(sa.select(processing)).scalar())


def _claim(session: Session, batch_id: UUID, now: datetime) -> _Claim | None:
    """
    Takes the batch's notification for `limits.NOTIFY_LEASE` seconds from now (or `now`, when later) if it's due at
    `now`, free and has attempts left, in one conditional update that no two processes can both win; this attempt, or
    None. A run of housekeeping may reach a batch long after its `now`: the lease still starts when it's claimed.
    """
    stmt = (
        sa.update(Batch)
        .where(*_claimable(batch_id, now))
        .values(notify_claimed_at=max(now, utcnow()), notify_attempts=Batch.notify_attempts + 1)
    )
    try:
        result = session.execute(stmt, execution_options={"synchronize_session": False})
        claim = None
        # PostgreSQL: an update that waited for the batch's row (a card being queued again arms the batch in the
        # queueing's transaction, `arm_limit_wave`) checked "no card processing" as the cards were when it began, before
        # that card was queued; checked again in a statement of its own, which sees it. The card's own finalize then
        # sends the notification, with it.
        if isinstance(result, CursorResult) and result.rowcount == 1 and not _being_read(session, batch_id):
            # read before the commit: until then the row is this transaction's
            attempt, claimed_at, delivered = session.execute(
                sa.select(Batch.notify_attempts, Batch.notify_claimed_at, Batch.notify_delivered).where(
                    Batch.id == batch_id
                )
            ).one()
            claim = _Claim(_Lease(batch_id, attempt, claimed_at), frozenset(delivered or ()))
        if claim is None:
            session.rollback()  # nothing claimed, or a card is being read after all: the claim is undone
        else:
            session.commit()
    except BaseException:
        session.rollback()
        raise
    return claim


def _fenced_update(session: Session, lease: _Lease, values: Mapping[str, Any]) -> bool:
    """One write of the attempt holding `lease`, if it still holds it; whether it did. Commits."""
    stmt = sa.update(Batch).where(*lease.fence()).values(**values)
    try:
        result = session.execute(stmt, execution_options={"synchronize_session": False})
        updated = isinstance(result, CursorResult) and result.rowcount == 1
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return updated


def _renew_claim(lease: _Lease) -> bool:
    """
    Moves the lease of the batch's notification on to now while the attempt still holds it (`Heartbeat`, from its own
    thread, with a session of its own); whether it did
    """
    with lease.lock, session_context() as session:
        renewed = max(utcnow(), naive_utc(lease.claimed_at) + timedelta(microseconds=1))
        if not _fenced_update(session, lease, {"notify_claimed_at": renewed}):
            return False
        lease.claimed_at = renewed
        return True


def _record(session: Session, lease: _Lease, delivered: Iterable[str], *, notified_at: datetime | None = None) -> bool:
    """
    Records the notifiers that have the batch's notification (and keeps a wave's cards) and, with `notified_at`, that
    it's done, if the attempt still holds `lease` (no later attempt took it over, nothing settled or armed it again);
    whether it did
    """
    values: dict[str, Any] = {"notify_delivered": sorted(delivered)}
    if notified_at is not None:
        values["notified_at"] = notified_at
    with lease.lock:
        return _fenced_update(session, lease, values)


ARM_TRIES = 10
"""How often `arm_limit_wave` reads the notification's state again when another process changed it meanwhile"""


def arm_limit_wave(session: Session, job_id: UUID) -> None:
    """
    Arms the notification of the batch of a card the automatic retry has just queued after it waited for a monthly
    limit (`runner/retries.py`), in the queueing's transaction, which the caller then commits, so the card is never
    queued without it, nor the batch armed for a card that wasn't queued. Under the household's intake lock
    (`intake.lock_household_intake`), so two processes arming one batch keep both cards; the batch's row is read
    locked (`FOR UPDATE` on PostgreSQL, where a claim being made meanwhile is waited for and seen; SQLite serializes
    writers already), and the write fenced on the notification's state as read, so it never lands between another
    process's claim and its record:
    - the batch's notification went out: it's due again once the card is read, for a wave of the cards that waited
      (`notified_at`, the claim and the attempts cleared, the card's id kept in `notify_delivered`);
    - a wave is pending: the card joins it, and one being sent starts over (its counts didn't have the card);
    - the batch's own notification is pending: it counts the card as it is once read, and one being sent (or waiting
      to be tried again) starts over, for the same reason.
    """
    job = session.execute(sa.select(Job.batch_id, Job.household_id).where(Job.id == job_id)).one_or_none()
    if job is None:
        return
    lock_household_intake(session, job.household_id)
    for _ in range(ARM_TRIES):
        state = session.execute(
            sa.select(Batch.notified_at, Batch.notify_claimed_at, Batch.notify_attempts, Batch.notify_delivered)
            .where(Batch.id == job.batch_id)
            .with_for_update()
        ).one_or_none()
        if state is None:
            return
        entries = set(state.notify_delivered or ())
        wave = {entry for entry in entries if entry.startswith(LIMIT_WAVE_PREFIX)}
        start_over = {"notified_at": None, "notify_claimed_at": None, "notify_attempts": 0}
        if state.notified_at is not None:
            values: dict[str, Any] = {**start_over, "notify_delivered": [_wave_entry(job_id)]}
        elif wave and state.notify_claimed_at is None:
            values = {"notify_delivered": sorted(entries | {_wave_entry(job_id)})}
        elif wave:
            values = {**start_over, "notify_delivered": sorted(wave | {_wave_entry(job_id)})}
        elif state.notify_claimed_at is not None:
            values = {**start_over, "notify_delivered": []}
        else:
            return  # its own notification is still to come
        fence = [
            Batch.id == job.batch_id,
            Batch.notify_attempts == state.notify_attempts,
            Batch.notified_at.is_(None) if state.notified_at is None else Batch.notified_at == state.notified_at,
            Batch.notify_claimed_at.is_(None)
            if state.notify_claimed_at is None
            else Batch.notify_claimed_at == state.notify_claimed_at,
        ]
        stmt = sa.update(Batch).where(*fence).values(**values)
        result = session.execute(stmt, execution_options={"synchronize_session": False})
        if isinstance(result, CursorResult) and result.rowcount == 1:
            return
    raise RuntimeError(f"Recipe card batch {job.batch_id}: its notification kept changing; not armed")


def _waiting_for_limit() -> list[sa.ColumnElement[bool]]:
    """
    A card waiting for a monthly limit: it failed `limit_reached` and is read again automatically (`auto_retry_at`),
    so no notification calls it failed
    """
    return [
        Job.status == IngestStatus.failed.value,
        Job.error_code == IngestErrorCode.limit_reached.value,
        Job.auto_retry_at.is_not(None),
    ]


@dataclass(frozen=True)
class _Tally:
    """What a notification says of some of a batch's cards"""

    counts: RecipeIngestionJobCounts
    """Ready, needing a look, and failed (not counting those waiting)"""
    read: list[UUID]
    """The cards that are ready or failed, in review order"""
    waiting: int = 0
    reset: datetime | None = None
    """When the waiting cards are read at the latest: the last of their `auto_retry_at`"""


def _tally(session: Session, batch: Batch, *only: sa.ColumnElement[bool]) -> tuple[_Tally, list[UUID]]:
    """The batch's cards (those where `only` holds) counted for a notification, and all of their ids in review order"""
    repos = IngestRepos(session, batch.group_id, batch.household_id)
    waits = sa.and_(*_waiting_for_limit())
    rows = session.execute(
        sa.select(Job.id, Job.status, Job.error_count, Job.warning_count, waits.label("waits"), Job.auto_retry_at)
        .where(Job.batch_id == batch.id, *only, *repos.jobs.scope)
        .order_by(Job.position, Job.created_at, Job.id)
    ).all()
    ready = [row for row in rows if row.status == IngestStatus.ready.value]
    failed = [row for row in rows if row.status == IngestStatus.failed.value and not row.waits]
    waiting = [naive_utc(row.auto_retry_at) for row in rows if row.waits]
    counts = RecipeIngestionJobCounts(
        ready=len(ready),
        needs_attention=sum(1 for row in ready if row.error_count or row.warning_count),
        failed=len(failed),
    )
    read = {row.id for row in [*ready, *failed]}
    tally = _Tally(counts, [row.id for row in rows if row.id in read], len(waiting), max(waiting, default=None))
    return tally, [row.id for row in rows]


def _event(session: Session, batch: Batch, tally: _Tally, job_ids: list[UUID], waited: int = 0) -> AIEvent:
    translator = translator_for(batch.locale)
    counts = tally.counts
    return AIEvent(
        message=ready_message(counts, translator, waited=waited, waiting=tally.waiting, reset=tally.reset),
        event_type=AIEventTypes.recipe_ingestion_ready,
        integration_id=INTERNAL_INTEGRATION_ID,
        document_data=EventIngestionReadyData(
            batch_id=batch.id,
            job_ids=job_ids,
            ready_count=counts.ready,
            needs_attention_count=counts.needs_attention,
            failed_count=counts.failed,
            waiting_count=tally.waiting,
            review_url=cards_url(group_slug(session, batch.group_id), batch.id),
        ),
    )


def _ready_event(session: Session, batch: Batch) -> AIEvent | None:
    """
    The batch's notification; None when it has nothing to look at (no ready, failed or waiting card). A card waiting
    for a monthly limit is said to wait, not to have failed; its batch notifies again once it's read (a wave).
    """
    tally, job_ids = _tally(session, batch)
    if not tally.read and not tally.waiting:
        return None
    return _event(session, batch, tally, job_ids)


def _wave_event(session: Session, batch: Batch, wave: set[UUID]) -> AIEvent | None:
    """
    The notification of a wave of the batch's cards that waited for a monthly limit, counting only those that were
    read (`job_ids` too); None when none of them was: each failed `limit_reached` again and waits on (for a wave of
    its own once read), or is gone
    """
    tally, _ = _tally(session, batch, Job.id.in_(wave))
    if not tally.read:
        return None
    return _event(session, batch, replace(tally, waiting=0, reset=None), tally.read, waited=len(tally.read))


def maybe_notify_batch(batch_id: UUID) -> bool:
    """
    Sends the batch's notification if it's due (sealed, not yet notified, none of its cards still processing, and one
    written in the last 24 hours) and no other process is sending it. Whether this call finished it: every notifier
    that opted in has it (or there's none). False when it wasn't due or was being sent elsewhere, when a notifier
    didn't get it (housekeeping tries again), and when the finished batch has nothing to look at (every card
    committed or discarded already, or a wave's cards are all still waiting). Blocking (Apprise).
    """
    return _notify_batch(batch_id, utcnow())


def _notify_batch(batch_id: UUID, now: datetime) -> bool:
    """`maybe_notify_batch` at `now`"""
    with session_context() as session:
        claim = _claim(session, batch_id, now)
        if claim is None:
            return False
        lease = claim.lease

        # anything raised from here leaves the claim: housekeeping tries again once its lease has passed
        batch = session.get(Batch, batch_id)
        if batch is None:
            return False
        wave = _wave_ids(claim.delivered)
        event = _wave_event(session, batch, wave) if wave else _ready_event(session, batch)
        if event is None:
            _record(session, lease, claim.delivered, notified_at=utcnow())
            return False

        listener = AIEventAppriseListener(batch.group_id, batch.household_id, session)
        targets = listener.targets(event)  # ends the session's transaction before anything is sent
        delivered = set(claim.delivered)
        missed: list[NotifierURL] = []
        renew = partial(_renew_claim, lease)
        with Heartbeat(renew, limits.NOTIFY_LEASE / 3, f"Recipe card batch {batch_id}'s ready notification"):
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
                if not _record(session, lease, delivered):
                    return False  # taken over (this attempt stalled past its lease), armed again, or settled

        if not missed:
            return _record(session, lease, delivered, notified_at=utcnow())
        if claim.attempt >= limits.NOTIFY_ATTEMPTS and _record(session, lease, delivered, notified_at=utcnow()):
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
    repos = IngestRepos(session, group_id, household_id)
    counts = repos.jobs.counts()
    waiting = session.execute(sa.select(sa.func.count(Job.id)).where(*_waiting_for_limit(), *repos.jobs.scope))
    waiting_count = waiting.scalar_one()  # counted apart from the failed ones, as a batch's notification counts them
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
            failed_count=counts.failed - waiting_count,
            waiting_count=waiting_count,
            review_url=cards_url(slug),
        ),
    )
    reason = AIEventAppriseListener(group_id, household_id, session).send(event, notifier)
    if reason is not None:
        logger.warning(f"Notifier {notifier.label} didn't get the test recipe card notification ({reason})")
    return reason is None
