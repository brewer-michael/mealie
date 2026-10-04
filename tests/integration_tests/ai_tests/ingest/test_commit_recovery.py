"""
Crash-safe commits (docs/ai/PHASE2.md §7, §18 Commit): a process dying after any step of a commit, then the commit
resumed (by `resume_stale_commits`, as the dispatcher's housekeeping runs it, or by a later request), never makes a
second recipe, food, unit or asset name, and `recipe_created` goes out exactly once. Runs on SQLite and PostgreSQL.
"""

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_jobs_api import banana_draft, fake_compute_flags, job_row, seed_job, set_columns, use_fake_flags

from mealie.core.config import get_app_dirs
from mealie.db.db_setup import session_context
from mealie.db.models.recipe.recipe import RecipeModel
from mealie.lang.providers import get_locale_provider
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.recipe_ingest import CardFlag, CommitRequest, FlagResolution, IngestStatus
from mealie.schema.response.pagination import PaginationQuery
from mealie.services.ai.ingest import commit as card_commit
from mealie.services.ai.ingest import limits, storage
from mealie.services.ai.ingest.review import JobActionError
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.event_types import EventTypes
from mealie.services.recipe.recipe_service import RecipeService
from tests.utils.fixture_schemas import TestUser


class Crash(BaseException):
    """The process dying at that point: nothing after it runs, and no handler sees it"""


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch, unique_user_fn_scoped: TestUser) -> list[str]:
    """The slug of every `recipe_created` dispatched for the test user's household"""
    slugs: list[str] = []

    def dispatch(self: EventBusService, *args: Any, **kwargs: Any) -> None:
        if (
            kwargs["event_type"] == EventTypes.recipe_created
            and str(kwargs["household_id"]) == unique_user_fn_scoped.household_id
        ):
            slugs.append(kwargs["document_data"].recipe_slug)

    monkeypatch.setattr(EventBusService, "dispatch", dispatch)
    return slugs


def ready_job(user: TestUser, **kwargs: Any) -> UUID:
    draft = banana_draft()
    flags: list[CardFlag] = [
        flag.model_copy(update={"resolution": FlagResolution.kept}) if flag.severity == "error" else flag
        for flag in fake_compute_flags(draft, None, {})
    ]
    return seed_job(user, draft=draft, flags=flags, page_count=2, **kwargs)


def run_commit(user: TestUser, job_id: UUID, version: int = 1) -> card_commit.CommitResult:
    """A commit request, as the route makes it"""
    with session_context() as session:
        repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
        private = get_repositories(session, group_id=UUID(user.group_id), household_id=None).users.get_one(user.user_id)
        return card_commit.commit_job(
            repos, private, job_id, CommitRequest(draft_version=version), translator=get_locale_provider("en-US")
        )


def after_the_lease() -> Any:
    return utcnow() + timedelta(seconds=limits.COMMIT_LEASE + 1)


def group_recipes(user: TestUser) -> list[Any]:
    with session_context() as session:
        stmt = sa.select(RecipeModel.id, RecipeModel.slug, RecipeModel.image).where(
            RecipeModel.group_id == UUID(user.group_id)
        )
        return list(session.execute(stmt).all())


def names_of(user: TestUser, kind: str) -> list[str]:
    with session_context() as session:
        repos = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        items = getattr(repos, kind).page_all(PaginationQuery(page=1, per_page=-1)).items
        return sorted(item.name for item in items)


def crash_once(target: Callable[..., Any], *, before: bool) -> Callable[..., Any]:
    """`target`, raising `Crash` before or after it the first time it's called"""
    armed = [True]

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if armed[0] and before:
            armed[0] = False
            raise Crash()
        result = target(*args, **kwargs)
        if armed[0]:
            armed[0] = False
            raise Crash()
        return result

    return wrapper


def partial_create_once(original: Callable[..., Any]) -> Callable[..., Any]:
    """`create_one` dying after the recipe's own insert, before its rating and timeline entry"""
    armed = [True]

    def create_one(self: RecipeService, create_data: Any) -> Any:
        if not armed[0]:
            return original(self, create_data)
        armed[0] = False
        data = self._recipe_creation_factory(name=create_data.name, additional_attrs=create_data.model_dump())
        self.repos.recipes.create(data)
        raise Crash()

    return create_one


CRASH_POINTS = {
    "after the claim": ("_write_files", True),
    "after the files": ("_write_files", False),
    "after foods and units were created": ("_create_recipe", True),
    "after create_one": ("_create_recipe", False),
    "after the cover key": ("_set_cover_key", False),
}


@pytest.mark.parametrize("point", list(CRASH_POINTS) + ["inside create_one"])
def test_a_crash_after_each_step_is_resumed_without_duplicates(
    point: str, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, published: list[str]
):
    user = unique_user_fn_scoped
    job_id = ready_job(user)
    if point == "inside create_one":
        monkeypatch.setattr(RecipeService, "create_one", partial_create_once(RecipeService.create_one))
    else:
        name, before = CRASH_POINTS[point]
        monkeypatch.setattr(card_commit, name, crash_once(getattr(card_commit, name), before=before))

    with pytest.raises(Crash):
        run_commit(user, job_id)
    row = job_row(job_id)
    assert row["status"] == "committing"
    recipe_id, token = row["commit_recipe_id"], row["commit_asset_token"]
    assert published == []

    # the lease holds the commit for its owner until it runs out
    card_commit.resume_stale_commits(utcnow())
    assert job_row(job_id)["commit_started_at"] == row["commit_started_at"]
    creates: list[Any] = []
    real_create = card_commit._create_recipe
    monkeypatch.setattr(card_commit, "_create_recipe", lambda *a: creates.append(a) or real_create(*a))

    assert card_commit.resume_stale_commits(after_the_lease()) >= 1
    row = job_row(job_id)
    assert row["status"] == "committed"
    assert row["recipe_id"] == recipe_id
    assert row["commit_asset_token"] == token

    recipes = group_recipes(user)
    assert [(r.id, r.slug) for r in recipes] == [(recipe_id, "banana-mug-cake")]
    assert recipes[0].image  # the cover key
    row_existed = point in ("inside create_one", "after create_one", "after the cover key")
    assert len(creates) == (0 if row_existed else 1)  # a recipe row is reused, never recreated

    assert names_of(user, "ingredient_foods") == ["coconut oil", "salt"]
    assert names_of(user, "ingredient_units") == ["tablespoon", "teaspoon"]
    asset_dir = get_app_dirs().RECIPE_DATA_DIR / str(recipe_id) / "assets"
    assert sorted(path.name for path in asset_dir.iterdir()) == [
        f"recipe-card-{token}-1.jpg",
        f"recipe-card-{token}-2.jpg",
    ]
    assert published == ["banana-mug-cake"]

    # nothing more to resume, and the event isn't sent again
    card_commit.resume_stale_commits(after_the_lease() + timedelta(minutes=5))
    assert published == ["banana-mug-cake"]
    assert len(group_recipes(user)) == 1


def test_a_crash_after_the_finish_never_publishes_twice(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, published: list[str]
):
    """At most once: a process dying right after the finish loses the event rather than doubling it"""
    user = unique_user_fn_scoped
    job_id = ready_job(user)
    monkeypatch.setattr(card_commit, "_publish_recipe_created", crash_once(lambda *a, **k: None, before=True))

    with pytest.raises(Crash):
        run_commit(user, job_id)
    assert job_row(job_id)["status"] == "committed"
    card_commit.resume_stale_commits(after_the_lease())
    assert published == []
    assert len(group_recipes(user)) == 1


def test_a_later_request_takes_over_a_stalled_commit(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, published: list[str]
):
    user = unique_user_fn_scoped
    job_id = ready_job(user)
    monkeypatch.setattr(card_commit, "_create_recipe", crash_once(card_commit._create_recipe, before=False))
    with pytest.raises(Crash):
        run_commit(user, job_id)

    # a double tap while the commit's lease runs is refused
    with pytest.raises(JobActionError) as refused:
        run_commit(user, job_id)
    assert (refused.value.status_code, refused.value.code) == (409, "invalid_status")

    set_columns(job_id, commit_started_at=utcnow() - timedelta(seconds=limits.COMMIT_LEASE + 5))
    result = run_commit(user, job_id)
    assert result.created is True  # this request won the finish
    assert result.out.slug == "banana-mug-cake"
    assert job_row(job_id)["status"] == "committed"
    assert published == ["banana-mug-cake"]

    again = run_commit(user, job_id)
    assert again.created is False
    assert again.out.recipe_id == result.out.recipe_id


def test_a_commit_whose_committer_is_gone_goes_back_to_ready(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, published: list[str]
):
    user = unique_user_fn_scoped
    job_id = ready_job(user)
    monkeypatch.setattr(card_commit, "_write_files", crash_once(card_commit._write_files, before=False))
    with pytest.raises(Crash):
        run_commit(user, job_id)
    recipe_id = job_row(job_id)["commit_recipe_id"]
    assert (get_app_dirs().RECIPE_DATA_DIR / str(recipe_id)).is_dir()

    set_columns(job_id, committed_by=uuid4())
    card_commit.resume_stale_commits(after_the_lease())

    row = job_row(job_id)
    assert (row["status"], row["error_code"]) == ("ready", "commit_interrupted")
    assert row["commit_recipe_id"] == recipe_id  # kept for the next attempt
    assert not (get_app_dirs().RECIPE_DATA_DIR / str(recipe_id)).exists()  # no recipe row has that id
    assert group_recipes(user) == []
    assert published == []

    # committed again by someone who's here
    assert run_commit(user, job_id).created is True
    assert job_row(job_id)["recipe_id"] == recipe_id


def test_a_created_recipe_is_finished_even_when_its_committer_is_gone(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, published: list[str]
):
    user = unique_user_fn_scoped
    job_id = ready_job(user)
    monkeypatch.setattr(card_commit, "_create_recipe", crash_once(card_commit._create_recipe, before=False))
    with pytest.raises(Crash):
        run_commit(user, job_id)

    set_columns(job_id, committed_by=uuid4())
    card_commit.resume_stale_commits(after_the_lease())
    assert job_row(job_id)["status"] == "committed"
    assert published == ["banana-mug-cake"]


def test_nothing_is_resumed_while_a_restore_pauses_ingestion(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    job_id = ready_job(user)
    monkeypatch.setattr(card_commit, "_create_recipe", crash_once(card_commit._create_recipe, before=True))
    with pytest.raises(Crash):
        run_commit(user, job_id)
    started = job_row(job_id)["commit_started_at"]

    marker = storage.pause_marker_path()
    marker.write_text(f"{time.time():.3f}")
    try:
        assert card_commit.resume_stale_commits(after_the_lease()) == 0  # it stops at once
    finally:
        marker.unlink(missing_ok=True)
    row = job_row(job_id)
    assert (row["status"], row["commit_started_at"]) == ("committing", started)

    card_commit.resume_stale_commits(after_the_lease())
    assert job_row(job_id)["status"] == IngestStatus.committed.value
