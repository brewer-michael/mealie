"""
`IngestDispatcher` (docs/ai/PHASE2.md §3.2-§3.9): one per worker process, started by the ingest router's lifespan
(`APIRouter(lifespan=dispatcher.lifespan)`, so `app.py` needs no change), unless `AI_INGEST_ENABLED` or
`AI_INGEST_WORKER` is off. `AI_INGEST_WORKER` is off under `TESTING`, where tests call `run_once()`. Off otherwise, the
lifespan logs a warning: uploads are still accepted, and only a worker in another process reads them.

Its loop runs on the app's event loop, and **every database call goes through `anyio.to_thread.run_sync` with the
dispatcher's own `CapacityLimiter`**, so an upload burst can't starve heartbeats and no query blocks the loop. Each
tick runs these phases; each one survives its own errors, logging once and backing off (doubling up to
`PHASE_BACKOFF_MAX`), so a failed query never stops the dispatcher for the life of the process:

- **Presence** (every `DISPATCHER_SEEN_INTERVAL`, paused or not): the presence file's time, which tells the app a card
  reader runs (`storage.dispatcher_seen_at`).
- **Pause check:** while a backup restore's marker is set, nothing below runs (§3.9).
- **Heartbeat** (every `HEARTBEAT_INTERVAL`): renews this process's leases; a task whose token is gone (commit,
  discard, sweep) or that was asked to stop is cancelled through its own loop. One whose token a backup restore took
  (a restore ended after it was claimed) carries on instead: its result is kept for the card's next task
  (`results`), so the reading already paid for isn't asked for again.
- **Sweep** (every tick): expired leases of other processes' tasks are requeued or given up (`sweep.py`). Held back
  for two heartbeats after a pause, so every live process renews its leases first.
- **Claims** (every tick, and at once on `wake()`): `AI_INGEST_CONCURRENCY` task threads for any task plus
  `REREAD_SLOTS` that only re-reads use, so a reviewer's re-read starts at once while a batch is being read. Groups
  take turns (`IngestQueue.queued_ids`), and `AI_INGEST_GROUP_CONCURRENCY` caps one group's cards read at once across
  every process.
- **Housekeeping, stale commits and cards waiting for a monthly limit** (every `HOUSEKEEPING_INTERVAL`; `retries`),
  **the inbox** (every `AI_INGEST_INBOX_POLL_SECONDS`) and **the purge** (daily, first `PURGE_FIRST_DELAY` after start)
  run in the background, so a slow scan or notification never delays a claim or a heartbeat.

Each task runs in a **daemon thread with its own event loop** (`worker.run_task`), registered as (loop, task, token,
deadline): not a thread pool, whose threads are joined at exit and would hold a container stop behind a provider
call. A task past `TASK_DEADLINE` is cancelled (`timeout`); one stuck in synchronous code keeps its slot until it
returns, and is logged.

**Shutdown** (the lifespan's exit): stop claiming, cancel the running tasks, wait up to `SHUTDOWN_GRACE`, then release
every lease this dispatcher holds (`queued`, `attempts - 1`, so deploys don't use up retries), by its owner id rather
than by the tasks it knows of. A claim still in flight takes no further task once shutdown begins and is waited for
within that grace; whatever it claimed is given back rather than started, by the claim itself when it ends later.

Every time stored or compared is `utcnow()` from Python: never the database's own clock (§3.2).
"""

import asyncio
import functools
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import anyio
import anyio.to_thread
from sqlalchemy.orm import Session

from mealie.core.config import get_app_settings
from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import IngestQueue, utcnow

from .. import limits, storage
from ..settings import get_ingest_settings
from . import worker
from .classify import safe_trace
from .sweep import sweep_expired
from .worker import CancelReason

logger = get_logger(__name__)

FIRST_BACKOFF = 1.0
"""A failing phase's first retry delay, in seconds; it doubles up to `PHASE_BACKOFF_MAX`"""
STOP_WAIT = 3.0
"""How long shutdown waits for the loop to finish its current tick before cancelling it"""


class Phase(StrEnum):
    presence = "presence file"
    pause = "pause check"
    heartbeat = "heartbeat"
    sweep = "sweep"
    claim = "claims"
    housekeeping = "housekeeping"
    commits = "stale commits"
    retries = "limit retries"
    inbox = "inbox scan"
    purge = "purge"


@dataclass
class _PhaseState:
    next_due: float = 0.0
    backoff: float = 0.0
    failing: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    token: UUID
    reread_slot: bool
    """Taken by the re-read slot rather than one for any task"""


@dataclass
class ClaimBatch:
    claims: list[Claim] = field(default_factory=list)
    error: Exception | None = None
    """Claiming stopped at this error; the claims before it stand"""


def claim_tasks(
    session: Session, *, owner: str, general_slots: int, reread_slots: int, stop: threading.Event | None = None
) -> ClaimBatch:
    """
    Claims up to `reread_slots` queued re-reads (priority `PRIORITY_REREAD`) for the re-read slot, then up to
    `general_slots` queued tasks of any kind, by priority, then taking turns across groups, then age (§3.2, §3.4).
    Each claim is a conditional `UPDATE` with a new lease token, kept only when it changed one row, so two processes
    never claim the same task (nor more of a group's cards than `AI_INGEST_GROUP_CONCURRENCY`). Once `stop` is set
    (shutdown), no further task is claimed.
    """
    queue = IngestQueue(session)
    batch = ClaimBatch()

    def stopping() -> bool:
        return stop is not None and stop.is_set()

    def claim(slots: int, *, reread_slot: bool) -> None:
        if slots <= 0 or stopping():
            return
        taken = 0
        # a few more candidates than slots, so a claim lost to another process doesn't leave a slot idle a tick
        candidates = queue.queued_ids(utcnow(), slots * 2, max_priority=limits.PRIORITY_REREAD if reread_slot else None)
        for job_id in candidates:
            if taken >= slots or stopping():
                break
            token = uuid4()
            if queue.claim(job_id, token=token, owner=owner, now=utcnow()):
                batch.claims.append(Claim(job_id, token, reread_slot))
                taken += 1

    try:
        claim(reread_slots, reread_slot=True)
        claim(general_slots, reread_slot=False)
    except Exception as e:
        batch.error = e
    return batch


def release_leases(session: Session, leases: Iterable[tuple[UUID, UUID]]) -> int:
    """Gives back tasks this process claimed (shutdown): queued again without using up an attempt. How many."""
    queue = IngestQueue(session)
    released = 0
    for job_id, token in leases:
        try:
            if queue.release(job_id, token):
                released += 1
        except Exception as e:
            logger.error(f"Recipe card job {job_id}: couldn't release its task's lease:\n{safe_trace(e)}")
    return released


def claim_unless_stopping(
    session: Session, *, owner: str, general_slots: int, reread_slots: int, stop: threading.Event
) -> ClaimBatch:
    """
    `claim_tasks`, in the claim's worker thread. When shutdown began meanwhile, what it claimed is given back here,
    never started: shutdown may already have released this dispatcher's leases and stopped waiting for the claim (or
    the event loop may be gone), so only the claim itself can still release them.
    """
    batch = claim_tasks(session, owner=owner, general_slots=general_slots, reread_slots=reread_slots, stop=stop)
    if stop.is_set() and batch.claims:
        released = release_leases(session, [(claim.job_id, claim.token) for claim in batch.claims])
        logger.info(f"Recipe card dispatcher stopping: {released} task(s) claimed meanwhile queued again")
        batch.claims = []
    return batch


def _with_session[T](call: Callable[[Session], T]) -> T:
    with session_context() as session:
        return call(session)


class _RunningTask:
    """A task this process runs: its thread, its loop and asyncio task once started, its lease and deadline"""

    def __init__(self, claim: Claim, deadline: float) -> None:
        self.job_id = claim.job_id
        self.token = claim.token
        self.reread_slot = claim.reread_slot
        self.deadline = deadline
        self.claimed_at = time.time()
        """When it was claimed (a Unix time), to tell whether a restore took its lease since"""
        self.done = threading.Event()
        self.stuck_logged = False
        self.cut_off_logged = False
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._reason: CancelReason | None = None
        self._sent: CancelReason | None = None

    def reason(self) -> CancelReason | None:
        """Why the task was cancelled, if it was (read by the worker)"""
        return self._reason

    def attach(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task[Any]) -> None:
        """Called in the task's thread once its loop runs; a cancellation that came earlier is delivered now"""
        with self._lock:
            self._loop, self._task = loop, task
            pending = self._reason is not None and self._sent != self._reason
            if pending:
                self._sent = self._reason
        if pending:
            task.cancel()

    def cancel(self, reason: CancelReason) -> bool:
        """Cancels the task through its own loop. A shutdown or a vanished lease overrides an earlier reason."""
        with self._lock:
            if self._reason is None or reason in (CancelReason.shutdown, CancelReason.vanished):
                if self._reason != CancelReason.shutdown:
                    self._reason = reason
            if self._sent == self._reason:
                return False
            loop, task = self._loop, self._task
            if loop is None or task is None:
                return True  # its loop isn't running yet: `attach` delivers it, so it isn't marked sent
            self._sent = self._reason
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass  # its loop has already closed: the task is over
        return True


class IngestDispatcher:
    """Claims queued recipe card tasks from the job table and runs them (one instance per process: `dispatcher`)"""

    def __init__(self, *, concurrency: int | None = None, instance: str | None = None) -> None:
        self._concurrency = concurrency
        self._instance = instance or uuid4().hex[:8]
        self._tasks: dict[UUID, _RunningTask] = {}
        self._phases: dict[Phase, _PhaseState] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._limiter: anyio.CapacityLimiter | None = None
        self._runner: asyncio.Task[None] | None = None
        self._claiming: asyncio.Task[None] | None = None
        self._stop_claims = threading.Event()
        """Set when shutdown begins: a claim in flight takes no further task (a new one for each run)"""
        self._stopping = False
        self._paused = False
        self._last_paused_at: float | None = None
        self._reset_schedule()

    # ==========================================
    # Public interface

    @property
    def concurrency(self) -> int:
        """Task threads for any task (`AI_INGEST_CONCURRENCY`); `REREAD_SLOTS` more only re-reads use"""
        return self._concurrency or get_ingest_settings().CONCURRENCY

    @property
    def owner(self) -> str:
        """
        `host:pid:instance`, stored on each claim: for the logs, and shutdown releases by it. A long host name is cut,
        never the process and instance that make it unique.
        """
        unique = f":{os.getpid()}:{self._instance}"[-64:]
        return f"{socket.gethostname()[: 64 - len(unique)]}{unique}"

    @property
    def running(self) -> bool:
        """Whether the dispatcher's loop is running (the lifespan started it)"""
        return self._runner is not None and not self._runner.done()

    @property
    def running_tasks(self) -> list[UUID]:
        """The jobs whose task this process is running"""
        return [handle.job_id for handle in self._tasks.values() if not handle.done.is_set()]

    @asynccontextmanager
    async def lifespan(self, _app: Any) -> AsyncIterator[None]:
        """Runs the dispatcher while the app runs; on exit, stops claiming, cancels its tasks and releases leases"""
        settings = get_ingest_settings()
        if not (settings.ENABLED and settings.WORKER) or self.running:
            if settings.ENABLED and not settings.WORKER and not get_app_settings().TESTING:
                logger.warning(
                    "Recipe card ingestion: uploads are accepted but this process reads no cards (AI_INGEST_WORKER is "
                    "off); run the worker in another process"
                )
            yield
            return

        await self.start()
        try:
            yield
        finally:
            await self.stop()

    def wake(self) -> None:
        """Asks the dispatcher to look for work now (intake calls it after inserting a job). Safe from any thread."""
        loop, event = self._loop, self._wake_event
        if loop is None or event is None:
            return
        try:
            current: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            event.set()
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # that loop has closed: nothing is waiting

    async def run_once(self) -> None:
        """One pass of every dispatcher phase that's due, for tests and for the loop itself"""
        await self._tick(background=False)

    async def drain(self, timeout: float | None = None) -> bool:
        """Waits until none of this process's tasks is running (for tests and shutdown); whether that happened"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while any(not handle.done.is_set() for handle in self._tasks.values()):
            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.02)
        self._reap()
        return True

    async def start(self) -> None:
        """Starts the dispatcher's loop on the running event loop"""
        if self.running:
            return
        self._bind_loop()
        self._stopping = False
        self._paused = False
        self._last_paused_at = None
        self._reset_schedule()
        try:
            # Where `flock` doesn't work (some network filesystems) a restore can't wait for in-flight writes;
            # `flock_supported` logs that once (§3.9)
            await self._call(storage.flock_supported)
        except Exception as e:
            logger.warning(f"Couldn't check file locking for recipe card ingestion: {type(e).__name__}: {e}")
        try:
            # a restore that crashed (or whose container stopped) left its pause marker: ingestion carries on now
            await self._call(storage.clear_stale_pause)
        except Exception as e:
            logger.warning(f"Couldn't check the recipe card pause marker: {type(e).__name__}: {e}")
        self._runner = asyncio.get_running_loop().create_task(self._run(), name="ai-ingest-dispatcher")
        logger.info(
            f"Recipe card dispatcher started ({self.concurrency} task threads + {limits.REREAD_SLOTS} for re-reads)"
        )

    async def stop(self) -> None:
        """
        Stops claiming (waiting for a claim in flight), cancels this process's tasks, waits up to `SHUTDOWN_GRACE` in
        all, then releases every lease this dispatcher holds
        """
        self._bind_loop()
        self._stopping = True
        self._stop_claims.set()
        self.wake()
        runner, self._runner = self._runner, None
        if runner is not None:
            try:
                await asyncio.wait_for(asyncio.shield(runner), STOP_WAIT)
            except TimeoutError:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
            except Exception as e:
                logger.error(f"The recipe card dispatcher's loop failed:\n{safe_trace(e)}")
        grace_ends = time.monotonic() + limits.SHUTDOWN_GRACE
        await self._finish_claiming(limits.SHUTDOWN_GRACE)

        handles = list(self._tasks.values())
        for handle in handles:
            if not handle.done.is_set():
                handle.cancel(CancelReason.shutdown)
        if not await self.drain(max(grace_ends - time.monotonic(), 0.0)):
            stuck = [str(handle.job_id) for handle in handles if not handle.done.is_set()]
            logger.warning(f"Recipe card tasks still running at shutdown, their leases are released: {stuck}")

        # by owner, not by the tasks it knows of: a claim that landed after the claim phase gave up is covered too
        owner = self.owner
        try:
            released = await self._call(_with_session, lambda session: IngestQueue(session).release_owned(owner))
            if handles or released:
                logger.info(f"Recipe card dispatcher stopped; {released} task(s) queued again")
        except Exception as e:
            logger.error(f"Couldn't release the recipe card tasks' leases at shutdown:\n{safe_trace(e)}")
        self._tasks = {handle.token: handle for handle in handles if not handle.done.is_set()}

        background = [state.task for state in self._phases.values() if state.task is not None]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        for state in self._phases.values():
            state.task = None
        # a claim still in flight keeps the event it started with (set); the next run gets its own
        self._stop_claims = threading.Event()
        self._stopping = False

    # ==========================================
    # The loop

    def _bind_loop(self) -> None:
        """Event-loop-bound state belongs to the loop that runs the dispatcher (tests run several in turn)"""
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._wake_event = asyncio.Event()
        self._limiter = anyio.CapacityLimiter(limits.DISPATCHER_DB_THREADS)
        self._claiming = None
        for state in self._phases.values():
            state.task = None

    def _reset_schedule(self) -> None:
        now = time.monotonic()
        self._phases = {phase: _PhaseState(next_due=now) for phase in Phase}
        self._phases[Phase.purge].next_due = now + limits.PURGE_FIRST_DELAY

    async def _call[T](self, func: Callable[..., T], *args: Any, abandon_on_cancel: bool = False) -> T:
        """`func(*args)` in a worker thread, through the dispatcher's own thread limiter"""
        return await anyio.to_thread.run_sync(func, *args, limiter=self._limiter, abandon_on_cancel=abandon_on_cancel)

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick(background=True)
            except Exception as e:  # every phase catches its own; this is the last line
                logger.error(f"The recipe card dispatcher's tick failed:\n{safe_trace(e)}")
            if self._stopping:
                break
            assert self._wake_event is not None
            try:
                await asyncio.wait_for(self._wake_event.wait(), limits.POLL_INTERVAL)
            except TimeoutError:
                pass
            self._wake_event.clear()

    async def _tick(self, *, background: bool) -> None:
        self._bind_loop()
        self._reap()
        self._check_deadlines()

        await self._phase(Phase.presence, self._mark_seen, limits.DISPATCHER_SEEN_INTERVAL)
        paused = await self._phase(Phase.pause, self._check_pause, 0)
        if paused is None or paused:
            return

        await self._phase(Phase.heartbeat, self._heartbeat, limits.HEARTBEAT_INTERVAL)
        await self._phase(Phase.sweep, self._sweep, 0)
        if not self._stopping:
            await self._phase(Phase.claim, self._claim, 0)

        intervals: dict[Phase, tuple[Callable[[], Awaitable[None]], float]] = {
            Phase.housekeeping: (self._housekeeping, limits.HOUSEKEEPING_INTERVAL),
            Phase.commits: (self._resume_commits, limits.HOUSEKEEPING_INTERVAL),
            Phase.retries: (self._retry_after_limits, limits.HOUSEKEEPING_INTERVAL),
            Phase.inbox: (self._scan_inbox, get_ingest_settings().INBOX_POLL_SECONDS),
            Phase.purge: (self._purge, limits.PURGE_INTERVAL),
        }
        for phase, (run, interval) in intervals.items():
            state = self._phases[phase]
            if self._stopping or time.monotonic() < state.next_due:
                continue
            if not background:
                await self._phase(phase, run, interval)
            elif state.task is None or state.task.done():
                state.task = asyncio.get_running_loop().create_task(
                    self._background_phase(phase, run, interval), name=f"ai-ingest-{phase.name}"
                )

    async def _background_phase(self, phase: Phase, run: Callable[[], Awaitable[None]], interval: float) -> None:
        await self._phase(phase, run, interval)

    async def _phase[T](self, phase: Phase, run: Callable[[], Awaitable[T]], interval: float) -> T | None:
        """
        Runs a phase if it's due. A failure is logged once (with its stack, not its message) and backs the phase off,
        doubling up to `PHASE_BACKOFF_MAX`; the next success logs the recovery. None when it didn't run or failed.
        """
        state = self._phases[phase]
        started = time.monotonic()
        if started < state.next_due:
            return None
        try:
            result = await run()
        except Exception as e:
            state.backoff = min(max(state.backoff * 2, FIRST_BACKOFF), limits.PHASE_BACKOFF_MAX)
            state.next_due = time.monotonic() + state.backoff
            if not state.failing:
                state.failing = True
                logger.error(
                    f"Recipe card dispatcher: the {phase.value} failed ({type(e).__name__}); retrying with a backoff "
                    f"of up to {limits.PHASE_BACKOFF_MAX} s:\n{safe_trace(e)}"
                )
            return None

        if state.failing:
            logger.info(f"Recipe card dispatcher: the {phase.value} works again")
        state.failing = False
        state.backoff = 0.0
        state.next_due = started + interval
        return result

    # ==========================================
    # Phases

    async def _mark_seen(self) -> None:
        await self._call(storage.mark_dispatcher_seen)

    async def _check_pause(self) -> bool:
        paused = await self._call(storage.is_paused)
        if paused:
            if not self._paused:
                logger.info("Recipe card ingestion is paused while a backup is restored")
            self._paused = True
            self._last_paused_at = time.monotonic()
        elif self._paused:
            logger.info("Recipe card ingestion resumed after the backup restore")
            self._paused = False
            # renew this process's leases before anything else, so no sweep takes them
            self._phases[Phase.heartbeat].next_due = 0.0
        return paused

    def _held(self) -> dict[UUID, _RunningTask]:
        return {token: handle for token, handle in self._tasks.items() if not handle.done.is_set()}

    async def _heartbeat(self) -> None:
        held = self._held()
        if not held:
            return
        alive = await self._call(_with_session, lambda session: IngestQueue(session).heartbeat(list(held), utcnow()))
        restored_at = await self._call(storage.restored_at) if set(held) - set(alive) else None
        for token, handle in held.items():
            if token not in alive:
                if restored_at is not None and restored_at >= handle.claimed_at:
                    # a backup restore queued its job again: it finishes, and its result is kept for the next task
                    if not handle.cut_off_logged:
                        handle.cut_off_logged = True
                        logger.info(
                            f"Recipe card job {handle.job_id}: a backup restore took its task's lease; it finishes, "
                            "and its reading is kept for the card's next task"
                        )
                    continue
                # usually the task's own finalize, just before its thread ends; else a commit, discard or sweep
                if handle.cancel(CancelReason.vanished):
                    logger.debug(f"Recipe card job {handle.job_id}: its lease is gone; stopping its task")
            elif alive[token] and handle.cancel(CancelReason.cancelled):
                logger.info(f"Recipe card job {handle.job_id}: cancelling its task, as asked")

    async def _sweep(self) -> None:
        if self._last_paused_at is not None:
            if time.monotonic() - self._last_paused_at < 2 * limits.HEARTBEAT_INTERVAL:
                return
        held = set(self._held())
        # what it requeues, this tick's claims pick up
        await self._call(_with_session, lambda session: sweep_expired(session, utcnow(), held=held))

    async def _claim(self) -> None:
        held = self._held().values()
        general = self.concurrency - sum(1 for handle in held if not handle.reread_slot)
        reread = limits.REREAD_SLOTS - sum(1 for handle in held if handle.reread_slot)
        if general <= 0 and reread <= 0:
            return

        # A task of its own that shutdown waits for (`_finish_claiming`): cancelling the loop mid-claim would leave
        # the claims already committed without their threads, so neither run nor released
        claiming = asyncio.get_running_loop().create_task(
            self._claim_and_start(general, reread), name="ai-ingest-claim"
        )
        self._claiming = claiming
        try:
            await asyncio.shield(claiming)
        finally:
            if claiming.done():
                self._claiming = None

    async def _claim_and_start(self, general: int, reread: int) -> None:
        owner, stop = self.owner, self._stop_claims
        batch: ClaimBatch = await self._call(
            _with_session,
            lambda session: claim_unless_stopping(
                session, owner=owner, general_slots=general, reread_slots=reread, stop=stop
            ),
        )
        if stop.is_set() and batch.claims:
            # shutdown began after the claim's own check: give them back rather than start them
            leases = [(claim.job_id, claim.token) for claim in batch.claims]
            await self._call(_with_session, functools.partial(release_leases, leases=leases))
            batch.claims = []
        # the threads start in this same step, so a claim is never left without its task
        for claim in batch.claims:
            self._start(claim)
        if batch.error is not None:
            raise batch.error

    async def _finish_claiming(self, timeout: float) -> None:
        """
        Shutdown: waits up to `timeout` for a claim that was in flight when the loop was cancelled. It takes no further
        task and gives back what it took (`claim_unless_stopping`), so nothing it claimed is left running.
        """
        claiming, self._claiming = self._claiming, None
        if claiming is None:
            return
        await asyncio.wait({claiming}, timeout=timeout)
        if not claiming.done():
            logger.warning(
                "A recipe card claim was still running at shutdown; what it claims is queued again when it ends"
            )
        elif not claiming.cancelled() and (error := claiming.exception()) is not None:
            logger.error(f"Recipe card dispatcher: the {Phase.claim.value} failed at shutdown:\n{safe_trace(error)}")

    async def _housekeeping(self) -> None:
        from .. import events  # imported here: stage B modules import the dispatcher (`wake`)

        await self._call(events.housekeeping, utcnow(), abandon_on_cancel=True)

    async def _resume_commits(self) -> None:
        from .. import commit

        await self._call(commit.resume_stale_commits, utcnow(), abandon_on_cancel=True)

    async def _retry_after_limits(self) -> None:
        from . import retries

        if await self._call(retries.retry_waiting, utcnow(), abandon_on_cancel=True):
            self.wake()

    async def _scan_inbox(self) -> None:
        from .. import inbox

        await self._call(inbox.scan_once, abandon_on_cancel=True)

    async def _purge(self) -> None:
        from .. import retention

        await self._call(retention.purge_once, utcnow(), abandon_on_cancel=True)

    # ==========================================
    # Task threads

    def _start(self, claim: Claim) -> None:
        handle = _RunningTask(claim, deadline=time.monotonic() + limits.TASK_DEADLINE)
        thread = threading.Thread(
            target=self._thread_main, args=(handle,), name=f"ai-ingest-{str(claim.job_id)[:8]}", daemon=True
        )
        self._tasks[claim.token] = handle
        try:
            thread.start()
        except RuntimeError as e:
            # no thread for it: the lease expires and the sweep queues the task again
            handle.done.set()
            logger.error(f"Recipe card job {claim.job_id}: couldn't start its task thread ({e})")
            return
        logger.debug(
            f"Recipe card job {claim.job_id}: task started ({'re-read slot' if claim.reread_slot else 'slot'})"
        )

    def _thread_main(self, handle: _RunningTask) -> None:
        async def main() -> Any:
            task = asyncio.current_task()
            assert task is not None
            handle.attach(asyncio.get_running_loop(), task)
            return await worker.run_task(
                handle.job_id, handle.token, deadline=handle.deadline, cancel_reason=handle.reason
            )

        try:
            with asyncio.Runner() as runner:
                runner.run(main())
        except BaseException as e:
            logger.error(f"Recipe card job {handle.job_id}: its task thread failed:\n{safe_trace(e)}")
        finally:
            handle.done.set()
            self.wake()  # a slot is free

    def _reap(self) -> None:
        for token in [token for token, handle in self._tasks.items() if handle.done.is_set()]:
            del self._tasks[token]

    def _check_deadlines(self) -> None:
        now = time.monotonic()
        for handle in self._held().values():
            if now < handle.deadline:
                continue
            if handle.cancel(CancelReason.timeout):
                logger.warning(f"Recipe card job {handle.job_id}: its task ran past its deadline and is cancelled")
            elif not handle.stuck_logged and now >= handle.deadline + limits.HEARTBEAT_INTERVAL:
                handle.stuck_logged = True
                logger.warning(
                    f"Recipe card job {handle.job_id}: its task hasn't stopped since it was cancelled (busy in "
                    "synchronous code); it keeps its slot until it returns"
                )


dispatcher = IngestDispatcher()
"""This process's dispatcher"""
