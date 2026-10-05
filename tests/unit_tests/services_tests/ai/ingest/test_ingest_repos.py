"""
Recipe card data access (docs/ai/PHASE2.md §3, §13): household scoping, the optimistic JSON writes, the queue's
conditional updates, batches, settings and notifier options. Runs on SQLite and PostgreSQL.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import AIEventNotifierOptions, RecipeIngestionJob, RecipeIngestionSettings
from mealie.repos.repository_recipe_ingest import (
    UPDATE_RETRIES,
    CancelOutcome,
    IngestQueue,
    IngestRepos,
    JobConflict,
    LimitWait,
    enqueue_task,
    naive_utc,
    update_job_json,
    utcnow,
)
from mealie.schema.household.group_events import GroupEventNotifierSave
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe_ingest import (
    AINotifierEventsOut,
    CardDraft,
    IngestErrorCode,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
    RecipeIngestionSettingsUpdate,
)
from mealie.services.ai.ingest import limits
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

Job = RecipeIngestionJob


@pytest.fixture()
def db() -> Iterator[Session]:
    """A session of the test's own (the module-scoped `session` fixture belongs to the user fixtures)"""
    with session_context() as session:
        yield session


def _repos(db: Session, user: TestUser) -> IngestRepos:
    return IngestRepos(db, UUID(user.group_id), UUID(user.household_id))


def _page(index: int = 0) -> PageMeta:
    return PageMeta(
        index=index,
        width=1536,
        height=2048,
        view_width=1536,
        view_height=2048,
        raw_sha256="a" * 64,
        page_sha256="b" * 64,
        format="jpeg",
        raw_bytes=1000,
    )


def _job(repos: IngestRepos, batch_id: UUID | None = None, **values: Any) -> UUID:
    batch_id = batch_id or repos.batches.create(source=IngestSource.app, created_by=None)
    return repos.jobs.create(
        {
            "batch_id": batch_id,
            "position": 0,
            "source": IngestSource.app.value,
            "status": IngestStatus.processing.value,
            "pages": [_page()],
            "source_sha256": uuid4().hex * 2,
            **values,
        }
    )


def _row(db: Session, job_id: UUID) -> RowMapping:
    row = db.execute(sa.select(*Job.__table__.columns).where(Job.id == job_id)).mappings().one()
    db.commit()
    return row


def test_utcnow_is_naive_utc():
    now = utcnow()
    assert now.tzinfo is None
    assert abs(now - datetime.now(UTC).replace(tzinfo=None)) < timedelta(seconds=5)


# ==========================================
# Jobs and scoping


def test_a_job_round_trips_its_json(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    draft = CardDraft(name="Banana Mug Cake")
    job_id = _job(repos, draft=draft, task_payload={"page": 0, "target": {"field": "steps", "ref": str(uuid4())}})

    job = repos.jobs.get(job_id)
    assert job is not None
    assert job.group_id == UUID(unique_user.group_id)
    assert job.household_id == UUID(unique_user.household_id)
    assert [PageMeta.model_validate(page) for page in job.pages] == [_page()]
    assert CardDraft.model_validate(job.draft) == draft
    assert job.task_payload["page"] == 0
    assert (job.row_version, job.draft_version, job.attempts) == (0, 0, 0)
    assert job.cancel_requested is False


def test_another_households_jobs_and_batches_are_invisible(db: Session, unique_user: TestUser, h2_user: TestUser):
    mine = _repos(db, unique_user)
    theirs = _repos(db, h2_user)
    job_id = _job(mine)
    batch_id = mine.jobs.get(job_id).batch_id  # type: ignore[union-attr]

    assert theirs.jobs.get(job_id) is None
    assert theirs.batches.get(batch_id) is None
    assert theirs.jobs.update_job_json(job_id, lambda row: {"title": "stolen"}) is None
    assert not theirs.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    assert theirs.jobs.cancel_task(job_id) == CancelOutcome.idle
    assert not theirs.jobs.delete(job_id)
    assert job_id not in [job.id for job in theirs.jobs.page(per_page=-1)[0]]
    assert mine.jobs.get(job_id) is not None


def test_settings_need_only_the_group(db: Session, unique_user: TestUser):
    repos = IngestRepos(db, UUID(unique_user.group_id), None)
    assert repos.settings.get() == RecipeIngestionSettingsUpdate()
    with pytest.raises(ValueError):
        repos.jobs  # noqa: B018


def test_paging_filters_and_counts(db: Session, unique_user_fn_scoped: TestUser):
    repos = _repos(db, unique_user_fn_scoped)
    batch = repos.batches.create(source=IngestSource.app, created_by=None)
    other_batch = repos.batches.create(source=IngestSource.api, created_by=None)
    _job(repos, batch, status=IngestStatus.processing.value)
    _job(repos, batch, status=IngestStatus.ready.value, warning_count=1)
    _job(repos, batch, status=IngestStatus.ready.value)
    _job(repos, other_batch, status=IngestStatus.failed.value)
    _job(repos, other_batch, status=IngestStatus.committed.value)

    counts = repos.jobs.counts()
    assert (counts.processing, counts.ready, counts.needs_attention, counts.failed) == (1, 2, 1, 1)
    assert repos.batches.counts(batch).failed == 0

    ready, total = repos.jobs.page(statuses=[IngestStatus.ready])
    assert total == 2 and len(ready) == 2
    page, total = repos.jobs.page(batch_id=batch, page=2, per_page=2)
    assert total == 3 and len(page) == 1
    assert repos.processing_jobs_in_group() == 1


def _recipe(user: TestUser) -> UUID:
    recipe = user.repos.recipes.create(
        Recipe(name=random_string(10), user_id=user.user_id, group_id=UUID(user.group_id))
    )
    assert recipe.id is not None
    return recipe.id


def test_duplicates_and_positions(db: Session, unique_user_fn_scoped: TestUser):
    repos = _repos(db, unique_user_fn_scoped)
    batch = repos.batches.create(source=IngestSource.app, created_by=None)
    assert repos.jobs.next_position(batch) == 0
    recipe_id = _recipe(unique_user_fn_scoped)
    first = _job(
        repos, batch, source_sha256="c" * 64, position=4, status=IngestStatus.committed.value, recipe_id=recipe_id
    )
    _job(repos, batch, source_sha256="c" * 64, position=1)

    assert repos.jobs.find_duplicate("c" * 64) == first  # the oldest, committed ones included
    assert repos.jobs.find_duplicate("d" * 64) is None
    assert repos.jobs.next_position(batch) == 5
    assert [job.position for job in repos.batches.jobs(batch)] == [1, 4]


def test_a_committed_card_whose_recipe_was_deleted_is_no_duplicate(db: Session, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    repos = _repos(db, user)
    recipe_id = _recipe(user)
    committed = _job(repos, source_sha256="e" * 64, status=IngestStatus.committed.value, recipe_id=recipe_id)
    assert repos.jobs.find_duplicate("e" * 64) == committed

    user.repos.recipes.delete(recipe_id, match_key="id")
    assert repos.jobs.find_duplicate("e" * 64) is None  # the card can be scanned again

    # its new reading counts, as every job that isn't committed does, whatever its recipe link says
    again = _job(repos, source_sha256="e" * 64)
    assert repos.jobs.find_duplicate("e" * 64) == again
    for status in (IngestStatus.ready, IngestStatus.failed, IngestStatus.committing):
        other = _job(repos, source_sha256=f"{status.value:f<64}"[:64], status=status.value, recipe_id=uuid4())
        assert repos.jobs.find_duplicate(f"{status.value:f<64}"[:64]) == other


def test_cards_committed_lately_come_by_commit_time(db: Session, unique_user_fn_scoped: TestUser):
    """A card uploaded 8 days ago and added today is among the cards added this week, newest addition first"""
    repos = _repos(db, unique_user_fn_scoped)
    now = utcnow()
    old_upload = _job(
        repos,
        status=IngestStatus.committed.value,
        created_at=now - timedelta(days=8),
        committed_at=now - timedelta(minutes=5),
    )
    this_week = _job(
        repos,
        status=IngestStatus.committed.value,
        created_at=now - timedelta(days=4),
        committed_at=now - timedelta(days=3),
    )
    _job(
        repos,
        status=IngestStatus.committed.value,
        created_at=now - timedelta(days=12),
        committed_at=now - timedelta(days=9),
    )
    waiting = _job(repos, status=IngestStatus.ready.value, created_at=now)

    added, total = repos.jobs.page(committed_since=now - timedelta(days=7), order="committed")
    assert [job.id for job in added] == [old_upload, this_week]
    assert total == 2

    # an aware time is read as the moment it names
    aware = (now - timedelta(days=7)).replace(tzinfo=UTC).astimezone(timezone(timedelta(hours=-5)))
    assert [job.id for job in repos.jobs.page(committed_since=aware, order="committed")[0]] == [old_upload, this_week]

    # by commit time without the filter, cards not committed come after the others; by upload time, newest upload first
    by_commit = [job.id for job in repos.jobs.page(order="committed", per_page=-1)[0]]
    assert by_commit[:2] == [old_upload, this_week] and by_commit[-1] == waiting
    assert repos.jobs.page(per_page=-1)[0][0].id == waiting


def test_another_waiting_card_with_the_same_name(db: Session, unique_user_fn_scoped: TestUser, g2_user: TestUser):
    repos = _repos(db, unique_user_fn_scoped)
    now = utcnow()
    this = _job(repos, status=IngestStatus.ready.value, title="Banana Bread", created_at=now)
    older = _job(repos, status=IngestStatus.ready.value, title="  banana   BREAD ", created_at=now - timedelta(hours=2))
    reading = _job(
        repos, status=IngestStatus.processing.value, title="Banana Bread", created_at=now - timedelta(hours=1)
    )
    for status in (IngestStatus.committed, IngestStatus.failed, IngestStatus.committing):
        _job(repos, status=status.value, title="Banana Bread", created_at=now - timedelta(days=1))
    _job(_repos(db, g2_user), status=IngestStatus.ready.value, title="Banana Bread", created_at=now - timedelta(days=2))

    match = repos.jobs.same_title("BANANA bread", exclude_id=this)
    assert match is not None and (match.id, match.title) == (older, "  banana   BREAD ")
    assert repos.jobs.same_title("Banana Bread", exclude_id=older).id == reading  # type: ignore[union-attr]
    assert repos.jobs.same_title("Banana Bread").id == older  # type: ignore[union-attr]
    assert repos.jobs.same_title("Ｂａｎａｎａ Bread", exclude_id=this).id == older  # type: ignore[union-attr]
    assert repos.jobs.same_title("Banana Muffins", exclude_id=this) is None
    assert repos.jobs.same_title("   ", exclude_id=this) is None


def test_cards_being_read_count_per_user_across_the_group(db: Session, unique_user: TestUser, h2_user: TestUser):
    user_id, someone_else = uuid4(), uuid4()
    mine, other_household = _repos(db, unique_user), _repos(db, h2_user)
    _job(mine, created_by=user_id)
    _job(mine, created_by=user_id)
    _job(other_household, created_by=user_id)  # the same group: the quota is the group's
    _job(mine, created_by=user_id, status=IngestStatus.ready.value)
    _job(mine, created_by=someone_else)
    _job(mine, created_by=None)

    assert mine.jobs.count_processing_by_user(user_id) == 3
    assert other_household.jobs.count_processing_by_user(user_id) == 3
    assert mine.jobs.count_processing_by_user(someone_else) == 1
    assert mine.jobs.count_processing_by_user(uuid4()) == 0


def test_the_households_latest_language(db: Session, unique_user_fn_scoped: TestUser, g2_user: TestUser):
    repos = _repos(db, unique_user_fn_scoped)
    now = utcnow()
    app, api = (IngestSource.app, IngestSource.api)
    assert repos.batches.latest_locale((app, api)) is None

    german = repos.batches.create(source=app, created_by=None, locale="de-DE", now=now - timedelta(days=3))
    assert repos.batches.latest_locale((app, api)) == "de-DE"

    repos.batches.create(source=api, created_by=None, locale="fr-FR", now=now - timedelta(days=2))
    repos.batches.create(source=app, created_by=None, locale=None, now=now - timedelta(days=1))  # recorded none
    repos.batches.create(source=IngestSource.inbox, created_by=None, locale="it-IT", now=now)
    _repos(db, g2_user).batches.create(source=app, created_by=None, locale="nl-NL", now=now)
    assert repos.batches.latest_locale((app, api)) == "fr-FR"
    assert repos.batches.latest_locale((app,)) == "de-DE"
    assert repos.batches.latest_locale(()) is None

    # a later upload into an older batch makes it the latest
    assert repos.batches.touch(german, now)
    assert repos.batches.latest_locale((app, api)) == "de-DE"


# ==========================================
# update_job_json


def test_update_job_json_writes_and_bumps_the_row_version(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(repos)

    write = repos.jobs.update_job_json(job_id, lambda row: {"flags": [], "title": f"v{row['row_version']}"})

    assert write is not None
    assert write.before["row_version"] == 0
    assert write.values == {"flags": [], "title": "v0", "row_version": 1}
    row = _row(db, job_id)
    assert (row["title"], row["row_version"], row["flags"]) == ("v0", 1, [])


def test_update_job_json_retries_after_a_concurrent_write(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(repos, proposals=[])
    calls: list[int] = []

    def add_proposal(row: RowMapping) -> dict[str, Any]:
        calls.append(row["row_version"])
        if len(calls) == 1:
            # another process writes between this read and its write
            with session_context() as other:
                update_job_json(other, job_id, lambda r: {"proposals": [*r["proposals"], "theirs"]})
        return {"proposals": [*row["proposals"], "mine"]}

    write = repos.jobs.update_job_json(job_id, add_proposal)

    assert write is not None
    assert calls == [0, 1]
    row = _row(db, job_id)
    assert row["proposals"] == ["theirs", "mine"]  # neither write was lost
    assert row["row_version"] == 2


def test_update_job_json_gives_up_after_three_retries(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(repos)
    calls = 0

    def always_raced(row: RowMapping) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        with session_context() as other:
            other.execute(sa.update(Job).where(Job.id == job_id).values(row_version=Job.row_version + 1))
            other.commit()
        return {"title": "never written"}

    with pytest.raises(JobConflict):
        repos.jobs.update_job_json(job_id, always_raced)

    assert calls == UPDATE_RETRIES + 1
    assert _row(db, job_id)["title"] is None


def test_update_job_json_writes_nothing_when_its_conditions_fail(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(repos, draft_version=3)

    assert repos.jobs.update_job_json(job_id, lambda row: {"title": "x"}, where=[Job.draft_version == 2]) is None
    assert repos.jobs.update_job_json(job_id, lambda row: None) is None
    assert repos.jobs.update_job_json(uuid4(), lambda row: {"title": "x"}) is None
    row = _row(db, job_id)
    assert (row["title"], row["row_version"]) == (None, 0)
    assert not db.in_transaction()


def test_a_failing_mutation_writes_nothing_and_leaves_no_transaction_open(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(repos)

    def invalid(row: RowMapping) -> dict[str, Any]:
        raise ValueError("the draft doesn't validate")

    with pytest.raises(ValueError):
        repos.jobs.update_job_json(job_id, invalid)
    assert not db.in_transaction()
    assert _row(db, job_id)["row_version"] == 0


def test_update_job_json_stops_when_the_fence_fails_on_a_retry(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(repos, draft_version=1)
    calls = 0

    def edited_meanwhile(row: RowMapping) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        with session_context() as other:
            update_job_json(other, job_id, lambda r: {"draft_version": 2})
        return {"title": "stale"}

    assert repos.jobs.update_job_json(job_id, edited_meanwhile, where=[Job.draft_version == 1]) is None
    assert calls == 1


# ==========================================
# Tasks


def test_a_new_task_starts_with_clean_counters(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(
        repos,
        status=IngestStatus.failed.value,
        error_code=IngestErrorCode.rate_limited.value,
        attempts=3,
        rate_limit_retries=6,
        not_before=utcnow() + timedelta(hours=1),
        cancel_requested=True,
    )

    assert repos.jobs.enqueue_task(
        job_id,
        IngestTaskKind.extract,
        None,
        limits.PRIORITY_EXTRACT,
        where=[Job.status == IngestStatus.failed.value],
        values={"status": IngestStatus.processing.value, "error_code": None, "error_params": None},
    )

    row = _row(db, job_id)
    assert row["task_state"] == IngestTaskState.queued.value
    assert row["task_kind"] == IngestTaskKind.extract.value
    assert row["task_priority"] == limits.PRIORITY_EXTRACT
    assert (row["attempts"], row["rate_limit_retries"], row["not_before"], row["cancel_requested"]) == (
        0,
        0,
        None,
        False,
    )
    assert (row["status"], row["error_code"]) == (IngestStatus.processing.value, None)
    assert row["row_version"] == 1  # the status and error changed

    # a job has at most one task
    assert not repos.jobs.enqueue_task(job_id, IngestTaskKind.reread, {"page": 0}, limits.PRIORITY_REREAD)


def test_enqueueing_without_versioned_columns_leaves_the_row_version(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(repos, status=IngestStatus.ready.value)
    assert enqueue_task(
        db,
        job_id,
        UUID(unique_user.household_id),
        IngestTaskKind.reread,
        {"page": 0, "x": 0.1},
        limits.PRIORITY_REREAD,
    )
    row = _row(db, job_id)
    assert (row["row_version"], row["task_payload"], row["task_priority"]) == (0, {"page": 0, "x": 0.1}, 0)


def test_cancelling(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    queue = IngestQueue(db)
    now = utcnow()

    processing = _job(repos)
    repos.jobs.enqueue_task(processing, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    assert repos.jobs.cancel_task(processing) == CancelOutcome.cancelled
    row = _row(db, processing)
    assert (row["status"], row["error_code"], row["task_state"]) == ("failed", "cancelled", None)

    ready = _job(repos, status=IngestStatus.ready.value)
    repos.jobs.enqueue_task(ready, IngestTaskKind.reread, {"page": 0}, limits.PRIORITY_REREAD)
    assert repos.jobs.cancel_task(ready) == CancelOutcome.cancelled
    row = _row(db, ready)
    assert (row["status"], row["error_code"], row["task_state"], row["task_payload"]) == ("ready", None, None, None)

    running = _job(repos)
    repos.jobs.enqueue_task(running, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    assert queue.claim(running, token=uuid4(), owner="test", now=now)
    assert repos.jobs.cancel_task(running) == CancelOutcome.requested
    assert _row(db, running)["cancel_requested"] is True

    assert repos.jobs.cancel_task(ready) == CancelOutcome.idle


def test_a_cancelled_retry_no_longer_waits_for_the_monthly_limit(db: Session, unique_user: TestUser):
    """A card retried by hand while waiting for the limit, then cancelled, fails `cancelled`: it isn't retried later"""
    repos = _repos(db, unique_user)
    job_id = _job(repos, auto_retry_at=utcnow() + timedelta(days=3), lift_retries=2, lift_retry_at=utcnow())
    repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    assert repos.jobs.cancel_task(job_id) == CancelOutcome.cancelled
    row = _row(db, job_id)
    assert (row["status"], row["error_code"], row["auto_retry_at"]) == ("failed", "cancelled", None)
    assert (row["lift_retries"], row["lift_retry_at"]) == (0, None)  # nor does its lift backoff stay


def test_cards_waiting_for_the_monthly_limit_are_read_again_once(db: Session, unique_user_fn_scoped: TestUser):
    repos = _repos(db, unique_user_fn_scoped)
    queue = IngestQueue(db)
    now = utcnow()
    limit = IngestErrorCode.limit_reached.value
    soon = _job(repos, status="failed", error_code=limit, auto_retry_at=now + timedelta(days=1), local_only=True)
    later = _job(repos, status="failed", error_code=limit, auto_retry_at=now + timedelta(days=9))
    not_waiting = [
        _job(repos, status="failed", error_code=limit),  # failed before automatic retries existed
        _job(repos, status="failed", error_code="provider_failed", auto_retry_at=now),
        _job(repos, status="ready", error_code=limit, auto_retry_at=now),  # a re-extract's banner
        _job(repos, status="failed", error_code=limit, auto_retry_at=now, task_state="queued"),  # retried by hand
    ]

    waiting = [wait for wait in queue.waiting_for_limit() if wait.job_id in {soon, later, *not_waiting}]
    assert [wait.job_id for wait in waiting] == [soon, later]
    assert (waiting[0].group_id, waiting[0].household_id) == (repos.group_id, repos.household_id)
    assert waiting[0].local_only and not waiting[1].local_only
    assert abs(waiting[0].auto_retry_at - (now + timedelta(days=1))) < timedelta(seconds=1)
    assert (waiting[0].lift_retries, waiting[0].lift_retry_at) == (0, None)
    soon_wait, later_wait = waiting

    assert not queue.retry_after_limit(soon_wait, now)  # its reset hasn't come
    reset = now + timedelta(days=1, seconds=1)
    assert queue.retry_after_limit(soon_wait, reset)
    row = _row(db, soon)
    assert (row["status"], row["error_code"], row["error_params"], row["auto_retry_at"]) == (
        "processing",
        None,
        None,
        None,
    )
    assert (row["task_kind"], row["task_state"], row["task_priority"], row["attempts"]) == (
        "extract",
        "queued",
        limits.PRIORITY_EXTRACT,
        0,
    )
    assert not queue.retry_after_limit(soon_wait, reset)
    later_reset = now + timedelta(days=10)
    assert not queue.retry_after_limit(replace(later_wait, household_id=uuid4()), later_reset)  # matches nothing
    for job_id in not_waiting:
        stale = replace(later_wait, job_id=job_id)
        assert not queue.retry_after_limit(stale, later_reset)
        assert not queue.retry_after_limit(stale, later_reset, next_lift_at=later_reset)
    assert soon not in [wait.job_id for wait in queue.waiting_for_limit()]


def test_a_lift_queues_a_waiting_card_once_the_last_lifts_wait_is_over(db: Session, unique_user_fn_scoped: TestUser):
    """
    A "lifted" limit that queued a card which then failed `limit_reached` again may queue it again only once the wait
    it set is over, and only if no other lift queued it since the waiting cards were read (another worker process);
    its reset queues it whatever the wait, and starts the backoff over
    """
    repos = _repos(db, unique_user_fn_scoped)
    queue = IngestQueue(db)
    now = utcnow()
    limit = IngestErrorCode.limit_reached.value
    job_id = _job(repos, status="failed", error_code=limit, auto_retry_at=now + timedelta(days=9))

    def wait_of() -> LimitWait:
        [wait] = [wait for wait in queue.waiting_for_limit() if wait.job_id == job_id]
        return wait

    def fails_the_limit_again() -> None:
        """Read, and over the limit again: waiting, its backoff kept (`finalize_failure`)"""
        db.execute(
            sa.update(Job)
            .where(Job.id == job_id)
            .values(status="failed", error_code=limit, auto_retry_at=now + timedelta(days=9), task_state=None)
        )
        db.commit()

    first_wait = wait_of()
    assert queue.retry_after_limit(first_wait, now, next_lift_at=now + timedelta(minutes=10))
    row = _row(db, job_id)
    assert (row["status"], row["lift_retries"]) == ("processing", 1)
    assert naive_utc(row["lift_retry_at"]) == now + timedelta(minutes=10)

    fails_the_limit_again()
    wait = wait_of()
    assert (wait.lift_retries, wait.lift_retry_at) == (1, now + timedelta(minutes=10))
    assert not queue.retry_after_limit(wait, now + timedelta(minutes=5), next_lift_at=now + timedelta(minutes=25))
    # another process's pass, from a list read before the first lift: that lift's count is gone
    assert not queue.retry_after_limit(first_wait, now + timedelta(minutes=11), next_lift_at=now + timedelta(hours=1))
    assert queue.retry_after_limit(wait, now + timedelta(minutes=11), next_lift_at=now + timedelta(minutes=31))
    row = _row(db, job_id)
    assert (row["lift_retries"], naive_utc(row["lift_retry_at"])) == (2, now + timedelta(minutes=31))

    # its reset comes: read again whatever the wait, which starts over
    fails_the_limit_again()
    assert queue.retry_after_limit(wait_of(), now + timedelta(days=9, seconds=1))
    row = _row(db, job_id)
    assert (row["status"], row["lift_retries"], row["lift_retry_at"]) == ("processing", 0, None)


# ==========================================
# The queue


def test_queued_tasks_come_by_priority_then_age_once_due(db: Session, unique_user_fn_scoped: TestUser):
    repos = _repos(db, unique_user_fn_scoped)
    queue = IngestQueue(db)
    now = utcnow()
    extract_old = _job(repos, created_at=now - timedelta(minutes=3))
    extract_new = _job(repos, created_at=now - timedelta(minutes=1))
    reread = _job(repos, status=IngestStatus.ready.value, created_at=now)
    later = _job(repos, created_at=now - timedelta(minutes=5))
    for job_id in (extract_old, extract_new, later):
        repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    repos.jobs.enqueue_task(reread, IngestTaskKind.reread, {"page": 0}, limits.PRIORITY_REREAD)
    db.execute(sa.update(Job).where(Job.id == later).values(not_before=now + timedelta(minutes=1)))
    db.commit()

    mine = {extract_old, extract_new, reread, later}
    queued = [job_id for job_id in queue.queued_ids(now, 100) if job_id in mine]
    assert queued == [reread, extract_old, extract_new]
    assert [job_id for job_id in queue.queued_ids(now, 100, max_priority=limits.PRIORITY_REREAD) if job_id in mine] == [
        reread
    ]
    assert later in queue.queued_ids(now + timedelta(minutes=2), 100)
    assert queue.queued_ids(now, 0) == []


def test_a_task_is_claimed_once(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    job_id = _job(repos)
    repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    now = utcnow()
    first, second = uuid4(), uuid4()

    with session_context() as other:
        assert IngestQueue(db).claim(job_id, token=first, owner="host:1:a", now=now)
        assert not IngestQueue(other).claim(job_id, token=second, owner="host:2:b", now=now)

    row = _row(db, job_id)
    assert row["lease_token"] == first
    assert row["task_state"] == IngestTaskState.running.value
    assert row["attempts"] == 1
    assert row["lease_owner"] == "host:1:a"
    assert row["lease_expires_at"].replace(tzinfo=None) == now + timedelta(seconds=limits.LEASE)


def test_heartbeats_renew_leases_and_report_cancelled_and_vanished_tokens(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    queue = IngestQueue(db)
    now = utcnow()
    tokens = {}
    for name in ("live", "cancelled", "vanished"):
        job_id = _job(repos)
        repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
        tokens[name] = (job_id, uuid4())
        assert queue.claim(job_id, token=tokens[name][1], owner="test", now=now)

    repos.jobs.cancel_task(tokens["cancelled"][0])
    repos.jobs.delete(tokens["vanished"][0])

    later = now + timedelta(seconds=60)
    held = queue.heartbeat([token for _, token in tokens.values()], later)

    assert held == {tokens["live"][1]: False, tokens["cancelled"][1]: True}
    expires = _row(db, tokens["live"][0])["lease_expires_at"].replace(tzinfo=None)
    assert expires == later + timedelta(seconds=limits.LEASE)
    assert queue.heartbeat([], later) == {}


def test_progress_is_fenced_on_the_lease(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    queue = IngestQueue(db)
    job_id = _job(repos)
    repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    token = uuid4()
    queue.claim(job_id, token=token, owner="test", now=utcnow())

    assert queue.set_progress(job_id, token, "recipe-ingest.progress.reading-card")
    assert not queue.set_progress(job_id, uuid4(), "recipe-ingest.progress.structuring")
    assert _row(db, job_id)["progress_key"] == "recipe-ingest.progress.reading-card"


def test_expired_leases_requeue_while_attempts_remain(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    queue = IngestQueue(db)
    now = utcnow()
    job_id = _job(repos)
    repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)

    for attempt in range(1, limits.MAX_ATTEMPTS + 1):
        token = uuid4()
        assert queue.claim(job_id, token=token, owner="test", now=now)
        after_lease = now + timedelta(seconds=limits.LEASE + 1)
        assert not queue.requeue_expired(job_id, token, now)  # not expired yet
        expired = [lease for lease in queue.expired(after_lease) if lease.job_id == job_id]
        assert [(lease.token, lease.attempts, lease.status) for lease in expired] == [
            (token, attempt, IngestStatus.processing)
        ]
        requeued = queue.requeue_expired(job_id, token, after_lease)
        assert requeued is (attempt < limits.MAX_ATTEMPTS)

    # the poison guard (the runner's) decides what happens after the last attempt
    row = _row(db, job_id)
    assert (row["task_state"], row["attempts"]) == (IngestTaskState.running.value, limits.MAX_ATTEMPTS)


def test_releasing_a_task_gives_back_its_attempt(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    queue = IngestQueue(db)
    now = utcnow()
    job_id = _job(repos)
    repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    token = uuid4()
    queue.claim(job_id, token=token, owner="test", now=now)

    assert not queue.release(job_id, uuid4())  # fenced on the token
    assert queue.release(job_id, token, not_before=now + timedelta(seconds=limits.PAUSED_RELEASE_DELAY))

    row = _row(db, job_id)
    assert (row["task_state"], row["attempts"], row["lease_token"]) == (IngestTaskState.queued.value, 0, None)
    assert row["not_before"].replace(tzinfo=None) == now + timedelta(seconds=limits.PAUSED_RELEASE_DELAY)
    assert job_id not in queue.queued_ids(now, 100)


def test_a_fenced_write_is_dropped_once_the_lease_moved_on(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    queue = IngestQueue(db)
    job_id = _job(repos)
    repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT)
    stale, current = uuid4(), uuid4()
    queue.claim(job_id, token=stale, owner="test", now=utcnow())
    queue.release(job_id, stale)
    queue.claim(job_id, token=current, owner="test", now=utcnow())

    assert queue.update_job_json(job_id, lambda row: {"title": "stale"}, where=IngestQueue.fence(stale)) is None
    assert queue.update_job_json(job_id, lambda row: {"title": "fresh"}, where=IngestQueue.fence(current))
    assert _row(db, job_id)["title"] == "fresh"


# ==========================================
# Batches


def test_a_sealed_batch_takes_no_more_uploads(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    now = utcnow()
    batch_id = repos.batches.create(source=IngestSource.app, created_by=UUID(str(unique_user.user_id)), now=now)

    assert repos.batches.touch(batch_id, now + timedelta(seconds=5))
    assert repos.batches.seal(batch_id, now + timedelta(seconds=10))
    assert not repos.batches.seal(batch_id, now + timedelta(seconds=11))
    assert not repos.batches.touch(batch_id, now + timedelta(seconds=12))

    batch = repos.batches.get(batch_id)
    assert batch is not None
    assert batch.sealed_at.replace(tzinfo=None) == now + timedelta(seconds=10)
    assert batch.last_upload_at.replace(tzinfo=None) == now + timedelta(seconds=5)


def test_only_idle_batches_seal_when_asked_to(db: Session, unique_user: TestUser):
    repos = _repos(db, unique_user)
    now = utcnow()
    batch_id = repos.batches.create(source=IngestSource.api, created_by=None, now=now)
    cutoff = now - timedelta(seconds=limits.AUTO_BATCH_IDLE)

    assert not repos.batches.seal(batch_id, now, idle_before=cutoff)
    assert repos.batches.seal(batch_id, now + timedelta(minutes=3), idle_before=now + timedelta(minutes=1))


def test_uploads_find_the_batch_to_join(db: Session, unique_user_fn_scoped: TestUser):
    repos = _repos(db, unique_user_fn_scoped)
    user_id = UUID(str(unique_user_fn_scoped.user_id))
    now = utcnow()
    since = now - timedelta(seconds=limits.AUTO_BATCH_IDLE)
    api = repos.batches.create(source=IngestSource.api, created_by=user_id, now=now - timedelta(seconds=30))
    newer = repos.batches.create(source=IngestSource.api, created_by=user_id, now=now - timedelta(seconds=10))
    repos.batches.create(source=IngestSource.api, created_by=user_id, now=now - timedelta(minutes=5))  # idle
    inbox = repos.batches.create(source=IngestSource.inbox, created_by=None, source_key="home/family", now=now)

    assert repos.batches.find_open(
        source=IngestSource.api, created_by=user_id, source_key=None, active_since=since
    ) == (newer)
    repos.batches.seal(newer, now)
    assert repos.batches.find_open(
        source=IngestSource.api, created_by=user_id, source_key=None, active_since=since
    ) == (api)
    assert (
        repos.batches.find_open(source=IngestSource.api, created_by=uuid4(), source_key=None, active_since=since)
        is None
    )
    assert (
        repos.batches.find_open(
            source=IngestSource.inbox, created_by=None, source_key="home/family", active_since=since
        )
        == inbox
    )
    assert (
        repos.batches.find_open(source=IngestSource.inbox, created_by=None, source_key="home/other", active_since=since)
        is None
    )


# ==========================================
# Settings and notifier options


def test_settings_default_without_a_row_and_upsert(db: Session, unique_user_fn_scoped: TestUser):
    repos = _repos(db, unique_user_fn_scoped)
    assert repos.settings.get() == RecipeIngestionSettingsUpdate(local_only=False, cross_read=False)

    repos.settings.upsert(RecipeIngestionSettingsUpdate(local_only=True, cross_read=False))
    assert repos.settings.get() == RecipeIngestionSettingsUpdate(local_only=True, cross_read=False)
    repos.settings.upsert(RecipeIngestionSettingsUpdate(local_only=True, cross_read=True))
    assert repos.settings.get() == RecipeIngestionSettingsUpdate(local_only=True, cross_read=True)

    count = db.execute(
        sa.select(sa.func.count())
        .select_from(RecipeIngestionSettings)
        .where(RecipeIngestionSettings.group_id == UUID(unique_user_fn_scoped.group_id))
    ).scalar_one()
    assert count == 1


def test_notifier_options(db: Session, unique_user: TestUser, h2_user: TestUser):
    repos = _repos(db, unique_user)

    def notifier(name: str, url: str) -> UUID:
        saved = unique_user.repos.group_event_notifier.create(
            GroupEventNotifierSave(
                name=name, apprise_url=url, group_id=unique_user.group_id, household_id=unique_user.household_id
            )
        )
        return saved.id

    on, off = notifier("HA", "jsons://ha.local/api/webhook/x"), notifier("Mail", "mailto://me")

    assert repos.notifier_options.get(on) == AINotifierEventsOut(recipe_ingestion_ready=False)
    assert repos.notifier_options.set(on, recipe_ingestion_ready=True) == AINotifierEventsOut(
        recipe_ingestion_ready=True
    )
    assert repos.notifier_options.set(off, recipe_ingestion_ready=False) is not None
    assert repos.notifier_options.get(on) == AINotifierEventsOut(recipe_ingestion_ready=True)
    assert on in repos.notifier_options.enabled_notifier_ids()
    assert off not in repos.notifier_options.enabled_notifier_ids()

    # another household's notifier isn't reachable
    assert _repos(db, h2_user).notifier_options.get(on) is None
    assert _repos(db, h2_user).notifier_options.set(on, recipe_ingestion_ready=False) is None

    # deleting the notifier deletes its options
    unique_user.repos.group_event_notifier.delete(on)
    left = db.execute(
        sa.select(sa.func.count()).select_from(AIEventNotifierOptions).where(AIEventNotifierOptions.notifier_id == on)
    ).scalar_one()
    assert left == 0
