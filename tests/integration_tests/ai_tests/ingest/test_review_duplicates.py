"""
The possible-duplicate banner (docs/ai/PHASE2.md §6.4): a group recipe with the draft's name, and the name commit would
then give the card's recipe ("Name (2)" when "Name (1)" is taken too, as upstream's create picks it); else the
household's recipe with the most similar name; and another card of the household waiting with the same name. Checked
on each GET of the job and on each save. Runs on SQLite and PostgreSQL.
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import banana_draft, job_row, job_url, seed_job, use_fake_flags

from mealie.schema.recipe_ingest import CardDraftStep, IngestStatus
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


def _recipe(api_client: TestClient, user: TestUser, name: str) -> dict[str, Any]:
    response = api_client.post(api_routes.recipes, json={"name": name}, headers=user.token)
    assert response.status_code == 201, response.text
    recipe = api_client.get(api_routes.recipes_slug(response.json()), headers=user.token).json()
    return {"id": recipe["id"], "slug": recipe["slug"], "name": recipe["name"]}


def _card(user: TestUser, name: str, **columns: Any) -> UUID:
    # no blank left on it, so it can be committed
    draft = banana_draft(name=name, steps=[CardDraftStep(text="Microwave for 2 minutes.")])
    return seed_job(user, draft=draft, **columns)


def _job(api_client: TestClient, user: TestUser, job_id: UUID) -> dict[str, Any]:
    response = api_client.get(job_url(job_id), headers=user.token)
    assert response.status_code == 200, response.text
    return response.json()


def _duplicates(body: dict[str, Any]) -> tuple[Any, Any, Any]:
    return body["duplicateOf"], body["duplicateJob"], body["duplicateName"]


def test_a_recipe_with_the_same_name_gives_the_name_commit_would_make(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    existing = _recipe(api_client, user, "Banana bread")
    job_id = _card(user, "Banana Bread")

    duplicate_of, duplicate_job, duplicate_name = _duplicates(_job(api_client, user, job_id))
    assert duplicate_of == existing
    assert duplicate_job is None
    assert duplicate_name == "Banana Bread (1)"


def test_the_name_suffix_is_the_first_free_one_as_commit_picks_it(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    first = _recipe(api_client, user, "Banana Bread")
    second = _recipe(api_client, user, "Banana Bread")
    assert second["name"] == "Banana Bread (1)"  # upstream's own rule
    job_id = _card(user, "Banana Bread")

    duplicate_of, _, duplicate_name = _duplicates(_job(api_client, user, job_id))
    assert duplicate_of == first
    assert duplicate_name == "Banana Bread (2)"

    response = api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1}, headers=user.token)
    assert response.status_code == 201, response.text
    recipe = api_client.get(api_routes.recipes_slug(response.json()["slug"]), headers=user.token).json()
    assert recipe["name"] == duplicate_name


def test_a_suffix_another_name_already_holds_is_skipped(api_client: TestClient, unique_user_fn_scoped: TestUser):
    # "Lemon Bars 1" holds the slug "Lemon Bars (1)" would get
    user = unique_user_fn_scoped
    _recipe(api_client, user, "Lemon Bars")
    _recipe(api_client, user, "Lemon Bars 1")
    job_id = _card(user, "Lemon Bars")
    assert _job(api_client, user, job_id)["duplicateName"] == "Lemon Bars (2)"


@pytest.mark.parametrize("taken", [10, 11, 12])
def test_a_name_taken_with_every_number_upstream_tries_gets_the_next_free_one(
    api_client: TestClient, unique_user_fn_scoped: TestUser, taken: int
):
    """
    A family recipe box with many versions of one recipe: upstream's create only tries "(1)" to "(9)" itself, so commit
    picks the name before it, as the banner says, and the card never gets stuck being added
    """
    user = unique_user_fn_scoped
    name = "Chocolate Chip Cookies"
    for title in [name, *(f"{name} ({number})" for number in range(1, taken))]:
        _recipe(api_client, user, title)
    job_id = _card(user, name)
    expected = f"{name} ({taken})"
    assert _job(api_client, user, job_id)["duplicateName"] == expected

    response = api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1}, headers=user.token)
    assert response.status_code == 201, response.text
    assert response.json()["slug"] == f"chocolate-chip-cookies-{taken}"
    recipe = api_client.get(api_routes.recipes_slug(response.json()["slug"]), headers=user.token).json()
    assert recipe["name"] == expected
    assert job_row(job_id)["status"] == "committed"


def test_a_name_with_every_number_taken_goes_back_for_review(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    from mealie.services.ai.ingest import review

    user = unique_user_fn_scoped
    monkeypatch.setattr(review, "MAX_NAME_SUFFIX", 3)
    for title in ["Plum Cake", "Plum Cake (1)", "Plum Cake (2)", "Plum Cake (3)"]:
        _recipe(api_client, user, title)
    job_id = _card(user, "Plum Cake")
    assert _job(api_client, user, job_id)["duplicateName"] is None  # no free one is left

    response = api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1}, headers=user.token)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {"code": "commit_invalid", "fields": ["name"]}
    row = job_row(job_id)
    assert (row["status"], row["error_code"]) == ("ready", "commit_invalid")


@pytest.mark.parametrize("existing", ["Bananna Bread", "Banana Bred", "banana-bread!"])
def test_a_recipe_with_a_near_name_is_a_possible_duplicate(
    api_client: TestClient, unique_user_fn_scoped: TestUser, existing: str
):
    user = unique_user_fn_scoped
    recipe = _recipe(api_client, user, existing)
    job_id = _card(user, "Banana Bread")

    duplicate_of, _, duplicate_name = _duplicates(_job(api_client, user, job_id))
    assert duplicate_of == recipe
    if recipe["slug"] == "banana-bread":
        assert duplicate_name == "Banana Bread (1)"
    else:
        assert duplicate_name is None  # commit keeps the name: its slug is free


def test_the_closest_near_name_is_the_one_shown(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    _recipe(api_client, user, "Chocolate Chip Cooks")
    closest = _recipe(api_client, user, "Chocolate Chip Cookie")
    job_id = _card(user, "Chocolate Chip Cookies")
    assert _job(api_client, user, job_id)["duplicateOf"] == closest


@pytest.mark.parametrize("existing", ["Banana Bread Muffins", "Bread", "Zucchini Bread"])
def test_a_different_recipe_is_no_duplicate(api_client: TestClient, unique_user_fn_scoped: TestUser, existing: str):
    user = unique_user_fn_scoped
    _recipe(api_client, user, existing)
    job_id = _card(user, "Banana Bread")
    assert _duplicates(_job(api_client, user, job_id)) == (None, None, None)


def test_near_names_are_looked_for_in_the_household_and_same_names_in_the_group(
    api_client: TestClient, unique_user: TestUser, h2_user: TestUser
):
    """
    A recipe of another household of the group with the same name still makes commit pick "Name (1)" (slugs are the
    group's); one with only a similar name isn't the household's
    """
    word = random_string(8)
    near = _recipe(api_client, h2_user, f"Spiced {word}x Loaf")
    job_id = _card(unique_user, f"Spiced {word} Loaf")
    assert _duplicates(_job(api_client, unique_user, job_id)) == (None, None, None)

    same = _recipe(api_client, h2_user, f"Spiced {word} Loaf")
    duplicate_of, _, duplicate_name = _duplicates(_job(api_client, unique_user, job_id))
    assert duplicate_of == same != near
    assert duplicate_name == f"Spiced {word} Loaf (1)"


def test_another_card_waiting_with_the_same_name_is_a_possible_duplicate(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    first = _card(user, "Banana Mug Cake")
    second = _card(user, "  banana   MUG cake ")

    _, duplicate_job, duplicate_name = _duplicates(_job(api_client, user, second))
    assert duplicate_job == {"id": str(first), "title": "Banana Mug Cake"}
    assert duplicate_name is None  # no recipe has the name yet
    assert _job(api_client, user, first)["duplicateJob"]["id"] == str(second)


def test_a_card_still_being_read_counts_and_an_added_one_doesnt(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    committed = _card(user, "Apple Crisp", status=IngestStatus.committed)
    job_id = _card(user, "Apple Crisp")
    assert _job(api_client, user, job_id)["duplicateJob"] is None
    assert job_row(committed)["title"] == "Apple Crisp"

    # a failed card read again keeps its old title while it's being read
    processing = seed_job(user, status=IngestStatus.processing, title="Apple Crisp")
    assert _job(api_client, user, job_id)["duplicateJob"] == {"id": str(processing), "title": "Apple Crisp"}


def test_another_households_card_isnt_shown(api_client: TestClient, unique_user: TestUser, h2_user: TestUser):
    name = f"Pear Tart {random_string(8)}"
    _card(h2_user, name)
    job_id = _card(unique_user, name)
    assert _job(api_client, unique_user, job_id)["duplicateJob"] is None


def test_only_a_card_being_reviewed_is_checked(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    _recipe(api_client, user, "Banana Bread")
    _card(user, "Banana Bread")
    job_id = _card(user, "Banana Bread", status=IngestStatus.failed)
    assert _duplicates(_job(api_client, user, job_id)) == (None, None, None)
