"""
Cards that failed because every provider was over its monthly limit (docs/ai/PHASE2.md §3.6): they're read again
automatically, at the next reset (the first of next month, UTC) or once the limit no longer applies (checked at most
every 10 minutes per group, under the card's own policy), as a manual retry would; any other failure clears the retry.
"""

import calendar
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from ingest_runner_testing import FakeHandlers, Jobs, extract_result, run, settle

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestQueue, IngestRepos, LimitWait, naive_utc, utcnow
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.schema.household.group_events import GroupEventNotifierSave
from mealie.schema.recipe_ingest import (
    IngestErrorCode,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    RecipeIngestionSettingsUpdate,
)
from mealie.services import ocr
from mealie.services.ai.errors import AIProviderLimitReachedError
from mealie.services.ai.ingest import events, limits
from mealie.services.ai.ingest.runner import retries
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.finalize import finalize_extract, finalize_failure, next_limit_reset
from mealie.services.ai.ingest.runner.retries import retry_waiting
from mealie.services.ai.ingest.runner.types import TaskContext, TaskFailed
from tests.utils.fixture_schemas import TestUser

LIMIT = IngestErrorCode.limit_reached.value

MAYBE_NOTIFY_BATCH = events.maybe_notify_batch
"""The real one: the runner's fixtures put a recorder in its place"""


@pytest.fixture(autouse=True)
def only_this_tests_waiting_cards(monkeypatch: pytest.MonkeyPatch, jobs: Jobs) -> None:
    """The retry phase reads every group's waiting cards: keep it to this test's, and forget earlier checks"""
    waiting_for_limit = IngestQueue.waiting_for_limit

    def own(self: IngestQueue) -> list[LimitWait]:
        return [wait for wait in waiting_for_limit(self) if wait.job_id in jobs.ids]

    monkeypatch.setattr(IngestQueue, "waiting_for_limit", own)
    retries.forget_checks()


class LimitChecks(list[tuple[UUID, bool]]):
    """`retries.limit_applies` stood in for: each check (group, local only), answering `applies`"""

    applies: bool | None = True

    def set(self, applies: bool | None) -> None:
        self.applies = applies

    def __call__(self, group_id: UUID, household_id: UUID, *, local_only: bool) -> bool | None:
        self.append((group_id, local_only))
        return self.applies


@pytest.fixture()
def limit(monkeypatch: pytest.MonkeyPatch) -> LimitChecks:
    """Whether the group's limit applies, as the test says (it does, to start with)"""
    checks = LimitChecks()
    monkeypatch.setattr(retries, "limit_applies", checks)
    return checks


def _waiting(jobs: Jobs, *, retry_in: timedelta = timedelta(days=3), **values: Any) -> UUID:
    return jobs.create(
        status=IngestStatus.failed,
        kind=None,
        state=None,
        error_code=LIMIT,
        auto_retry_at=utcnow() + retry_in,
        **values,
    )


def _queued_again(jobs: Jobs, job_id: UUID) -> bool:
    row = jobs.row(job_id)
    return (row["status"], row["task_state"], row["error_code"], row["auto_retry_at"]) == (
        IngestStatus.processing,
        IngestTaskState.queued,
        None,
        None,
    )


def test_the_next_reset_is_the_first_of_next_month_in_utc():
    def naive(*parts: int) -> datetime:
        return datetime(*parts, tzinfo=UTC).replace(tzinfo=None)

    assert next_limit_reset(naive(2026, 10, 4, 13, 0)) == naive(2026, 11, 1)
    assert next_limit_reset(naive(2026, 12, 31, 23, 59, 59)) == naive(2027, 1, 1)
    assert next_limit_reset(naive(2026, 2, 1, 0, 0)) == naive(2026, 3, 1)
    # an aware time is read as the moment it names: 20:00 on the 31st in New York is already the 1st in UTC
    assert next_limit_reset(datetime(2026, 10, 31, 20, 0, tzinfo=timezone(timedelta(hours=-4)))) == naive(2026, 12, 1)


def test_a_card_over_the_monthly_limit_waits_for_the_next_reset(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers
):
    over_limit = jobs.create()

    async def limit_reached(ctx: TaskContext) -> Any:
        raise AIProviderLimitReachedError("every provider is over its monthly limit")

    handlers.default = limit_reached

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(over_limit)
    assert (row["status"], row["error_code"]) == (IngestStatus.failed, IngestErrorCode.limit_reached)
    assert naive_utc(row["auto_retry_at"]) == next_limit_reset()


def test_another_failure_clears_a_waiting_retry(dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers):
    """A card retried by hand before the reset that then fails for another reason isn't retried automatically"""
    job_id = jobs.create(auto_retry_at=utcnow() + timedelta(days=3))

    async def provider_failed(ctx: TaskContext) -> Any:
        raise RuntimeError("OpenAI Request Failed. boom")

    handlers.default = provider_failed

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["auto_retry_at"]) == (IngestStatus.failed, None)


def test_a_card_is_read_again_when_its_retry_time_comes(jobs: Jobs, limit: LimitChecks):
    job_id = _waiting(jobs, retry_in=timedelta(days=3))
    not_waiting = jobs.create(status=IngestStatus.failed, kind=None, state=None, error_code="provider_failed")

    assert retry_waiting(utcnow()) == 0  # the limit still applies
    assert jobs.row(job_id)["status"] == IngestStatus.failed

    assert retry_waiting(utcnow() + timedelta(days=3, seconds=1)) == 1  # time travel: the reset has come
    assert _queued_again(jobs, job_id)
    assert jobs.row(not_waiting)["task_state"] is None
    assert retry_waiting(utcnow() + timedelta(days=3, seconds=1)) == 0  # once


def test_a_raised_limit_reads_waiting_cards_at_the_next_check(
    jobs: Jobs, limit: LimitChecks, monkeypatch: pytest.MonkeyPatch
):
    first, second = _waiting(jobs), _waiting(jobs, retry_in=timedelta(days=20))

    assert retry_waiting(utcnow()) == 0
    assert len(limit) == 1  # one check for the group's cards

    limit.set(False)  # a manager raised the limit
    assert retry_waiting(utcnow()) == 0  # checked at most every 10 minutes
    assert len(limit) == 1

    monkeypatch.setattr(limits, "LIMIT_RECHECK_INTERVAL", 0)
    assert retry_waiting(utcnow()) == 2
    assert _queued_again(jobs, first) and _queued_again(jobs, second)


def test_a_card_that_fails_again_after_a_lift_waits_for_a_new_check(jobs: Jobs, limit: LimitChecks):
    """
    A "lifted" answer isn't kept for the next ticks: a card it queued that fails `limit_reached` again (the raised
    budget ran out after a few cards) waits for a check that says so, rather than being read again every minute
    """
    job_id = _waiting(jobs)
    limit.set(False)  # a manager raised the limit a little
    assert retry_waiting(utcnow()) == 1
    assert _queued_again(jobs, job_id)

    # read, and over the limit again: finalize puts it back to waiting
    token = uuid4()
    with session_context() as session:
        assert IngestQueue(session).claim(job_id, token=token, owner="test", now=utcnow(), group_cap=0)
        finalize_failure(session, job_id, token, IngestErrorCode.limit_reached)
    limit.set(True)
    for _ in range(3):  # the next housekeeping ticks
        assert retry_waiting(utcnow()) == 0
    assert len(limit) == 2  # checked again once, then that answer kept
    assert jobs.row(job_id)["status"] == IngestStatus.failed


def _read_and_over_the_limit_again(jobs: Jobs, job_id: UUID) -> None:
    """
    The card a lift queued is read, and fails `limit_reached` again: finalize puts it back to waiting (its reset kept
    a month away, so the test's time travel never reaches it)
    """
    token = uuid4()
    with session_context() as session:
        assert IngestQueue(session).claim(job_id, token=token, owner="test", now=utcnow(), group_cap=0)
        finalize_failure(session, job_id, token, IngestErrorCode.limit_reached)
    jobs.update(job_id, auto_retry_at=utcnow() + timedelta(days=30))


def _lift_backoff(jobs: Jobs, job_id: UUID) -> tuple[int, datetime | None]:
    row = jobs.row(job_id)
    return row["lift_retries"], naive_utc(row["lift_retry_at"]) if row["lift_retry_at"] else None


def test_a_card_a_lift_doesnt_help_is_read_again_less_and_less_often(
    jobs: Jobs, limit: LimitChecks, monkeypatch: pytest.MonkeyPatch
):
    """
    A "lifted" answer that doesn't help the card (OCR stands in for an image slot over its limit, but finds no text on
    the card, so it fails `limit_reached` again while the check keeps saying "lifted") queues it again only after
    `LIMIT_RECHECK_INTERVAL`, then twice that, and so on up to `LIFT_RETRY_MAX_WAIT`: not at every run until the reset
    """
    interval = timedelta(seconds=limits.LIMIT_RECHECK_INTERVAL)
    monkeypatch.setattr(retries, "LIFT_RETRY_MAX_WAIT", 3 * limits.LIMIT_RECHECK_INTERVAL)
    job_id = _waiting(jobs, retry_in=timedelta(days=30))
    limit.set(False)

    queued_at = utcnow()
    assert retry_waiting(queued_at) == 1
    for lifts, wait in enumerate((interval, 2 * interval, 3 * interval), start=1):  # doubled, up to the longest
        _read_and_over_the_limit_again(jobs, job_id)
        assert _lift_backoff(jobs, job_id) == (lifts, queued_at + wait)  # kept on the card
        now = queued_at
        while now + timedelta(seconds=limits.HOUSEKEEPING_INTERVAL) < queued_at + wait:  # every run until then
            now += timedelta(seconds=limits.HOUSEKEEPING_INTERVAL)
            assert retry_waiting(now) == 0
            assert jobs.row(job_id)["status"] == IngestStatus.failed
        queued_at += wait
        assert retry_waiting(queued_at) == 1
        assert _queued_again(jobs, job_id)

    # its reset has come: read again whatever the last lift said, and the backoff starts over
    _read_and_over_the_limit_again(jobs, job_id)
    assert retry_waiting(utcnow() + timedelta(days=40)) == 1
    assert _queued_again(jobs, job_id)
    assert _lift_backoff(jobs, job_id) == (0, None)


def test_every_worker_process_keeps_a_cards_lift_backoff(jobs: Jobs, limit: LimitChecks):
    """
    Every worker process runs the retry phase: one that never queued the card (nothing of it in its memory) still
    waits for the backoff the last lift set, so the card isn't read once per process
    """
    interval = timedelta(seconds=limits.LIMIT_RECHECK_INTERVAL)
    job_id = _waiting(jobs, retry_in=timedelta(days=30))
    limit.set(False)
    start = utcnow()
    assert retry_waiting(start) == 1  # one process's lift queues it
    _read_and_over_the_limit_again(jobs, job_id)  # and it doesn't help

    now = start
    while now + timedelta(seconds=limits.HOUSEKEEPING_INTERVAL) < start + interval:
        now += timedelta(seconds=limits.HOUSEKEEPING_INTERVAL)
        retries.forget_checks()  # the run of another process, with its own memory
        assert retry_waiting(now) == 0
    retries.forget_checks()
    assert retry_waiting(start + interval) == 1
    assert _lift_backoff(jobs, job_id) == (2, start + 3 * interval)


def test_a_cards_lift_backoff_goes_once_it_no_longer_waits(jobs: Jobs, limit: LimitChecks):
    """
    A card a lift queued keeps its backoff only while it fails `limit_reached` again: reading it, or another failure,
    clears it
    """
    read, failed, waits = (_waiting(jobs, retry_in=timedelta(days=30)) for _ in range(3))
    limit.set(False)
    assert retry_waiting(utcnow()) == 3
    assert {_lift_backoff(jobs, job_id)[0] for job_id in (read, failed, waits)} == {1}

    for job_id in (read, failed):
        token = uuid4()
        with session_context() as session:
            assert IngestQueue(session).claim(job_id, token=token, owner="test", now=utcnow(), group_cap=0)
            if job_id == read:
                finalize_extract(session, job_id, token, extract_result())
            else:
                finalize_failure(session, job_id, token, IngestErrorCode.no_recipe_found)
    _read_and_over_the_limit_again(jobs, waits)

    assert _lift_backoff(jobs, read) == (0, None)
    assert _lift_backoff(jobs, failed) == (0, None)
    assert _lift_backoff(jobs, waits)[0] == 1


def test_a_lift_is_checked_once_per_tick_for_a_groups_cards(jobs: Jobs, limit: LimitChecks):
    first, second = _waiting(jobs), _waiting(jobs)
    limit.set(False)
    assert retry_waiting(utcnow()) == 2
    assert len(limit) == 1
    assert _queued_again(jobs, first) and _queued_again(jobs, second)


def test_a_card_that_cant_be_read_for_another_reason_waits_for_its_retry_time(jobs: Jobs, limit: LimitChecks):
    job_id = _waiting(jobs)
    limit.set(None)  # no provider set up any more, say: not a limit, so not read early
    assert retry_waiting(utcnow()) == 0
    assert jobs.row(job_id)["status"] == IngestStatus.failed


def test_the_check_uses_the_cards_own_policy(jobs: Jobs, limit: LimitChecks):
    _waiting(jobs, local_only=True)
    _waiting(jobs, local_only=False)
    retry_waiting(utcnow())
    assert sorted(local for _, local in limit) == [False, True]

    limit.clear()
    retries.forget_checks()
    settings = jobs.repos.settings
    before = settings.get()
    settings.upsert(RecipeIngestionSettingsUpdate(local_only=True, cross_read=before.cross_read))
    try:
        retry_waiting(utcnow())
    finally:
        settings.upsert(before)
    assert [local for _, local in limit] == [True]  # the group keeps cards local now: both are read local-only


def test_the_dispatcher_reads_a_waiting_card_again(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    limit: LimitChecks,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(retries, "retry_waiting", retry_waiting)  # the real phase, not the recorder
    job_id = _waiting(jobs, retry_in=-timedelta(seconds=1))  # its reset has come

    async def scenario() -> None:
        await dispatcher.run_once()  # queues it again
        await dispatcher.run_once()  # and claims it
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["auto_retry_at"]) == (IngestStatus.ready, None, None)
    assert len(handlers.calls) == 1


# ==========================================
# Whether the limit still applies, from the providers' usage


def _capped(user: TestUser, name: str, *, limit: int, used: int) -> Any:
    provider = user.repos.group_ai_providers.create(
        AIProviderCreate(name=name, model="m", api_key="k", monthly_token_limit=limit)
    )
    user.repos.group_ai_usage.create(
        AIUsageLogCreate(
            provider_id=provider.id,
            provider_name=provider.name,
            model=provider.model,
            protocol=provider.protocol,
            slot=AIProviderSlot.default,
            prompt_tokens=used,
            completion_tokens=0,
            success=True,
        )
    )
    return provider


def test_the_limit_check_reads_the_providers_usage(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch):
    user = unique_user_fn_scoped
    monkeypatch.setattr(ocr, "is_available", lambda: True)  # the photo can be read by OCR: the default slot decides
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    capped = _capped(user, "Capped", limit=100, used=100)
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=capped.id, image_provider_id=None, audio_provider_id=None),
    )
    assert retries.limit_applies(group_id, household_id, local_only=False) is True

    # local-only: the cloud provider can't be used at all, which isn't a limit
    assert retries.limit_applies(group_id, household_id, local_only=True) is None

    # a manager raises the limit
    user.repos.group_ai_providers.update(capped.id, capped.model_copy(update={"monthly_token_limit": 1000}))
    assert retries.limit_applies(group_id, household_id, local_only=False) is False


def test_a_card_retried_by_hand_and_read_no_longer_waits(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers
):
    job_id = jobs.create(auto_retry_at=utcnow() + timedelta(days=3))  # a manual retry before the reset

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["auto_retry_at"]) == (IngestStatus.ready, None)


# ==========================================
# The household hears once the cards that waited are read


def _notified_batch(jobs: Jobs) -> None:
    """The batch was sealed and its notification went out (saying its waiting cards wait for the monthly limit)"""
    with session_context() as session:
        session.execute(
            sa.update(RecipeIngestionBatch)
            .where(RecipeIngestionBatch.id == jobs.batch_id)
            .values(sealed_at=utcnow(), notified_at=utcnow(), notify_attempts=1, notify_delivered=["0" * 64])
        )
        session.commit()


@pytest.fixture()
def notifications(monkeypatch: pytest.MonkeyPatch, unique_user: TestUser) -> Iterator[list[events.AIEvent]]:
    """The ready notifications sent to a notifier of the test household (nothing leaves the server)"""
    sent: list[events.AIEvent] = []
    notifier = unique_user.repos.group_event_notifier.create(
        GroupEventNotifierSave(
            name="Kitchen HA",
            apprise_url="json://ha.local/hook",
            group_id=unique_user.group_id,
            household_id=unique_user.household_id,
        )
    )
    with session_context() as session:
        repos = IngestRepos(session, UUID(unique_user.group_id), UUID(unique_user.household_id))
        repos.notifier_options.set(notifier.id, recipe_ingestion_ready=True)

    def deliver(event: events.AIEvent, url: str) -> bool:
        sent.append(event)
        return True

    monkeypatch.setattr(events, "maybe_notify_batch", MAYBE_NOTIFY_BATCH)  # the real one, not the phases' recorder
    monkeypatch.setattr(events, "deliver", deliver)
    monkeypatch.setattr(retries, "retry_waiting", retry_waiting)  # the real phase
    yield sent
    unique_user.repos.group_event_notifier.delete(notifier.id)


def test_cards_read_after_waiting_for_the_limit_notify_their_batch_once(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    limit: LimitChecks,
    notifications: list[events.AIEvent],
):
    jobs.ready()  # read the first time round: the batch's own notification counted it
    first = _waiting(jobs, retry_in=-timedelta(seconds=1))  # their reset has come
    second = _waiting(jobs, retry_in=-timedelta(seconds=1))
    _waiting(jobs)  # its limit still applies: it waits on, and isn't counted
    _notified_batch(jobs)

    async def scenario() -> None:
        await dispatcher.run_once()  # queues both again
        await dispatcher.run_once()  # and reads them
        await settle(dispatcher)
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert jobs.row(first)["status"] == jobs.row(second)["status"] == IngestStatus.ready

    [sent] = notifications  # one for the two cards read together
    assert sent.event_type == events.AIEventTypes.recipe_ingestion_ready
    assert sent.message.title == "Recipe cards ready"
    assert sent.message.body == ("2 cards that waited for the monthly limit were read. 2 cards are ready to review.")
    data = sent.document_data
    assert isinstance(data, events.EventIngestionReadyData)
    assert (data.batch_id, data.job_ids, data.ready_count, data.failed_count) == (jobs.batch_id, [first, second], 2, 0)

    # and only once
    assert events.maybe_notify_batch(jobs.batch_id) is False
    assert len(notifications) == 1


def test_cards_over_the_limit_are_told_they_wait_then_that_they_were_read(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    limit: LimitChecks,
    notifications: list[events.AIEvent],
):
    """
    Cards uploaded while the monthly limit is reached fail `limit_reached` within seconds: their batch's notification
    says they wait (as the upload's summary did), not that they failed, and once they're read a second one says so
    """
    first, second = jobs.create(), jobs.create()
    jobs.repos.batches.seal(jobs.batch_id, utcnow())

    async def limit_reached(ctx: TaskContext) -> Any:
        raise AIProviderLimitReachedError("every provider is over its monthly limit")

    async def read_both() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    handlers.default = limit_reached
    run(read_both())
    [waiting] = notifications
    reset = next_limit_reset()
    assert waiting.event_type == events.AIEventTypes.recipe_ingestion_ready  # Home Assistant's automation matches it
    assert waiting.message.title == "Recipe cards waiting"
    assert waiting.message.body == (
        f"2 cards are waiting for the monthly limit. They'll be read when it resets on "
        f"{calendar.month_abbr[reset.month]} {reset.day}, or sooner if it's raised."
    )
    data = waiting.document_data
    assert isinstance(data, events.EventIngestionReadyData)
    assert (data.ready_count, data.failed_count, data.waiting_count) == (0, 0, 2)

    # the reset comes: both are read, and the household hears so once
    handlers.default = None
    for job_id in (first, second):
        jobs.update(job_id, auto_retry_at=utcnow() - timedelta(seconds=1))
    assert retry_waiting(utcnow()) == 2
    run(read_both())
    [_, read] = notifications
    assert read.message.title == "Recipe cards ready"
    assert read.message.body == "2 cards that waited for the monthly limit were read. 2 cards are ready to review."


def _limit_reached_but(*read: UUID) -> Any:
    """A handler: the new month's limit runs out again, after the cards `read` were read"""

    async def handler(ctx: TaskContext) -> Any:
        if ctx.job_id in read:
            return extract_result()
        raise AIProviderLimitReachedError("every provider is over its monthly limit again")

    return handler


def _date(when: datetime) -> str:
    return f"{calendar.month_abbr[when.month]} {when.day}"


async def _queue_and_read(dispatcher: IngestDispatcher, jobs: Jobs) -> None:
    """The retry phase queues the waiting cards, then the dispatcher reads them all (a few at a time)"""
    await dispatcher.run_once()
    for _ in range(10):
        await dispatcher.run_once()
        await settle(dispatcher)
        if all(jobs.row(job_id)["status"] != IngestStatus.processing for job_id in jobs.ids):
            return
    raise AssertionError("the cards were never read")


def _batch_notified_at(jobs: Jobs) -> datetime | None:
    with session_context() as session:
        return session.execute(
            sa.select(RecipeIngestionBatch.notified_at).where(RecipeIngestionBatch.id == jobs.batch_id)
        ).scalar_one()


@pytest.mark.parametrize("queued_by", ["reset", "lift"])
def test_a_wave_tells_of_its_cards_that_still_wait(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    limit: LimitChecks,
    notifications: list[events.AIEvent],
    queued_by: str,
):
    """
    The batch was told its 3 cards wait for the monthly limit and are read when it resets. The reset comes (or the
    limit is raised): the 3 are queued at once, and the limit runs out again after the first. The wave says the first
    was read and that the 2 others still wait, with their new date, never as if every card had been read.
    """
    jobs.ready()
    retry_in = -timedelta(seconds=1) if queued_by == "reset" else timedelta(days=3)
    first, second, third = (_waiting(jobs, retry_in=retry_in) for _ in range(3))
    _notified_batch(jobs)
    limit.set(queued_by == "reset")  # a lift: the limit no longer applies
    handlers.default = _limit_reached_but(first)

    run(_queue_and_read(dispatcher, jobs))
    assert jobs.row(first)["status"] == IngestStatus.ready
    for job_id in (second, third):
        row = jobs.row(job_id)
        assert (row["status"], row["error_code"]) == (IngestStatus.failed, LIMIT)
        assert naive_utc(row["auto_retry_at"]) == next_limit_reset()

    [sent] = notifications
    assert sent.message.title == "Recipe cards ready"
    assert sent.message.body == (
        "1 card that waited for the monthly limit was read. 1 card is ready to review. 2 cards still wait for the "
        f"monthly limit. They'll be read when it resets on {_date(next_limit_reset())}, or sooner if it's raised."
    )
    data = sent.document_data
    assert isinstance(data, events.EventIngestionReadyData)
    assert (data.ready_count, data.failed_count, data.waiting_count) == (1, 0, 2)
    assert data.job_ids == [first, second, third]  # what it tells of, in capture order
    assert events.maybe_notify_batch(jobs.batch_id) is False
    assert len(notifications) == 1


def test_cards_their_reset_queued_that_all_wait_again_are_told_so(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    limit: LimitChecks,
    notifications: list[events.AIEvent],
):
    """
    The batch was told its cards would be read when the limit resets. The reset comes, and the new month's limit runs
    out before any of them is read: the household hears that they still wait, until the next reset.
    """
    first = _waiting(jobs, retry_in=-timedelta(seconds=1))
    second = _waiting(jobs, retry_in=-timedelta(seconds=1))
    _notified_batch(jobs)
    handlers.default = _limit_reached_but()

    run(_queue_and_read(dispatcher, jobs))
    for job_id in (first, second):
        row = jobs.row(job_id)
        assert (row["status"], row["error_code"]) == (IngestStatus.failed, IngestErrorCode.limit_reached)

    [sent] = notifications
    assert sent.event_type == events.AIEventTypes.recipe_ingestion_ready
    assert sent.message.title == "Recipe cards waiting"
    assert sent.message.body == (
        f"2 cards still wait for the monthly limit. They'll be read when it resets on {_date(next_limit_reset())}, "
        "or sooner if it's raised."
    )
    data = sent.document_data
    assert isinstance(data, events.EventIngestionReadyData)
    assert (data.ready_count, data.failed_count, data.waiting_count, data.job_ids) == (0, 0, 2, [first, second])
    assert _batch_notified_at(jobs) is not None  # once
    assert events.maybe_notify_batch(jobs.batch_id) is False


def test_a_card_a_lift_queued_that_fails_the_limit_again_sends_nothing(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    limit: LimitChecks,
    notifications: list[events.AIEvent],
):
    """
    A raised limit that doesn't help a card (it fails `limit_reached` again) tells nobody: its reset date hasn't moved,
    and the lift backoff reads it again and again, which would repeat the same news each time
    """
    job_id = _waiting(jobs)  # its reset is days away
    _notified_batch(jobs)
    limit.set(False)  # raised
    handlers.default = _limit_reached_but()

    run(_queue_and_read(dispatcher, jobs))
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["lift_retries"]) == (IngestStatus.failed, LIMIT, 1)
    assert notifications == []
    assert _batch_notified_at(jobs) is not None  # settled: nothing left to send for it


def test_a_card_that_fails_for_good_after_the_wait_is_told(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    limit: LimitChecks,
    notifications: list[events.AIEvent],
):
    job_id = _waiting(jobs, retry_in=-timedelta(seconds=1))
    _notified_batch(jobs)

    async def no_recipe(ctx: TaskContext) -> Any:
        raise TaskFailed(IngestErrorCode.no_recipe_found)

    handlers.default = no_recipe

    async def scenario() -> None:
        await dispatcher.run_once()
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert jobs.row(job_id)["error_code"] == IngestErrorCode.no_recipe_found
    [sent] = notifications
    assert sent.message.title == "Recipe cards not read"
    assert sent.message.body == (
        "1 card that waited for the monthly limit was read. No cards are ready to review (1 failed)."
    )


def _notify_state(batch_id: UUID) -> dict[str, Any]:
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


def _read(jobs: Jobs, job_id: UUID) -> None:
    jobs.update(
        job_id, status=IngestStatus.ready.value, task_kind=None, task_state=None, error_code=None, auto_retry_at=None
    )


def test_a_pass_with_an_older_list_doesnt_send_a_wave_again(
    monkeypatch: pytest.MonkeyPatch, jobs: Jobs, limit: LimitChecks, notifications: list[events.AIEvent]
):
    """
    Every worker process runs the retry phase: two read the waiting cards at the same time, one queues the card, which
    is read and its wave sent, and the other reaches the card later in its list. The card no longer waits, so that
    pass neither queues it nor arms its batch again: the wave isn't sent twice.
    """
    job_id = _waiting(jobs, retry_in=-timedelta(seconds=1))
    _notified_batch(jobs)
    with session_context() as session:
        older_list = IngestQueue(session).waiting_for_limit()
    assert [wait.job_id for wait in older_list] == [job_id]

    assert retry_waiting(utcnow()) == 1  # the first process
    _read(jobs, job_id)
    assert events.maybe_notify_batch(jobs.batch_id) is True
    assert len(notifications) == 1
    notified = _notify_state(jobs.batch_id)

    monkeypatch.setattr(IngestQueue, "waiting_for_limit", lambda self: older_list)
    assert retry_waiting(utcnow()) == 0  # the second, from the list it read before the card was queued
    assert _notify_state(jobs.batch_id) == notified
    assert events.maybe_notify_batch(jobs.batch_id) is False
    assert len(notifications) == 1


def test_a_card_retried_by_hand_meanwhile_doesnt_arm_its_batch(
    monkeypatch: pytest.MonkeyPatch, jobs: Jobs, limit: LimitChecks, notifications: list[events.AIEvent]
):
    """A manual retry never sends a wave: nor does a retry pass that reaches the card after it, from its list"""
    job_id = _waiting(jobs, retry_in=-timedelta(seconds=1))
    _notified_batch(jobs)
    with session_context() as session:
        older_list = IngestQueue(session).waiting_for_limit()
    notified = _notify_state(jobs.batch_id)

    # the user retries it by hand (the review page's Retry) after the pass read its list
    assert jobs.repos.jobs.enqueue_task(
        job_id,
        IngestTaskKind.extract,
        None,
        limits.PRIORITY_EXTRACT,
        where=[RecipeIngestionJob.status == IngestStatus.failed.value],
        values={"status": IngestStatus.processing.value, "error_code": None, "error_params": None},
    )
    monkeypatch.setattr(IngestQueue, "waiting_for_limit", lambda self: older_list)
    assert retry_waiting(utcnow()) == 0
    assert _notify_state(jobs.batch_id) == notified

    _read(jobs, job_id)
    assert events.maybe_notify_batch(jobs.batch_id) is False
    assert notifications == []
