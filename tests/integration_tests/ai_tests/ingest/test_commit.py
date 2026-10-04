"""
Committing a reviewed card (docs/ai/PHASE2.md §7, §18 Commit): the recipe a draft becomes, its card assets and cover,
re-linked foods and units, organizers looked up in the group, kept markers, the household's settings, the event, double
commits and refusals. Crash recovery is in `test_commit_recovery.py`. Runs on SQLite and PostgreSQL.
"""

import fcntl
import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import ExifTags, Image
from test_jobs_api import (
    assert_code,
    banana_draft,
    fake_compute_flags,
    household_member,
    job_row,
    job_url,
    seed_job,
    use_fake_flags,
)

from mealie.core.config import get_app_dirs
from mealie.db.db_setup import session_context
from mealie.lang.providers import get_locale_provider
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.recipe.recipe_category import TagSave
from mealie.schema.recipe.recipe_ingredient import SaveIngredientFood, SaveIngredientUnit
from mealie.schema.recipe_ingest import (
    CardDraftIngredient,
    CardDraftNote,
    CardDraftRef,
    CardDraftStep,
    CardFlag,
    FlagResolution,
    IngestSource,
    IngestStatus,
)
from mealie.schema.response.pagination import PaginationQuery
from mealie.services.ai.ingest import commit as card_commit
from mealie.services.ai.ingest import storage
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.event_types import EventTypes
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `recipe_created` dispatched"""
    events: list[dict[str, Any]] = []

    def dispatch(
        self: EventBusService,
        integration_id: str,
        group_id: Any,
        household_id: Any,
        event_type: Any,
        document_data: Any,
        message: str = "",
    ) -> None:
        if event_type == EventTypes.recipe_created:
            events.append({"slug": document_data.recipe_slug, "household_id": household_id, "message": message})

    monkeypatch.setattr(EventBusService, "dispatch", dispatch)
    return events


def kept(flags: list[CardFlag]) -> list[CardFlag]:
    """The flags with every error kept as written"""
    return [
        flag.model_copy(update={"resolution": FlagResolution.kept}) if flag.severity == "error" else flag
        for flag in flags
    ]


def ready_to_commit(user: TestUser, **kwargs: Any) -> UUID:
    """A ready job whose errors were all kept as written"""
    draft = kwargs.pop("draft", None) or banana_draft()
    return seed_job(user, draft=draft, flags=kept(fake_compute_flags(draft, None, {})), **kwargs)


def commit(api_client: TestClient, user: TestUser, job_id: UUID, version: int = 1, **body: Any):
    return api_client.post(job_url(job_id, "commit"), json={"draftVersion": version, **body}, headers=user.token)


def recipe_of(api_client: TestClient, user: TestUser, slug: str) -> dict[str, Any]:
    response = api_client.get(api_routes.recipes_slug(slug), headers=user.token)
    assert response.status_code == 200, response.text
    return response.json()


def recipe_dir(recipe_id: UUID | str) -> Any:
    return get_app_dirs().RECIPE_DATA_DIR / str(recipe_id)


def foods_named(user: TestUser, name: str) -> list[Any]:
    with session_context() as session:
        repos = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        return [food for food in repos.ingredient_foods.page_all(_all()).items if food.name == name]


def units_named(user: TestUser, name: str) -> list[Any]:
    with session_context() as session:
        repos = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        return [unit for unit in repos.ingredient_units.page_all(_all()).items if unit.name == name]


def _all() -> PaginationQuery:
    return PaginationQuery(page=1, per_page=-1)


# ==================================================================================================================
# The recipe


def test_commit_creates_the_recipe(api_client: TestClient, unique_user_fn_scoped: TestUser, published: list):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user, page_count=2)

    response = commit(api_client, user, job_id)
    assert response.status_code == 201, response.text
    out = response.json()
    assert out["slug"] == "banana-mug-cake"
    assert out["nextJobId"] is None
    assert out["warnings"] == []
    recipe_id = UUID(out["recipeId"])

    row = job_row(job_id)
    assert row["status"] == "committed"
    assert row["recipe_id"] == row["commit_recipe_id"] == recipe_id
    assert row["committed_by"] == user.user_id
    assert row["committed_at"] is not None
    token = row["commit_asset_token"]
    assert len(token) == 22

    recipe = recipe_of(api_client, user, out["slug"])
    assert recipe["id"] == str(recipe_id)
    assert recipe["name"] == "Banana Mug Cake"
    assert recipe["description"] == "A quick cake"
    assert recipe["recipeYield"] == "1 mug"
    assert recipe["prepTime"] == "5 minutes"
    assert recipe["userId"] == str(user.user_id)
    assert recipe["householdId"] == user.household_id

    # the kept blank, and the attribution as a note titled "From", first
    assert [step["text"] for step in recipe["recipeInstructions"]] == [
        "Mash the banana in a mug and stir in everything else.",
        "Microwave for ___ minutes.",
    ]
    assert recipe["notes"][0]["title"] == "From"
    assert recipe["notes"][0]["text"] == "Grandma Jo"  # not "From: From Grandma Jo"

    # foods and units created (the committer can organize) and linked
    lines = recipe["recipeIngredient"]
    assert [(line["quantity"], line["unit"]["name"], line["food"]["name"], line["note"]) for line in lines] == [
        (1, "tablespoon", "coconut oil", "melted"),
        (0.25, "teaspoon", "salt", ""),
    ]
    assert lines[0]["originalText"] == "1 T. coconut oil (melted)"
    assert len(foods_named(user, "coconut oil")) == 1

    # the card as assets, named with the stored token, and as the cover
    assert [(a["name"], a["icon"], a["fileName"]) for a in recipe["assets"]] == [
        ("Recipe card", "mdi-file-image", f"recipe-card-{token}-1.jpg"),
        ("Recipe card (back)", "mdi-file-image", f"recipe-card-{token}-2.jpg"),
    ]
    assert recipe["settings"]["showAssets"] is True
    assert recipe["image"]
    assert (recipe_dir(recipe_id) / "images" / "original.webp").is_file()

    assets = sorted(path.name for path in (recipe_dir(recipe_id) / "assets").iterdir())
    assert assets == [f"recipe-card-{token}-1.jpg", f"recipe-card-{token}-2.jpg"]
    for name in assets:
        with Image.open(recipe_dir(recipe_id) / "assets" / name) as image:
            assert image.format == "JPEG"
            assert not image.getexif()
            assert not image.getexif().get_ifd(ExifTags.IFD.GPSInfo)
            assert "exif" not in image.info

    assert published == [
        {"slug": "banana-mug-cake", "household_id": UUID(user.household_id), "message": published[0]["message"]}
    ]
    assert "Banana Mug Cake" in published[0]["message"]


@pytest.mark.parametrize(
    "attribution, text",
    [
        ("From Grandma Jo", "Grandma Jo"),
        ("from: Aunt May's kitchen", "Aunt May's kitchen"),
        ("FROM:Aunt May", "Aunt May"),
        ("Fromage Farm newsletter", "Fromage Farm newsletter"),
        ("Grandma Jo", "Grandma Jo"),
        ("From:", None),
    ],
)
def test_the_attribution_note_doesnt_repeat_its_title(
    api_client: TestClient, unique_user_fn_scoped: TestUser, attribution: str, text: str | None
):
    # the note is titled "From" already, so the card's own "From" isn't repeated in its text
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user, draft=banana_draft(attribution=attribution))
    response = commit(api_client, user, job_id)
    assert response.status_code == 201, response.text
    notes = recipe_of(api_client, user, response.json()["slug"])["notes"]
    assert [(note["title"], note["text"]) for note in notes] == ([("From", text)] if text else [])


def test_the_recipe_takes_only_the_drafts_fields(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    out = commit(api_client, user, job_id).json()
    recipe = recipe_of(api_client, user, out["slug"])

    assert recipe["rating"] is None
    assert recipe["extras"] == {}
    assert recipe["orgURL"] is None
    assert recipe["nutrition"] is None or not any(recipe["nutrition"].values())
    assert recipe["tags"] == recipe["recipeCategory"] == recipe["tools"] == []


def test_settings_come_from_the_household_with_assets_shown(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    preferences = api_client.get(api_routes.households_preferences, headers=user.token).json()
    preferences.update({"recipePublic": False, "recipeShowAssets": False, "recipeLandscapeView": True})
    assert api_client.put(api_routes.households_preferences, json=preferences, headers=user.token).status_code == 200

    out = commit(api_client, user, ready_to_commit(user)).json()
    settings = recipe_of(api_client, user, out["slug"])["settings"]
    assert settings["public"] is False
    assert settings["showAssets"] is True
    assert settings["landscapeView"] is True

    preferences["recipePublic"] = True
    assert api_client.put(api_routes.households_preferences, json=preferences, headers=user.token).status_code == 200
    out = commit(api_client, user, ready_to_commit(user)).json()
    assert recipe_of(api_client, user, out["slug"])["settings"]["public"] is True


def test_no_cover_when_the_draft_says_so(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    out = commit(api_client, user, ready_to_commit(user, draft=banana_draft(use_card_as_cover=False))).json()
    recipe = recipe_of(api_client, user, out["slug"])
    assert recipe["image"] is None
    assert not (recipe_dir(out["recipeId"]) / "images" / "original.webp").exists()
    assert len(recipe["assets"]) == 1


def test_keep_as_written_conversions(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    draft = banana_draft(
        name="Grandma's [illegible] Cake",
        prep_time="[blank] minutes",
        ingredients=[CardDraftIngredient(original_text="1 [illegible] flour", note="1 [illegible] flour")],
        notes=[CardDraftNote(title="Tip", text="Use [illegible] bananas")],
    )
    out = commit(api_client, user, ready_to_commit(user, draft=draft)).json()
    recipe = recipe_of(api_client, user, out["slug"])

    assert recipe["name"] == "Grandma's (unreadable) Cake"
    assert recipe["prepTime"] == "___ minutes"
    assert recipe["recipeIngredient"][0]["note"] == "1 (unreadable) flour"
    assert recipe["recipeIngredient"][0]["food"] is None
    assert recipe["recipeInstructions"][1]["text"] == "Microwave for ___ minutes."
    assert recipe["notes"][1] == {**recipe["notes"][1], "title": "Tip", "text": "Use (unreadable) bananas"}


@pytest.mark.parametrize("locale", ["de-DE", "en-GB"])
def test_a_language_without_the_forks_texts_gets_them_in_english(
    api_client: TestClient, unique_user_fn_scoped: TestUser, locale: str
):
    """Only en-US carries the fork's texts: a commit in another language writes them in English, never their keys"""
    user = unique_user_fn_scoped
    draft = banana_draft(steps=[CardDraftStep(text="Bake at [illegible] degrees.")])
    job_id = ready_to_commit(user, draft=draft, locale=locale)

    response = api_client.post(
        job_url(job_id, "commit"), json={"draftVersion": 1}, headers={**user.token, "Accept-Language": locale}
    )
    assert response.status_code == 201, response.text
    recipe = recipe_of(api_client, user, response.json()["slug"])
    written = [
        recipe["notes"][0]["title"],
        recipe["recipeInstructions"][0]["text"],
        *(asset["name"] for asset in recipe["assets"]),
    ]
    assert not any("recipe-ingest" in text for text in written), written
    if get_locale_provider(locale).t("recipe-ingest.note-from") == "recipe-ingest.note-from":
        assert written == ["From", "Bake at (unreadable) degrees.", "Recipe card"]


# ==================================================================================================================
# Linking at commit


def test_foreign_and_unknown_ids_are_dropped(
    api_client: TestClient, unique_user_fn_scoped: TestUser, unique_user: TestUser
):
    user, stranger = unique_user_fn_scoped, unique_user
    with session_context() as session:
        theirs = get_repositories(session, group_id=UUID(stranger.group_id), household_id=None)
        foreign_food = theirs.ingredient_foods.create(
            SaveIngredientFood(name="coconut oil", group_id=stranger.group_id)
        )
        foreign_unit = theirs.ingredient_units.create(SaveIngredientUnit(name="tablespoon", group_id=stranger.group_id))
        foreign_tag = theirs.tags.create(TagSave(name="Dessert", group_id=stranger.group_id))

        ours = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        our_food = ours.ingredient_foods.create(SaveIngredientFood(name="banana", group_id=user.group_id))
        our_unit = ours.ingredient_units.create(
            SaveIngredientUnit(name="teaspoon", abbreviation="tsp", group_id=user.group_id)
        )
        our_tag = ours.tags.create(TagSave(name="Quick", group_id=user.group_id))

    draft = banana_draft(
        ingredients=[
            CardDraftIngredient(
                original_text="1 T. coconut oil",
                quantity=1,
                unit=CardDraftRef(id=foreign_unit.id, name="tablespoon"),
                food=CardDraftRef(id=foreign_food.id, name="coconut oil"),
            ),
            CardDraftIngredient(
                original_text="1 banana",
                quantity=1,
                food=CardDraftRef(id=our_food.id, name="whatever the draft says"),
            ),
            # names matched exactly again: an abbreviation finds the unit, a food made since extraction is found
            CardDraftIngredient(
                original_text="1/2 t. banana",
                quantity=0.5,
                unit=CardDraftRef(name="tsp"),
                food=CardDraftRef(name="Banana"),
            ),
            CardDraftIngredient(original_text="1 pinch", quantity=1, food=CardDraftRef(id=uuid4(), name="")),
        ],
        tags=[CardDraftRef(id=foreign_tag.id, name="Dessert"), CardDraftRef(id=our_tag.id, name="Quick")],
        categories=[CardDraftRef(name="No id")],
        tools=[CardDraftRef(id=uuid4(), name="Mug")],
    )
    response = commit(api_client, user, ready_to_commit(user, draft=draft))
    assert response.status_code == 201, response.text
    out = response.json()
    assert sorted(out["warnings"]) == ["category_dropped:No id", "tag_dropped:Dessert", "tool_dropped:Mug"]

    recipe = recipe_of(api_client, user, out["slug"])
    oil, banana, half, pinch = recipe["recipeIngredient"]
    assert oil["food"]["id"] != str(foreign_food.id)
    assert oil["food"]["id"] == str(foods_named(user, "coconut oil")[0].id)
    assert oil["unit"]["id"] != str(foreign_unit.id)
    assert banana["food"]["id"] == str(our_food.id)
    assert half["food"]["id"] == str(our_food.id)
    assert half["unit"]["id"] == str(our_unit.id)
    assert pinch["food"] is None
    assert pinch["note"] == "1 pinch"

    assert [tag["id"] for tag in recipe["tags"]] == [str(our_tag.id)]
    assert recipe["recipeCategory"] == []
    assert recipe["tools"] == []
    # nothing was created in, or taken from, the other group
    assert len(foods_named(stranger, "coconut oil")) == 1
    assert len(foods_named(user, "coconut oil")) == 1
    assert len(foods_named(user, "banana")) == 1


def test_a_member_who_cant_organize_keeps_new_foods_as_text(
    api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    member = household_member(api_client, admin_token, user)
    job_id = ready_to_commit(user, source=IngestSource.inbox, created_by=None)
    assert api_client.get(job_url(job_id), headers=member.token).json()["permissions"]["canCreateFoods"] is False

    response = commit(api_client, member, job_id)
    assert response.status_code == 201, response.text
    recipe = recipe_of(api_client, member, response.json()["slug"])
    oil, salt = recipe["recipeIngredient"]
    assert oil["food"] is None
    assert oil["note"] == "coconut oil, melted"
    assert oil["unit"]["name"] == "tablespoon"  # units need no permission
    assert salt["food"] is None
    assert salt["note"] == "salt"
    assert foods_named(user, "coconut oil") == []
    assert len(units_named(user, "tablespoon")) == 1
    assert recipe["userId"] == str(member.user_id)  # the committer owns the recipe


def test_markers_kept_in_unit_and_food_names_stay_text(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """A unit or food name with a kept marker never becomes one of the group's units or foods"""
    user = unique_user_fn_scoped
    draft = banana_draft(
        ingredients=[
            CardDraftIngredient(
                original_text="1 [blank] cup [illegible] flour",
                quantity=1,
                unit=CardDraftRef(name="[blank] cup"),
                food=CardDraftRef(name="[illegible] flour"),
            ),
            CardDraftIngredient(
                original_text="2 cups [illegible] sugar",
                quantity=2,
                unit=CardDraftRef(name="cup"),
                food=CardDraftRef(name="[illegible] sugar"),
                note="sifted",
            ),
        ]
    )
    response = commit(api_client, user, ready_to_commit(user, draft=draft))
    assert response.status_code == 201, response.text

    flour, sugar = recipe_of(api_client, user, response.json()["slug"])["recipeIngredient"]
    assert (flour["quantity"], flour["unit"], flour["food"], flour["note"]) == (
        1,
        None,
        None,
        "___ cup (unreadable) flour",
    )
    assert (sugar["quantity"], sugar["unit"]["name"], sugar["food"]) == (2, "cup", None)
    assert sugar["note"] == "(unreadable) sugar, sifted"
    with session_context() as session:
        repos = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        foods = [food.name for food in repos.ingredient_foods.page_all(_all()).items]
        units = [unit.name for unit in repos.ingredient_units.page_all(_all()).items]
    assert foods == []
    assert units == ["cup"]


@pytest.mark.parametrize("kind", ["food", "unit"])
def test_a_food_or_unit_another_commit_just_created_is_linked(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, kind: str
):
    """Two cards with the same new food committed at once: the second finds the first's (the name is unique)"""
    user = unique_user_fn_scoped
    draft = banana_draft(
        ingredients=[
            CardDraftIngredient(
                original_text="1 scoop dragonfruit",
                quantity=1,
                unit=CardDraftRef(name="scoop"),
                food=CardDraftRef(name="dragonfruit"),
            )
        ]
    )
    job_id = ready_to_commit(user, draft=draft)

    real = getattr(card_commit.IngredientLinker, kind)
    created: list[Any] = []

    def linked_after_another_commit(self: card_commit.IngredientLinker, ref: CardDraftRef) -> Any:
        if not created:
            # the matcher was loaded before another member's commit made the same name
            self.matcher.foods_by_alias, self.matcher.units_by_alias  # noqa: B018
            with session_context() as other:
                repos = get_repositories(other, group_id=UUID(user.group_id), household_id=None)
                if kind == "food":
                    created.append(
                        repos.ingredient_foods.create(SaveIngredientFood(name="dragonfruit", group_id=user.group_id))
                    )
                else:
                    created.append(
                        repos.ingredient_units.create(SaveIngredientUnit(name="scoop", group_id=user.group_id))
                    )
        return real(self, ref)

    monkeypatch.setattr(card_commit.IngredientLinker, kind, linked_after_another_commit)
    response = commit(api_client, user, job_id)
    assert response.status_code == 201, response.text

    [line] = recipe_of(api_client, user, response.json()["slug"])["recipeIngredient"]
    assert line[kind]["id"] == str(created[0].id)
    assert len(foods_named(user, "dragonfruit")) == len(units_named(user, "scoop")) == 1
    assert job_row(job_id)["status"] == "committed"


# ==================================================================================================================
# The job


def test_a_pending_reread_is_cancelled_by_commit(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    token = uuid4()
    job_id = ready_to_commit(
        user, task_kind="reread", task_state="running", lease_token=token, task_payload={"page": 0}
    )

    assert commit(api_client, user, job_id).status_code == 201
    row = job_row(job_id)
    assert (row["task_kind"], row["task_state"], row["lease_token"], row["task_payload"]) == (None, None, None, None)


def test_a_double_commit_answers_with_the_same_recipe(
    api_client: TestClient, unique_user_fn_scoped: TestUser, published: list
):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    first = commit(api_client, user, job_id)
    assert first.status_code == 201

    again = commit(api_client, user, job_id)
    assert again.status_code == 200
    assert again.json()["recipeId"] == first.json()["recipeId"]
    assert again.json()["slug"] == first.json()["slug"]
    assert len(published) == 1

    recipes = api_client.get(api_routes.recipes, params={"perPage": -1}, headers=user.token).json()["items"]
    assert [recipe["slug"] for recipe in recipes] == ["banana-mug-cake"]


def test_a_commit_in_progress_is_a_409(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user, status=IngestStatus.committing, commit_started_at=utcnow(), commit_recipe_id=uuid4())
    assert assert_code(commit(api_client, user, job_id), 409, "invalid_status")["status"] == "committing"


def test_refusals(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    assert assert_code(commit(api_client, user, job_id, version=7), 409, "version_conflict")["current"] == 1

    processing = seed_job(user, status=IngestStatus.processing)
    assert assert_code(commit(api_client, user, processing), 409, "invalid_status")["status"] == "processing"
    failed = seed_job(user, status=IngestStatus.failed)
    assert assert_code(commit(api_client, user, failed), 409, "invalid_status")["status"] == "failed"
    assert_code(commit(api_client, user, uuid4()), 404, "not_found")
    assert job_row(job_id)["status"] == "ready"


def test_a_commit_can_carry_the_final_draft(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)  # the blank isn't kept
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["steps"][1]["text"] = "Microwave for 2 minutes."

    response = commit(api_client, user, job_id, draft=draft)
    assert response.status_code == 201, response.text
    assert job_row(job_id)["draft_version"] == 2
    steps = recipe_of(api_client, user, response.json()["slug"])["recipeInstructions"]
    assert steps[1]["text"] == "Microwave for 2 minutes."

    stale = seed_job(user)
    assert_code(commit(api_client, user, stale, version=3, draft=draft), 409, "version_conflict")


def test_a_draft_over_the_size_limits_is_a_422(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """The limits a PUT checks apply to a draft sent with the commit: refused, nothing saved or claimed"""
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["notes"] = [{"title": "", "text": f"note {i}"} for i in range(501)]
    assert (
        api_client.put(job_url(job_id), json={"draftVersion": 1, "draft": draft}, headers=user.token).status_code == 422
    )

    detail = assert_code(commit(api_client, user, job_id, draft=draft), 422, "commit_invalid")
    assert detail["fields"] == ["draft"]
    row = job_row(job_id)
    assert (row["status"], row["draft_version"], row["commit_recipe_id"], row["error_code"]) == ("ready", 1, None, None)


def test_a_draft_that_no_longer_validates_goes_back_to_ready(
    api_client: TestClient, unique_user_fn_scoped: TestUser, published: list
):
    user = unique_user_fn_scoped
    # nothing flags the empty name here (the stored flags say the card is clean), so the commit's own check does
    job_id = seed_job(user, draft=banana_draft(name="  ", steps=[CardDraftStep(text="Bake.")]), flags=[])

    detail = assert_code(commit(api_client, user, job_id), 422, "commit_invalid")
    assert detail["fields"] == ["name"]
    row = job_row(job_id)
    assert row["status"] == "ready"
    assert row["error_code"] == "commit_invalid"
    assert row["error_params"] == {"fields": ["name"]}
    reserved = row["commit_recipe_id"]
    assert reserved is not None
    assert not recipe_dir(reserved).exists()
    assert published == []

    # fixed and committed again: the same server-owned id
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["name"] = "Bread"
    saved = api_client.put(job_url(job_id), json={"draftVersion": 1, "draft": draft}, headers=user.token).json()
    response = commit(api_client, user, job_id, version=saved["draftVersion"])
    assert response.status_code == 201
    assert response.json()["recipeId"] == str(reserved)
    assert job_row(job_id)["error_code"] is None


def test_commit_and_next(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    with session_context() as session:
        batch_id = IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).batches.create(
            source=IngestSource.app, created_by=user.user_id
        )
    names = ["Card A", "Card B", "Card C", "Card D"]
    jobs = [
        ready_to_commit(user, batch_id=batch_id, position=i, draft=banana_draft(name=n)) for i, n in enumerate(names)
    ]
    failed = seed_job(user, status=IngestStatus.failed, batch_id=batch_id, position=9)

    assert commit(api_client, user, jobs[1]).json()["nextJobId"] == str(jobs[2])
    assert commit(api_client, user, jobs[3]).json()["nextJobId"] == str(jobs[0])  # wraps round to a skipped card
    assert commit(api_client, user, jobs[0]).json()["nextJobId"] == str(jobs[2])
    assert commit(api_client, user, jobs[2]).json()["nextJobId"] is None
    assert job_row(failed)["status"] == "failed"


def test_commit_and_rotate_answer_503_while_a_restore_holds_the_write_lock(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    fd = os.open(storage.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # what a restore takes, with no marker yet
        for response in (
            commit(api_client, user, job_id),
            api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token),
            api_client.delete(job_url(job_id), headers=user.token),
        ):
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "paused_for_restore"
            assert response.headers["Retry-After"]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    row = job_row(job_id)
    assert (row["status"], row["commit_recipe_id"]) == ("ready", None)
    assert commit(api_client, user, job_id).status_code == 201


def test_missing_card_files_return_the_job_to_ready(
    api_client: TestClient, unique_user_fn_scoped: TestUser, published: list
):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    (storage.page_dir(UUID(user.group_id), job_id, 0) / "page.jpg").unlink()

    assert_code(commit(api_client, user, job_id), 409, "commit_interrupted")
    row = job_row(job_id)
    assert (row["status"], row["error_code"]) == ("ready", "commit_interrupted")
    assert not recipe_dir(row["commit_recipe_id"]).exists()
    assert published == []
    assert api_client.get(api_routes.recipes, headers=user.token).json()["items"] == []
