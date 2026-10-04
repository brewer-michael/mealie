"""
Intake (docs/ai/PHASE2.md §2): turning one card's uploaded images into a job, inside the ingest write lock. Pages are
normalized into the new job's directory, then one transaction checks for a duplicate, touches the batch (unsealed
only) and inserts the job with its extraction queued; the dispatcher is woken. The uploaded bytes never reach
`DATA_DIR`. Used by the upload route and the inbox.

**Order inside the transaction:** the batch touch comes first. On SQLite it takes the database's write lock, so the
duplicate check, the position and the insert that follow can't interleave with another intake; on PostgreSQL it
holds the batch's row lock, which serializes cards sent to the same batch (a resend usually is) and makes a waiting
seal re-check its `WHERE` after the insert (§1.4).

**Failures** remove the job's directory, still under the write lock, so nothing is left in `DATA_DIR`: a rejected
image, a duplicate, a lost inbox claim or a database error. A pause is found before the directory exists.

Public interface:
- `IntakePage`, `IntakeCard`, `IntakeOptions`: what an upload or the inbox hands over.
- `IntakeAccepted`, `IntakeRejected` (`IntakeOutcome`): what became of the card.
- `IntakeService(session, group_id, household_id)`: `ingest(card, options, *, confirm=None)` (blocking, takes one of
  the process's intake slots and the write lock; raises `IngestPaused`, `NoEntryFound` for an unknown batch,
  `ClaimLost`) and `ingest_async(...)`, which waits for a slot on the event loop and runs `ingest` in a worker thread.
- `in_intake_slot(work)`: other memory-heavy upload work (a JSON body's decoding) under the same slots.
- `ClaimLost`: the inbox's claimed file moved away before the insert (another scanner retried it).
- `ReadingReadiness` and `reading_readiness(session, group_id, household_id)`: whether the group can read cards, with
  local providers only or at all, its own local-only setting, its processing jobs (the upload's checks 3 and 4,
  and the inbox's) and whether the monthly token limits stop a card being read now (the capture page's warning).
"""

import hashlib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, BinaryIO, Literal
from uuid import UUID, uuid4

import anyio
import anyio.to_thread
from sqlalchemy.orm import Session

from mealie.core.root_logger import get_logger
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.schema.recipe_ingest import (
    IngestRejectReason,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
)
from mealie.services import ocr
from mealie.services.ai.errors import AIProviderLimitReachedError, AIProviderLocalOnlyError
from mealie.services.ai.local import is_local_provider
from mealie.services.ai.policy import ai_call_policy, current_policy
from mealie.services.ai.routing import AIProviderRouter

from . import batches, images, limits, storage

if TYPE_CHECKING:
    from mealie.services.openai import OpenAIService

logger = get_logger(__name__)

SOURCE_NAME_MAX = 255
BATCH_ATTEMPTS = 3
"""How often an insert looks for another batch when the one it picked was sealed meanwhile"""

_intake_limiter = anyio.CapacityLimiter(limits.INTAKE_CONCURRENCY)
"""At most this many uploads are normalized at once in this process; further uploads wait on the event loop"""
_intake_slots = threading.BoundedSemaphore(limits.INTAKE_CONCURRENCY)
"""The same bound for every caller of `ingest`, the inbox's scan included: one large photo takes hundreds of MB"""


class ClaimLost(Exception):
    """The inbox's claimed file was no longer at its path before the insert: another scanner is retrying it"""


@dataclass
class IntakePage:
    """One uploaded image: an open, seekable binary file that's never reopened by path"""

    file: BinaryIO
    filename: str | None = None
    """As uploaded, for display (sanitized when stored)"""
    index: int = 0
    """The image's position in the request, for a rejection"""


@dataclass
class IntakeCard:
    pages: list[IntakePage]
    """Front first"""
    source_name: str | None = None
    """Stored with a prefix holding a `/` (`upload/IMG_0007.HEIC`, `inbox/<group>/<household>/card.jpg`), which
    `uuid.UUID` never accepts, so a backup restore can't reformat it (F15)"""


@dataclass(frozen=True)
class IntakeOptions:
    source: IngestSource
    """`api` or `inbox`: the source a batch started for this card gets (an explicit batch keeps its own)"""
    batch_id: UUID | Literal["new"] | None = None
    created_by: UUID | None = None
    """The uploader; none for the inbox"""
    source_key: str | None = None
    """The inbox folder, which inbox batches auto-join on"""
    position: int | None = None
    """The app's capture index; else the card goes after the batch's last one"""
    local_only: bool = False
    """The upload's own request; the job is local-only with this or the group's setting as the insert reads it"""
    allow_duplicate: bool = False
    locale: str | None = None
    integration_id: str | None = None


@dataclass(frozen=True)
class IntakeAccepted:
    job_id: UUID
    batch_id: UUID
    page_count: int


@dataclass(frozen=True)
class IntakeRejected:
    index: int
    filename: str | None
    reason: IngestRejectReason
    duplicate_of: UUID | None = None


IntakeOutcome = IntakeAccepted | IntakeRejected


async def in_intake_slot[T](work: Callable[[], T]) -> T:
    """
    Runs an upload's memory-heavy blocking work (intake, or decoding a JSON body's images) in a worker thread once one
    of the process's intake slots is free; until then the upload waits on the event loop
    """
    return await anyio.to_thread.run_sync(work, limiter=_intake_limiter)


def source_name(prefix: str, name: str | None) -> str | None:
    """`<prefix>/<name>`, cut to the column's length; None without a name"""
    if not name:
        return None
    return f"{prefix}/{name}"[:SOURCE_NAME_MAX]


def source_sha256(pages: Sequence[PageMeta]) -> str:
    """The duplicate key: SHA-256 of the ordered pages' `raw_sha256`s, so a front sent again with its back is new"""
    return hashlib.sha256("".join(page.raw_sha256 for page in pages).encode("ascii")).hexdigest()


def _wake_dispatcher() -> None:
    # imported here: the dispatcher imports the inbox, which imports this module
    from .runner.dispatcher import dispatcher

    try:
        dispatcher.wake()
    except Exception:
        logger.exception("Couldn't wake the recipe card dispatcher; it will find the job on its next poll")


class IntakeService:
    """Creates recipe card jobs for one group and household"""

    def __init__(self, session: Session, group_id: UUID, household_id: UUID) -> None:
        self.session = session
        self.group_id = group_id
        self.household_id = household_id

    @property
    def repos(self) -> IngestRepos:
        return IngestRepos(self.session, self.group_id, self.household_id)

    async def ingest_async(
        self, card: IntakeCard, options: IntakeOptions, *, confirm: Callable[[], bool] | None = None
    ) -> IntakeOutcome:
        """
        `ingest` from async code: waits for one of the process's intake slots on the event loop, so waiting uploads
        hold no worker thread, then runs in a worker thread.
        """

        def run() -> IntakeOutcome:
            return self.ingest(card, options, confirm=confirm)

        return await in_intake_slot(run)

    def ingest(
        self, card: IntakeCard, options: IntakeOptions, *, confirm: Callable[[], bool] | None = None
    ) -> IntakeOutcome:
        """
        Turns one card into a job, holding the ingest write lock from its directory's creation through the insert,
        and one of the process's `INTAKE_CONCURRENCY` intake slots (waiting for one). Each image is normalized into
        `pages/<n>/`; then one transaction touches the batch (choosing another if it was sealed meanwhile), checks for
        a duplicate (unless allowed), calls `confirm` (the inbox checks that its claimed file is still there) and
        inserts the job, `processing` with its extraction queued. The dispatcher is woken.

        A rejected image or a duplicate is an `IntakeRejected`, and leaves nothing on disk. Raises `IngestPaused`
        (nothing written) while a restore pauses ingestion, `NoEntryFound` for an unknown batch and `ClaimLost` when
        `confirm` says no. Blocking: call it from a worker thread.
        """
        if not card.pages:
            raise ValueError("A card needs at least one page")
        if len(card.pages) > limits.MAX_PAGES_PER_CARD:
            extra = card.pages[limits.MAX_PAGES_PER_CARD]
            return IntakeRejected(
                extra.index, images.sanitize_filename(extra.filename), IngestRejectReason.too_many_pages
            )

        job_id = uuid4()
        # the slot first: a restore waiting for the write lock never waits for a card that's waiting for a slot
        with _intake_slots, storage.ingest_write():
            accepted = False
            try:
                storage.create_job_dir(self.group_id, job_id, len(card.pages))
                outcome = self._normalize_and_insert(job_id, card, options, confirm)
                accepted = isinstance(outcome, IntakeAccepted)
            finally:
                if not accepted:
                    self._remove_job_dir(job_id)

        if accepted:
            _wake_dispatcher()
        return outcome

    def _remove_job_dir(self, job_id: UUID) -> None:
        try:
            storage.remove_job_dir(self.group_id, job_id)
        except OSError:
            # the daily purge removes directories without a row
            logger.exception(f"Couldn't remove the directory of recipe card job {job_id} after intake")

    def _normalize_and_insert(
        self, job_id: UUID, card: IntakeCard, options: IntakeOptions, confirm: Callable[[], bool] | None
    ) -> IntakeOutcome:
        metas: list[PageMeta] = []
        for number, page in enumerate(card.pages):
            try:
                meta = images.normalize_page(
                    page.file,
                    storage.page_dir(self.group_id, job_id, number),
                    number,
                    original_filename=page.filename,
                )
            except images.PageRejected as e:
                return IntakeRejected(page.index, images.sanitize_filename(page.filename), e.reason)
            metas.append(meta)
        return self._insert(job_id, card, metas, options, confirm)

    def _insert(
        self,
        job_id: UUID,
        card: IntakeCard,
        pages: list[PageMeta],
        options: IntakeOptions,
        confirm: Callable[[], bool] | None,
    ) -> IntakeOutcome:
        """The duplicate check, the batch touch and the job insert, in one transaction"""
        session = self.session
        repos = self.repos
        if session.in_transaction():
            session.commit()  # start from a fresh transaction (and a fresh snapshot)

        now = utcnow()
        digest = source_sha256(pages)
        try:
            batch_id = self._join_batch(repos, options, now)

            if not options.allow_duplicate:
                duplicate = repos.jobs.find_duplicate(digest)
                if duplicate is not None:
                    session.rollback()
                    front = card.pages[0]
                    return IntakeRejected(
                        front.index,
                        images.sanitize_filename(front.filename),
                        IngestRejectReason.duplicate,
                        duplicate_of=duplicate,
                    )

            position = batches.next_position(repos, batch_id, options.position)
            # the group's setting as it is now: an upload read it before its body arrived, and a switch to local only
            # since then must still cover this card (it's never loosened later, §10)
            local_only = options.local_only or repos.settings.get().local_only
            if confirm is not None and not confirm():
                session.rollback()
                raise ClaimLost(f"The card for recipe card job {job_id} was taken by another scan")

            repos.jobs.create(
                {
                    "id": job_id,
                    "batch_id": batch_id,
                    "position": position,
                    "created_by": options.created_by,
                    "source": batches.batch_source(session, batch_id).value,
                    "source_name": card.source_name,
                    "integration_id": options.integration_id,
                    "locale": options.locale,
                    "local_only": local_only,
                    "status": IngestStatus.processing.value,
                    "task_kind": IngestTaskKind.extract.value,
                    "task_state": IngestTaskState.queued.value,
                    "task_priority": limits.PRIORITY_EXTRACT,
                    "pages": pages,
                    "source_sha256": digest,
                    "created_at": now,
                    "update_at": now,
                },
                commit=False,
            )
            session.commit()
        except BaseException:
            if session.in_transaction():
                session.rollback()
            raise

        return IntakeAccepted(job_id=job_id, batch_id=batch_id, page_count=len(pages))

    def _join_batch(self, repos: IngestRepos, options: IntakeOptions, now: datetime) -> UUID:
        """
        The batch the card goes into, touched in this transaction. When the chosen batch was sealed between choosing
        and touching, the choice is made again: an explicit batch now reads as sealed and is replaced, and an
        auto-joined one is no longer open.
        """
        requested = options.batch_id
        for _ in range(BATCH_ATTEMPTS):
            batch_id = batches.select_batch(
                repos,
                batch_id=requested,
                source=options.source,
                created_by=options.created_by,
                source_key=options.source_key,
                locale=options.locale,
                now=now,
            )
            if batches.touch(repos, batch_id, now):
                return batch_id
        raise RuntimeError("Recipe card batches kept being sealed while a card was added")


# ==================================================================================================================
# Can the group read cards?


@dataclass(frozen=True)
class ReadingReadiness:
    """What the upload's checks 3 and 4 (and the inbox) need to know about a group, read in one worker-thread call"""

    can_read: bool
    """A default provider, plus an image provider or OCR (upstream's rule), under no policy"""
    local_ready: bool
    """The same with local providers only"""
    group_local_only: bool
    """The group keeps every card on this server"""
    processing: int
    """The group's `processing` jobs, every household's"""
    limit_reached: bool = False
    """
    Cards can be read, but under the group's policy every provider the read needs is over its monthly token limit: a
    card read now fails `limit_reached`. Uploads are still accepted (the limit may reset before the card is read);
    the capture page warns.
    """


class _EveryProvider(AIProviderRouter):
    """A slot's providers in the router's order, whatever their monthly token limits"""

    def _within_limits(self, providers: list[AIProviderOut]) -> list[AIProviderOut]:
        return providers


def _slot_usable(service: OpenAIService, slot: AIProviderSlot, over_limit: set[AIProviderSlot] | None = None) -> bool:
    """
    Whether `slot` has a provider a card may use under the current policy. A slot whose providers are all over their
    monthly limit is added to `over_limit`.
    """
    from mealie.services.openai import OpenAINotEnabledException

    try:
        return bool(service.runtime.candidates(slot))
    except AIProviderLimitReachedError:
        if over_limit is not None:
            over_limit.add(slot)
        # set up, just over this month's limit: a card read later fails `limit_reached` if it still is. The router
        # checks the limits before the policy filters, so under "local only" that holds only if a provider is local.
        if not current_policy().local_only:
            return True
        primaries = {
            AIProviderSlot.default: service.default_provider,
            AIProviderSlot.image: service.image_provider,
            AIProviderSlot.audio: service.audio_provider,
        }
        return any(
            is_local_provider(provider) for provider in _EveryProvider(service.repos, primaries).candidates(slot)
        )
    except OpenAINotEnabledException, AIProviderLocalOnlyError:
        return False


def _can_read(service: OpenAIService, *, local_only: bool, over_limit: set[AIProviderSlot] | None = None) -> bool:
    with ai_call_policy(local_only=local_only):
        if not _slot_usable(service, AIProviderSlot.default, over_limit):
            return False
        return _slot_usable(service, AIProviderSlot.image, over_limit) or ocr.is_available()


def _limit_reached(over_limit: set[AIProviderSlot]) -> bool:
    """
    Whether a card read now fails `limit_reached`: the default slot builds every recipe, and the image slot reads the
    photo unless OCR can (the pipeline falls back to it)
    """
    if AIProviderSlot.default in over_limit:
        return True
    return AIProviderSlot.image in over_limit and not ocr.is_available()


def reading_readiness(session: Session, group_id: UUID, household_id: UUID) -> ReadingReadiness:
    """
    Whether the group can read cards at all and with local providers only, its local-only setting and its processing
    jobs. Blocking (provider settings, address lookups for "local", a count): call it from a worker thread. A monthly
    token limit doesn't count as "can't read": the card fails `limit_reached` when it's read, if it still applies, and
    `limit_reached` says so beforehand.
    """
    from mealie.services.openai import OpenAIService

    ingest_repos = IngestRepos(session, group_id, household_id)
    group_local_only = ingest_repos.settings.get().local_only
    processing = ingest_repos.processing_jobs_in_group()

    service = OpenAIService(get_repositories(session, group_id=group_id, household_id=household_id))
    # the limits are tallied under the group's own policy, from the same lookups
    over_limit: set[AIProviderSlot] = set()
    can_read = _can_read(service, local_only=False, over_limit=None if group_local_only else over_limit)
    local_ready = can_read and _can_read(service, local_only=True, over_limit=over_limit if group_local_only else None)
    if session.in_transaction():
        session.commit()  # no transaction stays open while the body streams in

    readable = local_ready if group_local_only else can_read
    return ReadingReadiness(
        can_read=can_read,
        local_ready=local_ready,
        group_local_only=group_local_only,
        processing=processing,
        limit_reached=readable and _limit_reached(over_limit),
    )
