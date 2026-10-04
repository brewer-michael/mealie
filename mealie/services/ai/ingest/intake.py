"""
Intake (docs/ai/PHASE2.md §2): turning one card's uploaded files into a job. Each file gives its pages (a multi-page
TIFF or a PDF several, a PDF's rendered in a child process before the write lock); inside the ingest write lock they
are normalized into the new job's directory, then one transaction checks for a duplicate, touches the batch (unsealed
only) and inserts the job with its extraction queued; the dispatcher is woken. The uploaded bytes never reach
`DATA_DIR`. Used by the upload route and the inbox.

**One household's intakes take turns.** The transaction starts with the household's intake lock
(`lock_household_intake`): a transaction-level advisory lock on PostgreSQL, the database's write lock on SQLite (plus
a lock per household in this process, so its own threads queue there rather than in SQLite's busy wait). So the
batch choice, the duplicate check, the position and the insert can't interleave with another upload or inbox card
of the household, in any worker process: two cards sent at once share one batch, and the same card sent twice at
once is one job and one `duplicate`. The batch touch then holds the batch's row (§1.4): a waiting seal re-checks its
`WHERE` after the insert.

**Failures** remove the job's directory, still under the write lock, so nothing is left in `DATA_DIR`: a rejected
image, a duplicate, a lost inbox claim or a database error. A pause is found before the directory exists.

Public interface:
- `IntakePage`, `IntakeCard`, `IntakeOptions`: what an upload or the inbox hands over.
- `IntakeAccepted`, `IntakeRejected` (`IntakeOutcome`): what became of the card.
- `IntakeService(session, group_id, household_id)`: `ingest(card, options, *, confirm=None)` (blocking, takes one of
  the process's intake slots and the write lock; raises `IngestPaused`, `NoEntryFound` for an unknown batch,
  `ClaimLost`) and `ingest_async(...)`, which waits for a slot on the event loop and runs `ingest` in a worker thread.
- `in_intake_slot(work)`: other memory-heavy upload work (a JSON body's decoding) under the same slots, the inbox's
  included.
- `ClaimLost`: the inbox's claimed file moved away before the insert (another scanner retried it).
- `lock_household_intake(session, household_id)`: the household's intake lock, held until the transaction ends.
- `ReadingReadiness` and `reading_readiness(session, group_id, household_id)`: whether the group can read cards, with
  local providers only or at all, its own local-only setting, its processing jobs (the upload's checks 3 and 4,
  and the inbox's), whether the monthly token limits stop a card being read now (the capture page's warning), and
  which optional parts of the read they skip (`limited_features`).
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
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core.root_logger import get_logger
from mealie.db.models.recipe_ingest import RecipeIngestionBatch
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.group.ai_providers import AIProviderSlot
from mealie.schema.recipe_ingest import (
    IngestLimitedFeature,
    IngestRejectReason,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
)
from mealie.services import ocr
from mealie.services.ai.errors import AIProviderLimitReachedError, AIProviderLocalOnlyError
from mealie.services.ai.policy import ai_call_policy

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


_household_locks: dict[UUID, threading.Lock] = {}
"""This process's intake lock per household, taken before the database's (`lock_household_intake`)"""
_household_locks_guard = threading.Lock()


class ClaimLost(Exception):
    """The inbox's claimed file was no longer at its path before the insert: another scanner is retrying it"""


def _household_lock(household_id: UUID) -> threading.Lock:
    """This process's intake lock for one household"""
    with _household_locks_guard:
        return _household_locks.setdefault(household_id, threading.Lock())


def _advisory_key(household_id: UUID) -> int:
    """The household's PostgreSQL advisory lock key: a signed 64-bit number from a hash of its id"""
    digest = hashlib.sha256(b"ai-ingest-intake:" + household_id.bytes).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_household_intake(session: Session, household_id: UUID) -> None:
    """
    Takes the household's intake lock for the session's transaction (it's released when the transaction ends), waiting
    for another worker process's intake of the same household to finish. PostgreSQL: `pg_advisory_xact_lock`. SQLite:
    a write statement that changes nothing but takes the database's write lock, which a second writer waits for (its
    busy timeout); every later read in the transaction then sees the other intake's committed rows.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": _advisory_key(household_id)})
        return
    batch = RecipeIngestionBatch.__table__
    session.connection().execute(sa.update(batch).where(sa.false()).values(id=batch.c.id))


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
    Runs an upload's other memory-heavy blocking work (decoding a JSON body's images) in a worker thread, under the
    same bound as intake: once one of the process's intake slots is free (the upload waits on the event loop until
    then), and holding one of the slots every caller of `ingest` takes, the inbox's scan included
    """

    def run() -> T:
        with _intake_slots:
            return work()

    return await anyio.to_thread.run_sync(run, limiter=_intake_limiter)


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
            return self.ingest(card, options, confirm=confirm)  # takes one of `_intake_slots` itself

        return await anyio.to_thread.run_sync(run, limiter=_intake_limiter)

    def ingest(
        self, card: IntakeCard, options: IntakeOptions, *, confirm: Callable[[], bool] | None = None
    ) -> IntakeOutcome:
        """
        Turns one card into a job, holding one of the process's `INTAKE_CONCURRENCY` intake slots (waiting for one),
        and the ingest write lock from its directory's creation through the insert. Each file gives its pages first
        (`images.expand_document`: a multi-page TIFF or a PDF fills several, at most `MAX_PAGES_PER_CARD` in all);
        then each page is normalized into `pages/<n>/`, and one transaction takes the household's intake lock,
        chooses and touches the batch (choosing again if it was sealed meanwhile), checks for a duplicate (unless
        allowed), calls `confirm` (the inbox checks that its claimed file is still there) and inserts the job,
        `processing` with its extraction queued. The dispatcher is woken.

        A rejected file or a duplicate is an `IntakeRejected` (naming the file), and leaves nothing on disk. Raises
        `IngestPaused` (nothing written) while a restore pauses ingestion, `NoEntryFound` for an unknown batch and
        `ClaimLost` when `confirm` says no. Blocking: call it from a worker thread.
        """
        if not card.pages:
            raise ValueError("A card needs at least one page")
        if len(card.pages) > limits.MAX_PAGES_PER_CARD:
            extra = card.pages[limits.MAX_PAGES_PER_CARD]
            return IntakeRejected(
                extra.index, images.sanitize_filename(extra.filename), IngestRejectReason.too_many_pages
            )

        job_id = uuid4()
        accepted = False
        # the slot first: a restore waiting for the write lock never waits for a card that's waiting for a slot
        with _intake_slots:
            # a PDF's pages are rendered before the write lock, which a restore may be waiting for
            expanded = self._expand(card)
            if isinstance(expanded, IntakeRejected):
                return expanded
            try:
                with storage.ingest_write():
                    try:
                        storage.create_job_dir(self.group_id, job_id, len(expanded))
                        outcome = self._normalize_and_insert(job_id, card, expanded, options, confirm)
                        accepted = isinstance(outcome, IntakeAccepted)
                    finally:
                        if not accepted:
                            self._remove_job_dir(job_id)
            finally:
                images.close_pages(page for _, page in expanded)

        if accepted:
            _wake_dispatcher()
        return outcome

    @staticmethod
    def _expand(card: IntakeCard) -> list[tuple[IntakePage, images.DocumentPage]] | IntakeRejected:
        """
        The card's pages in order, each uploaded file's own (`images.expand_document`: a multi-page TIFF or a PDF
        gives several), with the file each came from; or the rejection of the first file that can't be used, or that
        takes the card over `MAX_PAGES_PER_CARD` pages
        """
        expanded: list[tuple[IntakePage, images.DocumentPage]] = []
        try:
            for upload in card.pages:
                expanded += [(upload, page) for page in images.expand_document(upload.file)]
                if len(expanded) > limits.MAX_PAGES_PER_CARD:
                    raise images.PageRejected(IngestRejectReason.too_many_pages)
        except images.PageRejected as e:
            images.close_pages(page for _, page in expanded)
            return IntakeRejected(upload.index, images.sanitize_filename(upload.filename), e.reason)
        except BaseException:
            images.close_pages(page for _, page in expanded)
            raise
        return expanded

    def _remove_job_dir(self, job_id: UUID) -> None:
        try:
            storage.remove_job_dir(self.group_id, job_id)
        except OSError:
            # the daily purge removes directories without a row
            logger.exception(f"Couldn't remove the directory of recipe card job {job_id} after intake")

    def _normalize_and_insert(
        self,
        job_id: UUID,
        card: IntakeCard,
        expanded: list[tuple[IntakePage, images.DocumentPage]],
        options: IntakeOptions,
        confirm: Callable[[], bool] | None,
    ) -> IntakeOutcome:
        metas: list[PageMeta] = []
        for number, (upload, page) in enumerate(expanded):
            try:
                meta = images.normalize_document_page(
                    page,
                    storage.page_dir(self.group_id, job_id, number),
                    number,
                    original_filename=images.page_filename(upload.filename, page.number),
                )
            except images.PageRejected as e:
                return IntakeRejected(upload.index, images.sanitize_filename(upload.filename), e.reason)
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
        """
        The batch choice, the duplicate check, the batch touch and the job insert, in one transaction that holds the
        household's intake lock (this process's first, then the database's)
        """
        with _household_lock(self.household_id):
            return self._insert_locked(job_id, card, pages, options, confirm)

    def _insert_locked(
        self,
        job_id: UUID,
        card: IntakeCard,
        pages: list[PageMeta],
        options: IntakeOptions,
        confirm: Callable[[], bool] | None,
    ) -> IntakeOutcome:
        session = self.session
        repos = self.repos
        if session.in_transaction():
            session.commit()  # start from a fresh transaction (and a fresh snapshot)

        digest = source_sha256(pages)
        try:
            # first in the transaction: waits for another intake of the household to commit, so everything read
            # below (the open batch, a duplicate, the next position) includes its rows
            lock_household_intake(session, self.household_id)
            now = utcnow()
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
    limited_features: tuple[IngestLimitedFeature, ...] = ()
    """
    Cards are read, but an optional part of reading them is skipped because, under the group's policy, every provider
    it needs is over its monthly token limit: tag, category and tool `suggestions` (the fast slot, when the group has
    any to suggest) and the `cross_read` (the image slot, when the group reads every card twice and OCR does the main
    read). Empty whenever `limit_reached`, which stops the whole read.
    """
    user_processing: int | None = None
    """
    The uploader's `processing` cards, every household's of the group, for the optional per-user cap
    (`AI_INGEST_MAX_PROCESSING_PER_USER`); None unless asked for (`user_id`)
    """


def _slot_usable(service: OpenAIService, slot: AIProviderSlot, over_limit: set[AIProviderSlot] | None = None) -> bool:
    """
    Whether `slot` has a provider a card may use under the current policy. A slot whose providers are all over their
    monthly limit counts as usable (a card read later fails `limit_reached` if it still is) and is added to
    `over_limit`. Under "local only" that's about the local providers: the policy drops the others first.
    """
    from mealie.services.openai import OpenAINotEnabledException

    try:
        return bool(service.runtime.candidates(slot))
    except AIProviderLimitReachedError:
        if over_limit is not None:
            over_limit.add(slot)
        return True
    except AIProviderLocalOnlyError, OpenAINotEnabledException:
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


def _limited_features(
    session: Session, group_id: UUID, service: OpenAIService, *, local_only: bool, over_limit: set[AIProviderSlot]
) -> tuple[IngestLimitedFeature, ...]:
    """
    The optional parts of a card's read that its providers' monthly limits skip (`ReadingReadiness.limited_features`),
    when the main read itself still works. Asked of the group's options only when a slot is over its limit.
    """
    from .pipeline import options_for_group  # imported here: heavy, and only a group over a limit needs it

    limited: list[IngestLimitedFeature] = []
    fast_over: set[AIProviderSlot] = set()
    with ai_call_policy(local_only=local_only):
        _slot_usable(service, AIProviderSlot.fast, fast_over)  # without its own providers, the default slot's
    image_over = AIProviderSlot.image in over_limit
    if not (fast_over or image_over):
        return ()

    options = options_for_group(session, group_id)
    if fast_over and options.suggest_organizers:
        limited.append(IngestLimitedFeature.suggestions)
    if image_over and options.cross_read:
        limited.append(IngestLimitedFeature.cross_read)  # OCR reads the card; the second reading needs the image slot
    return tuple(limited)


def reading_readiness(
    session: Session, group_id: UUID, household_id: UUID, user_id: UUID | None = None
) -> ReadingReadiness:
    """
    Whether the group can read cards at all and with local providers only, its local-only setting and its processing
    jobs (and `user_id`'s, when given). Blocking (provider settings, address lookups for "local", counts): call it from
    a worker thread. A monthly token limit doesn't count as "can't read": the card fails `limit_reached` when it's
    read, if it still applies, and `limit_reached` says so beforehand; `limited_features` names the optional parts it
    skips.
    """
    from mealie.services.openai import OpenAIService

    ingest_repos = IngestRepos(session, group_id, household_id)
    group_local_only = ingest_repos.settings.get().local_only
    processing = ingest_repos.processing_jobs_in_group()
    user_processing = ingest_repos.jobs.count_processing_by_user(user_id) if user_id is not None else None

    service = OpenAIService(get_repositories(session, group_id=group_id, household_id=household_id))
    # the limits are tallied under the group's own policy, from the same lookups
    over_limit: set[AIProviderSlot] = set()
    can_read = _can_read(service, local_only=False, over_limit=None if group_local_only else over_limit)
    local_ready = can_read and _can_read(service, local_only=True, over_limit=over_limit if group_local_only else None)
    readable = local_ready if group_local_only else can_read
    limit_reached = readable and _limit_reached(over_limit)
    limited = (
        _limited_features(session, group_id, service, local_only=group_local_only, over_limit=over_limit)
        if readable and not limit_reached
        else ()
    )
    if session.in_transaction():
        session.commit()  # no transaction stays open while the body streams in

    return ReadingReadiness(
        can_read=can_read,
        local_ready=local_ready,
        group_local_only=group_local_only,
        processing=processing,
        limit_reached=limit_reached,
        limited_features=limited,
        user_processing=user_processing,
    )
