"""
Cards that failed because every provider was over its monthly limit (docs/ai/PHASE2.md §3.6): they're read again
automatically, at the next reset (the first of next month, UTC) or once the limit no longer applies (checked at most
every 10 minutes per group, under the card's own policy), as a manual retry would; any other failure clears the retry.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from ingest_runner_testing import FakeHandlers, Jobs, run, settle

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch
from mealie.repos.repository_recipe_ingest import IngestQueue, IngestRepos, LimitWait, naive_utc, utcnow
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.schema.household.group_events import GroupEventNotifierSave
from mealie.schema.recipe_ingest import IngestErrorCode, IngestStatus, IngestTaskState, RecipeIngestionSettingsUpdate
from mealie.services import ocr
from mealie.services.ai.errors import AIProviderLimitReachedError
from mealie.services.ai.ingest import events, limits
from mealie.services.ai.ingest.runner import retries
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.finalize import finalize_failure, next_limit_reset
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


class _Clock:
    """`time.monotonic()` as `retries` reads it, moved on by the test"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _read_and_over_the_limit_again(job_id: UUID) -> None:
    """The card a lift queued is read, and fails `limit_reached` again: finalize puts it back to waiting"""
    token = uuid4()
    with session_context() as session:
        assert IngestQueue(session).claim(job_id, token=token, owner="test", now=utcnow(), group_cap=0)
        finalize_failure(session, job_id, token, IngestErrorCode.limit_reached)


def test_a_card_a_lift_doesnt_help_is_read_again_less_and_less_often(
    jobs: Jobs, limit: LimitChecks, monkeypatch: pytest.MonkeyPatch
):
    """
    A "lifted" answer that doesn't help the card (OCR stands in for an image slot over its limit, but finds no text on
    the card, so it fails `limit_reached` again while the check keeps saying "lifted") queues it again only after
    `LIMIT_RECHECK_INTERVAL`, then twice that, and so on up to `LIFT_RETRY_MAX_WAIT`: not at every run until the reset
    """
    clock = _Clock()
    monkeypatch.setattr(retries, "time", clock)
    interval = limits.LIMIT_RECHECK_INTERVAL
    monkeypatch.setattr(retries, "LIFT_RETRY_MAX_WAIT", 3 * interval)
    job_id = _waiting(jobs)
    limit.set(False)

    assert retry_waiting(utcnow()) == 1
    for wait in (interval, 2 * interval, 3 * interval):  # doubled each time, up to the longest wait
        _read_and_over_the_limit_again(job_id)
        queued_at = clock.now
        while clock.now + limits.HOUSEKEEPING_INTERVAL < queued_at + wait:  # every run until then
            clock.now += limits.HOUSEKEEPING_INTERVAL
            assert retry_waiting(utcnow()) == 0
            assert jobs.row(job_id)["status"] == IngestStatus.failed
        clock.now = queued_at + wait
        assert retry_waiting(utcnow()) == 1
        assert _queued_again(jobs, job_id)

    # its reset has come: read again whatever the last lift said
    _read_and_over_the_limit_again(job_id)
    assert retry_waiting(utcnow() + timedelta(days=40)) == 1


def test_a_lifts_wait_goes_with_the_card_no_longer_waiting(
    jobs: Jobs, limit: LimitChecks, monkeypatch: pytest.MonkeyPatch
):
    clock = _Clock()
    monkeypatch.setattr(retries, "time", clock)
    read, waiting = _waiting(jobs), _waiting(jobs)
    limit.set(False)
    assert retry_waiting(utcnow()) == 2
    assert set(retries._lift_waits) >= {read, waiting}

    # one is read at last, the other fails again; once their waits are over, only the waiting card's is kept
    jobs.update(read, status=IngestStatus.ready.value, task_kind=None, task_state=None)
    _read_and_over_the_limit_again(waiting)
    clock.now += limits.LIMIT_RECHECK_INTERVAL
    limit.set(True)  # and the limit applies again
    assert retry_waiting(utcnow()) == 0
    assert read not in retries._lift_waits
    assert waiting in retries._lift_waits
    retries.forget_checks()
    assert retries._lift_waits == {}


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
    """The batch was sealed and its notification went out (saying its waiting cards failed)"""
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


def test_a_card_that_fails_the_limit_again_sends_nothing(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    limit: LimitChecks,
    notifications: list[events.AIEvent],
):
    job_id = _waiting(jobs, retry_in=-timedelta(seconds=1))
    _notified_batch(jobs)

    async def limit_reached(ctx: TaskContext) -> Any:
        raise AIProviderLimitReachedError("every provider is still over its monthly limit")

    handlers.default = limit_reached

    async def scenario() -> None:
        await dispatcher.run_once()
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"]) == (IngestStatus.failed, IngestErrorCode.limit_reached)
    assert notifications == []
    with session_context() as session:
        notified_at = session.execute(
            sa.select(RecipeIngestionBatch.notified_at).where(RecipeIngestionBatch.id == jobs.batch_id)
        ).scalar_one()
    assert notified_at is not None  # settled: nothing left to send for it


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
