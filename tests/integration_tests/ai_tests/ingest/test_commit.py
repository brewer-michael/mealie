"""
Committing a reviewed card (docs/ai/PHASE2.md §7, §18 Commit): the recipe a draft becomes, its card assets and cover,
re-linked foods and units, organizers looked up in the group, kept markers, the household's settings, the event, double
commits and refusals. Crash recovery is in `test_commit_recovery.py`. Runs on SQLite and PostgreSQL.
"""

import fcntl
import io
import os
import threading
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
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
    set_columns,
    use_fake_flags,
)

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.db.db_setup import session_context
from mealie.db.models.recipe.recipe import RecipeModel
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
from mealie.services.backups_v2.backup_v2 import BackupV2
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
    """
    A ready job whose errors were all kept as written; unless `draft` says otherwise, the card photo is attached and is
    the cover (a test user's household is public or not at random)
    """
    draft = kwargs.pop("draft", None) or banana_draft(attach_card_photo=True, use_card_as_cover=True)
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
    draft = banana_draft(use_card_as_cover=False, attach_card_photo=True)
    out = commit(api_client, user, ready_to_commit(user, draft=draft)).json()
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
    # a line kept as written is parsed around its marker (the unit is the unreadable word here)
    ingredient = recipe["recipeIngredient"][0]
    assert (ingredient["quantity"], ingredient["unit"], ingredient["food"]["name"]) == (1, None, "flour")
    assert (ingredient["note"], ingredient["originalText"]) == ("(unreadable)", "1 (unreadable) flour")
    assert recipe["recipeInstructions"][1]["text"] == "Microwave for ___ minutes."
    assert recipe["notes"][1] == {**recipe["notes"][1], "title": "Tip", "text": "Use (unreadable) bananas"}


@pytest.mark.parametrize("locale", ["de-DE", "en-GB"])
def test_a_language_without_the_forks_texts_gets_them_in_english(
    api_client: TestClient, unique_user_fn_scoped: TestUser, locale: str
):
    """Only en-US carries the fork's texts: a commit in another language writes them in English, never their keys"""
    user = unique_user_fn_scoped
    draft = banana_draft(steps=[CardDraftStep(text="Bake at [illegible] degrees.")], attach_card_photo=True)
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
        tools=[CardDraftRef(id=uuid4(), name="Mug")],
    )
    response = commit(api_client, user, ready_to_commit(user, draft=draft))
    assert response.status_code == 201, response.text
    out = response.json()
    # an id that isn't one of the group's is dropped, whatever its name
    assert sorted(out["warnings"]) == ["tag_dropped:Dessert", "tool_dropped:Mug"]

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


def test_a_unit_whose_amount_is_a_kept_marker_is_named_in_the_note(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """
    Upstream's recipe page shows a unit only with an amount: "[blank] C. sugar" kept as written would read "sugar ___"
    and lose its cup. The unit is named after the marker instead, as the page names the group's unit (its abbreviation
    when it uses one), and isn't linked; a unit the group hasn't is named as written and not created.
    """
    user = unique_user_fn_scoped
    with session_context() as session:
        repos = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        cup = repos.ingredient_units.create(
            SaveIngredientUnit(name="cup", abbreviation="c", use_abbreviation=True, group_id=user.group_id)
        )
    draft = banana_draft(
        ingredients=[
            CardDraftIngredient(
                original_text="[blank] C. sugar",
                unit=CardDraftRef(id=cup.id, name="cup"),
                food=CardDraftRef(name="sugar"),
                note="[blank], packed",
            ),
            CardDraftIngredient(
                original_text="[illegible] scoops flour",
                unit=CardDraftRef(name="scoop"),
                food=CardDraftRef(name="flour"),
                note="[illegible]",
            ),
            CardDraftIngredient(
                original_text="2 C. [blank]",
                quantity=2,
                unit=CardDraftRef(id=cup.id, name="cup"),
                note="[blank]",
            ),
        ]
    )
    response = commit(api_client, user, ready_to_commit(user, draft=draft))
    assert response.status_code == 201, response.text

    sugar, flour, other = recipe_of(api_client, user, response.json()["slug"])["recipeIngredient"]
    assert (sugar["quantity"] or None, sugar["unit"], sugar["food"]["name"]) == (None, None, "sugar")
    assert sugar["note"] == "___ c, packed"
    assert sugar["display"] == "sugar ___ c, packed"  # the recipe page's text: food, then note
    assert (flour["unit"], flour["food"]["name"], flour["note"]) == (None, "flour", "(unreadable) scoop")
    assert flour["display"] == "flour (unreadable) scoop"
    assert units_named(user, "scoop") == []
    # a line with an amount keeps its unit, linked
    assert (other["quantity"], other["unit"]["id"], other["note"]) == (2, str(cup.id), "___")


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


# ==================================================================================================================
# The cover (a portrait card is letterboxed) and the card photo switch


def _set_recipes_public(api_client: TestClient, user: TestUser, public: bool) -> None:
    """The household's new recipes seen without a login or not, with their assets hidden by default"""
    preferences = api_client.get(api_routes.households_preferences, headers=user.token).json()
    preferences.update({"privateHousehold": not public, "recipePublic": public, "recipeShowAssets": False})
    assert api_client.put(api_routes.households_preferences, json=preferences, headers=user.token).status_code == 200


def _dark(pixel: Any) -> bool:
    return sum(pixel[:3]) < 3 * 80


def _light(pixel: Any) -> bool:
    return all(channel > 200 for channel in pixel[:3])


def test_a_portrait_card_is_letterboxed_as_the_cover(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """The recipe header crops its image to a landscape box: the whole card, title included, stays in view"""
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)  # 480 x 640 pages, a dark block in the top-left corner
    out = commit(api_client, user, job_id).json()
    recipe = recipe_of(api_client, user, out["slug"])
    assert recipe["image"]

    with Image.open(recipe_dir(out["recipeId"]) / "images" / "original.webp") as cover:
        rgb = cover.convert("RGB")
    width, height = rgb.size
    assert abs(width / height - 4 / 3) < 0.01
    page_width = height * 480 / 640
    left = (width - page_width) / 2
    # the page's top row is in the cover (its dark corner block), centred on the page's own border colour
    assert _dark(rgb.getpixel((round(left + page_width * 0.1), round(height * 0.05))))
    assert _light(rgb.getpixel((round(left / 2), round(height * 0.05))))
    assert _light(rgb.getpixel((round(width - left / 2), round(height * 0.95))))
    assert _light(rgb.getpixel((round(left + page_width * 0.9), round(height * 0.9))))

    # the asset is still the whole page, as it was
    token = job_row(job_id)["commit_asset_token"]
    with Image.open(recipe_dir(out["recipeId"]) / "assets" / f"recipe-card-{token}-1.jpg") as asset:
        assert asset.size == (480, 640)


def test_a_landscape_card_is_the_cover_as_it_is(tmp_path: Any):
    landscape, portrait = tmp_path / "landscape.jpg", tmp_path / "portrait.jpg"
    Image.new("RGB", (640, 480), (250, 245, 230)).save(landscape, "JPEG")
    page = Image.new("RGB", (300, 600), (30, 90, 200))
    page.paste((250, 250, 250), (20, 20, 280, 580))
    page.save(portrait, "JPEG")

    assert card_commit.cover_image(landscape) == landscape
    cover_bytes = card_commit.cover_image(portrait)
    assert isinstance(cover_bytes, bytes)
    with Image.open(io.BytesIO(cover_bytes)) as cover:
        assert cover.format == "JPEG"
        assert cover.size == (800, 600)
        # the letterbox takes the page's border colour, not the paper's
        red, green, blue = cover.convert("RGB").getpixel((50, 300))
        assert abs(red - 30) < 12 and abs(green - 90) < 12 and abs(blue - 200) < 12


def test_the_card_photo_is_attached_unless_recipes_are_seen_without_a_login(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """Assets and the recipe image are served without a login, so in a household whose recipes are public the card
    photo and the cover are off unless the reviewer turns them on; elsewhere they're on unless they turn them off"""
    user = unique_user_fn_scoped

    def committed(draft: Any) -> tuple[dict[str, Any], list[str]]:
        job_id = ready_to_commit(user, draft=draft)
        out = commit(api_client, user, job_id).json()
        recipe = recipe_of(api_client, user, out["slug"])
        assets = recipe_dir(out["recipeId"]) / "assets"
        files = sorted(path.name for path in assets.glob("recipe-card-*")) if assets.exists() else []
        return recipe, files

    # a new install: a private household whose recipes are "public" by default keeps the card (set here: a test
    # user's registration picks the household's privacy at random)
    preferences = api_client.get(api_routes.households_preferences, headers=user.token).json()
    preferences.update({"privateHousehold": True, "recipePublic": True})
    assert api_client.put(api_routes.households_preferences, json=preferences, headers=user.token).status_code == 200
    job = api_client.get(job_url(ready_to_commit(user, draft=banana_draft())), headers=user.token).json()
    assert (job["householdRecipesPublic"], job["cardPhotoDefault"], job["cardCoverDefault"]) == (False, True, True)

    _set_recipes_public(api_client, user, True)
    job = api_client.get(job_url(ready_to_commit(user, draft=banana_draft())), headers=user.token).json()
    assert (job["householdRecipesPublic"], job["cardPhotoDefault"], job["cardCoverDefault"]) == (True, False, False)
    assert (job["draft"]["attachCardPhoto"], job["draft"]["useCardAsCover"]) == (None, None)
    listed = api_client.get("/api/ai/ingest/jobs", headers=user.token).json()["items"]
    assert {item["householdRecipesPublic"] for item in listed} == {True}  # for the batch's "Add clean cards"

    recipe, files = committed(banana_draft(name="Public default"))
    assert (recipe["assets"], files) == ([], [])
    assert recipe["settings"]["showAssets"] is False  # the household's own default
    assert recipe["image"] is None
    assert not (recipe_dir(recipe["id"]) / "images" / "original.webp").exists()

    recipe, files = committed(banana_draft(name="Public attached", attach_card_photo=True))
    assert [asset["name"] for asset in recipe["assets"]] == ["Recipe card"]
    assert len(files) == 1
    assert recipe["settings"]["showAssets"] is True
    assert recipe["image"] is None  # the cover is its own switch

    recipe, files = committed(banana_draft(name="Public cover", use_card_as_cover=True))
    assert (recipe["assets"], files) == ([], [])
    assert recipe["image"]

    _set_recipes_public(api_client, user, False)
    job = api_client.get(job_url(ready_to_commit(user, draft=banana_draft())), headers=user.token).json()
    assert (job["householdRecipesPublic"], job["cardPhotoDefault"], job["cardCoverDefault"]) == (False, True, True)

    recipe, files = committed(banana_draft(name="Private default"))
    assert [asset["name"] for asset in recipe["assets"]] == ["Recipe card"]
    assert len(files) == 1
    assert recipe["image"]

    recipe, files = committed(banana_draft(name="Private detached", attach_card_photo=False, use_card_as_cover=False))
    assert (recipe["assets"], files) == ([], [])
    assert recipe["image"] is None


def test_a_resumed_commit_that_no_longer_attaches_removes_the_written_assets(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user, draft=banana_draft(attach_card_photo=False))
    set_columns(job_id, commit_recipe_id=uuid4(), commit_asset_token="tok")
    row = job_row(job_id)
    stale = recipe_dir(row["commit_recipe_id"]) / "assets" / "recipe-card-tok-1.jpg"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"an earlier attempt's asset")

    out = commit(api_client, user, job_id).json()
    assert out["recipeId"] == str(row["commit_recipe_id"])
    assert not stale.exists()


# ==================================================================================================================
# Upstream's provenance bit


def _is_ocr_recipe(recipe_id: Any) -> Any:
    with session_context() as session:
        stmt = sa.select(RecipeModel.is_ocr_recipe).where(RecipeModel.id == UUID(str(recipe_id)))
        return session.execute(stmt).scalar_one()


def test_a_card_recipe_is_marked_as_one_and_others_arent(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    out = commit(api_client, user, ready_to_commit(user)).json()
    assert _is_ocr_recipe(out["recipeId"]) is True

    # a recipe made in the editor isn't
    response = api_client.post(api_routes.recipes, json={"name": "Typed in"}, headers=user.token)
    assert response.status_code == 201
    typed = recipe_of(api_client, user, response.json())
    assert not _is_ocr_recipe(typed["id"])


def test_the_mark_survives_a_backup(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    out = commit(api_client, user, ready_to_commit(user)).json()

    backup_v2 = BackupV2(get_app_settings().DB_URL)
    try:
        backup_v2.restore(backup_v2.backup())
    finally:
        backup_v2.db_exporter.engine.dispose()

    assert _is_ocr_recipe(out["recipeId"]) is True
    assert not storage.is_paused()


# ==================================================================================================================
# recipe_created, at least once


def test_a_commit_sends_recipe_created_once_and_records_it(
    api_client: TestClient, unique_user_fn_scoped: TestUser, published: list
):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    assert commit(api_client, user, job_id).status_code == 201

    row = job_row(job_id)
    assert len(published) == 1
    assert row["recipe_event_claimed_at"] is not None
    assert row["recipe_event_sent_at"] is not None

    # housekeeping has nothing to send for it, however late it runs
    later = utcnow() + card_commit.RECIPE_EVENT_LEASE + timedelta(minutes=1)
    assert card_commit.resend_recipe_events(later) == 0
    assert len(published) == 1


def test_a_failed_send_is_sent_again_by_housekeeping(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    sent: list[str] = []

    def dispatch(self: EventBusService, *args: Any, **kwargs: Any) -> None:
        if kwargs["event_type"] != EventTypes.recipe_created:
            return
        if not sent and not getattr(dispatch, "failed", False):
            dispatch.failed = True  # type: ignore[attr-defined]
            raise RuntimeError("the notifier's server is down")
        sent.append(kwargs["document_data"].recipe_slug)

    monkeypatch.setattr(EventBusService, "dispatch", dispatch)
    job_id = ready_to_commit(user)
    assert commit(api_client, user, job_id).status_code == 201
    assert sent == []
    assert job_row(job_id)["recipe_event_sent_at"] is None

    # the committer's claim holds for a while, then housekeeping sends it, once
    assert card_commit.resend_recipe_events(utcnow() + timedelta(minutes=2)) == 0
    later = utcnow() + card_commit.RECIPE_EVENT_LEASE + timedelta(seconds=5)
    assert card_commit.resend_recipe_events(later) == 1
    assert sent == ["banana-mug-cake"]
    assert job_row(job_id)["recipe_event_sent_at"] is not None
    assert card_commit.resend_recipe_events(later + timedelta(minutes=10)) == 0
    assert sent == ["banana-mug-cake"]


def test_two_housekeepers_send_a_late_event_once(unique_user_fn_scoped: TestUser, published: list, api_client):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    assert commit(api_client, user, job_id).status_code == 201
    published.clear()
    # as if the process had stopped between the finish and the send, six minutes ago
    set_columns(
        job_id,
        recipe_event_sent_at=None,
        recipe_event_claimed_at=utcnow() - timedelta(minutes=6),
        committed_at=utcnow() - timedelta(minutes=6),
    )

    barrier = threading.Barrier(2)
    counts: list[int] = []

    def housekeeping() -> None:
        barrier.wait()
        counts.append(card_commit.resend_recipe_events(utcnow()))

    threads = [threading.Thread(target=housekeeping) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(counts) == [0, 1]
    assert len(published) == 1


def test_late_events_skip_old_commits_and_deleted_recipes(
    api_client: TestClient, unique_user_fn_scoped: TestUser, published: list
):
    user = unique_user_fn_scoped
    old_job, deleted_job = ready_to_commit(user), ready_to_commit(user, draft=banana_draft(name="Gone"))
    old = commit(api_client, user, old_job).json()
    gone = commit(api_client, user, deleted_job).json()
    published.clear()
    set_columns(
        old_job, recipe_event_sent_at=None, recipe_event_claimed_at=None, committed_at=utcnow() - timedelta(hours=25)
    )
    set_columns(deleted_job, recipe_event_sent_at=None, recipe_event_claimed_at=None)
    assert api_client.delete(api_routes.recipes_slug(gone["slug"]), headers=user.token).status_code == 200

    later = utcnow() + timedelta(minutes=2)
    assert card_commit.resend_recipe_events(later) == 0
    assert published == []
    assert job_row(old_job)["recipe_event_sent_at"] is None  # a restored backup never replays old events
    assert job_row(deleted_job)["recipe_event_sent_at"] is not None  # nothing to announce
    assert old["slug"] == "banana-mug-cake"


# ==================================================================================================================
# Organizers named in review are created at commit


def _organizer_names(user: TestUser, kind: str) -> list[str]:
    with session_context() as session:
        repos = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        return sorted(item.name for item in getattr(repos, kind).page_all(_all()).items)


def test_new_organizers_are_created_for_a_committer_who_can_organize(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    with session_context() as session:
        existing = get_repositories(session, group_id=UUID(user.group_id), household_id=None).tags.create(
            TagSave(name="Quick", group_id=user.group_id)
        )
    job_id = ready_to_commit(
        user,
        draft=banana_draft(
            attach_card_photo=True,
            tags=[CardDraftRef(name="Weeknight"), CardDraftRef(name="quick")],  # "quick" is the group's "Quick"
            categories=[CardDraftRef(name="Dessert")],
            tools=[CardDraftRef(name="Mug"), CardDraftRef(name="[illegible] pan")],
        ),
    )
    assert api_client.get(job_url(job_id), headers=user.token).json()["permissions"]["canCreateOrganizers"] is True

    out = commit(api_client, user, job_id).json()
    recipe = recipe_of(api_client, user, out["slug"])
    tags = {tag["name"]: tag["id"] for tag in recipe["tags"]}
    assert sorted(tags) == ["Quick", "Weeknight"]
    assert tags["Quick"] == str(existing.id)
    assert [category["name"] for category in recipe["recipeCategory"]] == ["Dessert"]
    assert [tool["name"] for tool in recipe["tools"]] == ["Mug"]
    assert out["warnings"] == ["tool_dropped:[illegible] pan"]  # a name with a marker is never made an organizer
    assert _organizer_names(user, "tags") == ["Quick", "Weeknight"]
    assert _organizer_names(user, "categories") == ["Dessert"]
    assert _organizer_names(user, "tools") == ["Mug"]


def test_a_committer_who_cant_organize_drops_new_organizers(
    api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    member = household_member(api_client, admin_token, user)
    job_id = ready_to_commit(user, draft=banana_draft(tags=[CardDraftRef(name="Weeknight")]))
    assert api_client.get(job_url(job_id), headers=member.token).json()["permissions"]["canCreateOrganizers"] is False

    out = commit(api_client, member, job_id).json()
    assert out["warnings"] == ["tag_dropped:Weeknight"]
    assert recipe_of(api_client, user, out["slug"])["tags"] == []
    assert _organizer_names(user, "tags") == []


def test_an_organizer_another_commit_creates_meanwhile_is_used_once(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """The lookup misses it, the insert then clashes with the one another commit just made: that one is used"""
    user = unique_user_fn_scoped
    with session_context() as session:
        theirs = get_repositories(session, group_id=UUID(user.group_id), household_id=None).tags.create(
            TagSave(name="Weeknight", group_id=user.group_id)
        )
    real = card_commit.OrganizerMaker._find_or_create

    class Late:
        """The tags repository, seen before the other commit's insert"""

        def __init__(self, repo: Any) -> None:
            self.repo, self.looked = repo, False

        def get_one(self, *args: Any) -> Any:
            if not self.looked:
                self.looked = True
                return None
            return self.repo.get_one(*args)

        def create(self, data: Any) -> Any:
            return self.repo.create(data)

    monkeypatch.setattr(
        card_commit.OrganizerMaker, "_find_or_create", lambda self, repo, save, name: real(self, Late(repo), save, name)
    )
    out = commit(api_client, user, ready_to_commit(user, draft=banana_draft(tags=[CardDraftRef(name="Weeknight")])))
    assert out.status_code == 201, out.text
    recipe = recipe_of(api_client, user, out.json()["slug"])
    assert [tag["id"] for tag in recipe["tags"]] == [str(theirs.id)]
    assert _organizer_names(user, "tags") == ["Weeknight"]


@pytest.mark.parametrize(
    "attribution, title, text",
    [
        ("Van der Berg", "Van", "Van der Berg"),
        ("De la Torre", "De", "De la Torre"),
        ("Van: Oma", "Van", "Oma"),
        ("From Grandma Jo", "Van", "Grandma Jo"),
        ("From Grandma Jo", "From", "Grandma Jo"),
    ],
)
def test_the_attribution_follows_the_drafts_rule(attribution: str, title: str, text: str):
    """The title's translated word goes only with a colon after it: it may begin a surname (`strip_from_prefix`)"""
    assert card_commit._attribution_text(attribution, title) == text


def test_a_unit_commit_creates_gets_its_standard_abbreviation(api_client: TestClient, unique_user_fn_scoped: TestUser):
    # the card flags accept a linked unit's abbreviation; a unit commit creates has one, as the seeded units do
    user = unique_user_fn_scoped
    draft = banana_draft(
        ingredients=[
            CardDraftIngredient(
                original_text="2 tablespoons honey", quantity=2, unit=CardDraftRef(name="tablespoons"), food=None
            ),
            CardDraftIngredient(original_text="1 scoop ice", quantity=1, unit=CardDraftRef(name="scoop"), food=None),
        ]
    )
    response = commit(api_client, user, ready_to_commit(user, draft=draft))
    assert response.status_code == 201, response.text

    [tablespoons] = units_named(user, "tablespoons")
    assert tablespoons.abbreviation == "tbsp"
    [scoop] = units_named(user, "scoop")
    assert scoop.abbreviation == ""
