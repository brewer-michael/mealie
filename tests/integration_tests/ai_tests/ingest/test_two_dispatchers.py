"""
Two dispatchers on one database, as two worker processes run them (docs/ai/PHASE2.md §3.2, §3.8, §8, §18): each on a
thread and event loop of its own, with its own sessions, reading one batch of real uploads with a fake provider. Each
claim is a conditional `UPDATE`, so no job runs twice; each card's task checks its batch after finalizing, and the
conditional `notified_at` update lets only one of them send the notification. Runs on SQLite and PostgreSQL.
"""

import asyncio
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mealie.services import ocr
from mealie.services.ai.ingest import events, tasks
from mealie.services.ai.ingest.runner import dispatcher as dispatcher_module
from mealie.services.ai.ingest.runner.dispatcher import ClaimBatch, IngestDispatcher
from mealie.services.ai.ingest.runner.types import TaskContext
from tests.integration_tests.ai_tests.ingest.card_flow_testing import (
    BATCHES,
    READY_EVENT,
    Notified,
    all_settled,
    apprise_sent,  # noqa: F401  (the fixture)
    batch_columns,
    job_columns,
    make_card_reader,
    make_notifier,
    only_households,
    photo,
    quiet_other_phases,
    run_until,
    seal,
    sent_to,
    start_batch,
    upload,
)
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import FakeCardAI, banana_answers
from tests.utils.fixture_schemas import TestUser

PROCESSES = ("process-1", "process-2")
WAIT = 20.0
"""How long a test's gates wait for the other dispatcher before giving up (and the test then fails)"""


@dataclass
class Activity:
    """What the two dispatchers did: their claims, the extractions their tasks ran, and the batch checks after them"""

    claims: list[tuple[str, UUID, UUID]] = field(default_factory=list)
    """(owner instance, job, lease token) for every claim won"""
    extractions: list[tuple[UUID, UUID]] = field(default_factory=list)
    """(job, lease token) for every extraction a task ran"""
    notify_checks: list[tuple[UUID, bool]] = field(default_factory=list)
    """(batch, whether this check sent it) for every task's check after its finalize"""
    errors: list[BaseException] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def owners(self) -> Counter[str]:
        return Counter(owner for owner, _, _ in self.claims)


@pytest.fixture()
def user(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch) -> TestUser:
    make_card_reader(unique_user_fn_scoped)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    only_households(monkeypatch, unique_user_fn_scoped.household_id)
    quiet_other_phases(monkeypatch)
    FakeCardAI(banana_answers()).install(monkeypatch)
    return unique_user_fn_scoped


@pytest.fixture()
def activity(monkeypatch: pytest.MonkeyPatch) -> Activity:
    """Records claims, extractions and the tasks' batch checks; each goes on to the real thing"""
    seen = Activity()
    claim_tasks = dispatcher_module.claim_tasks
    handle_extract = tasks.handle_extract
    maybe_notify_batch = events.maybe_notify_batch

    def recording_claims(
        session: Session, *, owner: str, general_slots: int, reread_slots: int, stop: threading.Event | None = None
    ) -> ClaimBatch:
        batch = claim_tasks(session, owner=owner, general_slots=general_slots, reread_slots=reread_slots, stop=stop)
        instance = owner.rsplit(":", 1)[-1]
        with seen.lock:
            seen.claims.extend((instance, claim.job_id, claim.token) for claim in batch.claims)
        return batch

    async def recording_extract(ctx: TaskContext) -> Any:
        with seen.lock:
            seen.extractions.append((ctx.job_id, ctx.token))
        return await handle_extract(ctx)

    def recording_notify(batch_id: UUID) -> bool:
        sent = maybe_notify_batch(batch_id)
        if threading.current_thread().name.startswith("ai-ingest-"):  # a card's task, not housekeeping or a seal
            with seen.lock:
                seen.notify_checks.append((batch_id, sent))
        return sent

    monkeypatch.setattr(dispatcher_module, "claim_tasks", recording_claims)
    monkeypatch.setattr(tasks, "handle_extract", recording_extract)
    monkeypatch.setattr(events, "maybe_notify_batch", recording_notify)
    return seen


def run_two_dispatchers(condition: Callable[[], bool], activity: Activity) -> None:
    """
    Two dispatchers, each `run_once()`-ing on its own thread and event loop until `condition()` holds, then draining
    and stopping (releasing whatever it still held). They start their first tick together.
    """
    start = threading.Barrier(len(PROCESSES))

    def process(instance: str) -> None:
        async def main() -> None:
            dispatcher = IngestDispatcher(concurrency=2, instance=instance)
            try:
                await asyncio.to_thread(start.wait, WAIT)
                await run_until(dispatcher, condition, timeout=90)
            finally:
                await dispatcher.stop()

        try:
            asyncio.run(main())
        except BaseException as e:
            with activity.lock:
                activity.errors.append(e)

    threads = [threading.Thread(target=process, args=(name,), name=f"dispatcher-{name}") for name in PROCESSES]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not any(thread.is_alive() for thread in threads), "a dispatcher didn't stop"
    assert activity.errors == []


async def wait_for_event(event: threading.Event, timeout: float = WAIT) -> None:
    """Waits (on the task's own loop, cancellably) for another thread's signal; a test's assertions catch a miss"""
    deadline = time.monotonic() + timeout
    while not event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def queue_batch(api_client: TestClient, user: TestUser, cards: int) -> tuple[str, list[str]]:
    """A phone's batch of single-sided cards, uploaded and then sealed with Done: (batch, jobs in capture order)"""
    batch_id = start_batch(api_client, user)
    job_ids = [
        upload(api_client, user, photo(f"card {position}"), batchId=batch_id, position=position)["jobs"][0]["id"]
        for position in range(cards)
    ]
    seal(api_client, user, batch_id)
    return batch_id, job_ids


def assert_each_ran_once(activity: Activity, job_ids: list[str]) -> None:
    ids = sorted(UUID(job_id) for job_id in job_ids)
    claimed = [job_id for _, job_id, _ in activity.claims if job_id in ids]
    assert sorted(claimed) == ids  # each job claimed once, by one of the two
    extracted = [job_id for job_id, _ in activity.extractions if job_id in ids]
    assert sorted(extracted) == ids  # and read once
    tokens = {token for _, job_id, token in activity.claims if job_id in ids}
    assert tokens == {token for job_id, token in activity.extractions if job_id in ids}

    for job_id in job_ids:
        row = job_columns(job_id)
        assert row["status"] == "ready"
        assert row["attempts"] == 1  # never claimed again, never swept
        assert (row["task_state"], row["lease_token"], row["lease_owner"]) == (None, None, None)
        assert row["draft"] is not None


# ==================================================================================================================


def test_two_dispatchers_never_run_a_job_twice(
    api_client: TestClient,
    user: TestUser,
    activity: Activity,
    apprise_sent: list[Notified],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    home_assistant = make_notifier(api_client, user, cards_ready=True)
    batch_id, job_ids = queue_batch(api_client, user, cards=8)

    # the first tasks wait until the other process has claimed too, so both really share the batch
    both_claimed = threading.Event()
    recording_claims = dispatcher_module.claim_tasks

    def claims_seen(session: Session, **kwargs: Any) -> ClaimBatch:
        batch = recording_claims(session, **kwargs)
        if len(activity.owners()) == len(PROCESSES):
            both_claimed.set()
        return batch

    extract = tasks.handle_extract

    async def after_both_claimed(ctx: TaskContext) -> Any:
        await wait_for_event(both_claimed)
        return await extract(ctx)

    monkeypatch.setattr(dispatcher_module, "claim_tasks", claims_seen)
    monkeypatch.setattr(tasks, "handle_extract", after_both_claimed)

    run_two_dispatchers(all_settled(job_ids), activity)

    assert both_claimed.is_set(), f"only one dispatcher claimed: {activity.owners()}"
    assert set(activity.owners()) == set(PROCESSES)
    assert_each_ran_once(activity, job_ids)

    # one notification for the batch, sent by whichever card's task found the batch finished
    [ready] = sent_to(apprise_sent, home_assistant, READY_EVENT)
    assert ready.document()["batchId"] == batch_id
    assert ready.document()["jobIds"] == job_ids
    assert ready.body == "8 cards are ready to review (8 need a look)."
    assert [sent for checked, sent in activity.notify_checks if checked == UUID(batch_id)].count(True) == 1
    assert batch_columns(batch_id)["notified_at"] is not None


@pytest.mark.parametrize("round_", range(3))
def test_the_last_cards_finishing_together_in_both_processes_notify_once(
    api_client: TestClient,
    user: TestUser,
    activity: Activity,
    apprise_sent: list[Notified],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    round_: int,
):
    """
    A sealed batch of four cards: each dispatcher takes two, the four tasks wait until all four have started, and
    their four batch checks start at the same moment, two in each process. Exactly one sends the notification.
    """
    home_assistant = make_notifier(api_client, user, cards_ready=True)
    batch_id, job_ids = queue_batch(api_client, user, cards=4)

    all_started = threading.Event()
    checks = threading.Barrier(len(job_ids), timeout=WAIT)
    extract = tasks.handle_extract
    recording_notify = events.maybe_notify_batch

    async def together(ctx: TaskContext) -> Any:
        result = await extract(ctx)  # records it
        if len(activity.extractions) == len(job_ids):
            all_started.set()
        await wait_for_event(all_started)
        return result

    def at_once(batch_id_: UUID) -> bool:
        if threading.current_thread().name.startswith("ai-ingest-"):
            try:
                checks.wait()
            except threading.BrokenBarrierError as e:
                activity.errors.append(e)
        return recording_notify(batch_id_)

    monkeypatch.setattr(tasks, "handle_extract", together)
    monkeypatch.setattr(events, "maybe_notify_batch", at_once)

    run_two_dispatchers(all_settled(job_ids), activity)

    assert all_started.is_set(), "the four tasks never ran at the same time"
    assert activity.owners() == Counter(dict.fromkeys(PROCESSES, 2))
    assert_each_ran_once(activity, job_ids)

    # four checks at once, two per process: one sent it
    assert sorted(sent for checked, sent in activity.notify_checks if checked == UUID(batch_id)) == [
        False,
        False,
        False,
        True,
    ]
    [ready] = sent_to(apprise_sent, home_assistant, READY_EVENT)
    assert ready.document()["batchId"] == batch_id
    assert ready.document()["jobIds"] == job_ids

    batch = api_client.get(f"{BATCHES}/{batch_id}", headers=user.token).json()
    assert batch["notifiedAt"] is not None
    assert [job["status"] for job in batch["jobs"]] == ["ready"] * 4
