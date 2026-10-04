"""
A line the reviewer writes as text is parsed on save as a freshly read line is (docs/ai/PHASE2.md §5, §6.6): a filled
blank ("[blank] C. brown sugar" becomes "1 C. brown sugar"), an edited text line or a new one gets its amount, unit and
food, with "C." written out. A line with an amount, unit or food is the reviewer's and stays as sent, and a text line
nobody changed isn't parsed again. Uses the real flag rules and Mealie's NLP parser. Runs on SQLite and PostgreSQL.
"""

import logging
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import banana_draft, job_row, job_url, seed_job

from mealie.schema.recipe_ingest import CardDraftIngredient, ExtractionMeta
from mealie.services.ai.ingest import review
from mealie.services.ai.ingest.pipeline.flags import compute_flags, ingredient_hash
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser

CARD = (
    "Banana Mug Cake\n1 T. coconut oil (melted)\n1/4 t. salt\n[blank] C. brown sugar\nSalt and pepper\n"
    "Mash the banana in a mug and stir in everything else.\nMicrowave for [blank] minutes."
)


def _text_line(text: str) -> CardDraftIngredient:
    """A line as extraction keeps one it doesn't parse (it holds a marker, or the parser couldn't split it)"""
    ingredient = CardDraftIngredient(original_text=text, note=text, display=text)
    ingredient.extracted_hash = ingredient_hash(ingredient)
    return ingredient


def _card(user: TestUser, language: str | None = "en") -> UUID:
    banana = banana_draft()
    draft = banana.model_copy(
        update={
            "ingredients": [*banana.ingredients, _text_line("[blank] C. brown sugar"), _text_line("Salt and pepper")]
        }
    )
    extraction = ExtractionMeta(read_path="image", provider="Claude", model="claude-sonnet", language=language)
    flags = compute_flags(draft, extraction, {}, transcription=CARD)
    return seed_job(user, draft=draft, flags=flags, transcription=CARD, extraction=extraction)


def _put(api_client: TestClient, user: TestUser, job_id: UUID, draft: dict[str, Any], **body: Any) -> dict[str, Any]:
    version = job_row(job_id)["draft_version"]
    response = api_client.put(
        job_url(job_id), json={"draftVersion": version, "draft": draft, **body}, headers=user.token
    )
    assert response.status_code == 200, response.text
    return response.json()


def _fill(line: dict[str, Any], text: str) -> dict[str, Any]:
    """What the review page does when a value is typed into a text line's blank: the note and display change"""
    return {**line, "note": text, "display": text}


def _flags_of(body: dict[str, Any], ref: str) -> dict[str, dict[str, Any]]:
    """The flags of one line, by kind, in a job or a save's answer"""
    return {flag["kind"]: flag for flag in body["flags"] if flag["ref"] == ref}


def test_a_filled_blank_is_parsed_like_a_freshly_read_line(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = _card(user)
    job = api_client.get(job_url(job_id), headers=user.token).json()
    draft = job["draft"]
    sugar = draft["ingredients"][2]
    ref = sugar["referenceId"]
    assert "blank" in _flags_of(job, ref)

    draft["ingredients"][2] = _fill(sugar, "1 C. brown sugar")
    saved = _put(api_client, user, job_id, draft)

    line = job_row(job_id)["draft"]["ingredients"][2]
    assert (line["reference_id"], line["original_text"], line["quantity"]) == (ref, "1 C. brown sugar", 1)
    assert (line["unit"]["name"], line["food"]["name"]) == ("cup", "brown sugar")
    assert line["parse_confidence"] is not None
    assert line["display"].startswith("1 cup brown sugar")

    # the line's flags are a freshly read line's: the marker's gone, and "C." was read as a cup
    flags = _flags_of(saved, ref)
    assert "blank" not in flags
    assert flags["shorthand_read"]["params"] == {"from": "C.", "to": "cup"}

    # the other text line, which nobody changed, is still text
    salt = job_row(job_id)["draft"]["ingredients"][3]
    assert (salt["quantity"], salt["unit"], salt["food"], salt["note"]) == (None, None, None, "Salt and pepper")

    # and the recipe gets the amount, unit and food
    blank = next(flag["id"] for flag in saved["flags"] if flag["kind"] == "blank")
    _put(
        api_client,
        user,
        job_id,
        api_client.get(job_url(job_id), headers=user.token).json()["draft"],
        flagResolutions={blank: "kept"},
    )
    response = api_client.post(
        job_url(job_id, "commit"), json={"draftVersion": job_row(job_id)["draft_version"]}, headers=user.token
    )
    assert response.status_code == 201, response.text
    recipe = api_client.get(api_routes.recipes_slug(response.json()["slug"]), headers=user.token).json()
    committed = recipe["recipeIngredient"][2]
    assert (committed["quantity"], committed["unit"]["name"], committed["food"]["name"]) == (1, "cup", "brown sugar")
    assert committed["originalText"] == "1 C. brown sugar"


def test_the_pages_unparsed_copy_of_the_line_changes_nothing(api_client: TestClient, unique_user_fn_scoped: TestUser):
    # until it reloads, the page still holds the line as it typed it, and sends that with its next saves
    user = unique_user_fn_scoped
    job_id = _card(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["ingredients"][2] = _fill(draft["ingredients"][2], "1 C. brown sugar")
    first = _put(api_client, user, job_id, draft)
    parsed = job_row(job_id)["draft"]["ingredients"][2]

    again = _put(api_client, user, job_id, draft)
    assert again["draftVersion"] == first["draftVersion"]  # the same draft: not an edit
    assert job_row(job_id)["draft"]["ingredients"][2] == parsed

    # an edit of it in that copy is parsed again
    draft["ingredients"][2] = _fill(draft["ingredients"][2], "2 C. packed brown sugar")
    _put(api_client, user, job_id, draft)
    line = job_row(job_id)["draft"]["ingredients"][2]
    assert (line["quantity"], line["unit"]["name"], line["original_text"]) == (2, "cup", "2 C. packed brown sugar")


def test_the_reviewers_own_amount_unit_and_food_are_kept(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = _card(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["ingredients"][2] = {**_fill(draft["ingredients"][2], "packed"), "quantity": 2, "food": {"name": "sugar"}}
    draft["ingredients"][0] = {**draft["ingredients"][0], "unit": None, "food": None, "quantity": None}  # cleared
    _put(api_client, user, job_id, draft)

    sugar, oil = job_row(job_id)["draft"]["ingredients"][2], job_row(job_id)["draft"]["ingredients"][0]
    assert (sugar["quantity"], sugar["unit"], sugar["food"]["name"], sugar["note"]) == (2, None, "sugar", "packed")
    # a line cleared to its note is the reviewer's text: its note is unchanged, so it isn't parsed
    assert (oil["quantity"], oil["unit"], oil["food"], oil["note"]) == (None, None, None, "melted")


def test_new_and_edited_text_lines_are_parsed_but_unparseable_ones_stay_as_sent(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    job_id = _card(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["ingredients"][3] = _fill(draft["ingredients"][3], "1/2 t. black pepper")
    draft["ingredients"].append({"note": "2 T. butter", "display": "2 T. butter", "originalText": ""})
    draft["ingredients"].append({"note": "[illegible] flour", "display": "[illegible] flour"})
    _put(api_client, user, job_id, draft)

    lines = job_row(job_id)["draft"]["ingredients"]
    assert (lines[3]["quantity"], lines[3]["unit"]["name"], lines[3]["food"]["name"]) == (
        0.5,
        "teaspoon",
        "black pepper",
    )
    assert (lines[4]["quantity"], lines[4]["unit"]["name"], lines[4]["food"]["name"]) == (2, "tablespoon", "butter")
    # a marker still in the line: it stays text, flagged, until it's filled or kept
    assert (lines[5]["quantity"], lines[5]["food"], lines[5]["note"]) == (None, None, "[illegible] flour")


def test_a_card_in_another_language_isnt_parsed(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = _card(user, language="de")
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["ingredients"][2] = _fill(draft["ingredients"][2], "1 C. Zucker")
    _put(api_client, user, job_id, draft)

    line = job_row(job_id)["draft"]["ingredients"][2]
    assert (line["quantity"], line["unit"], line["food"], line["note"]) == (None, None, None, "1 C. Zucker")


def test_a_parser_failure_never_loses_the_edit(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the parser's model couldn't be loaded")

    monkeypatch.setattr(review, "normalize_lines", broken)
    user = unique_user_fn_scoped
    job_id = _card(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["ingredients"][2] = _fill(draft["ingredients"][2], "1 C. brown sugar")
    saved = _put(api_client, user, job_id, draft)

    assert saved["draftVersion"] == 2
    line = job_row(job_id)["draft"]["ingredients"][2]
    assert (line["quantity"], line["unit"], line["food"], line["note"]) == (None, None, None, "1 C. brown sugar")


def test_a_failed_parse_logs_no_card_text(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A database error's text holds its parameters, here names typed from the card: neither it nor a traceback is
    logged (§10), only the job and the error's type"""

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("INSERT INTO ingredient_foods (name) VALUES ('brown sugar') failed")

    monkeypatch.setattr(review, "normalize_lines", broken)
    user = unique_user_fn_scoped
    job_id = _card(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["ingredients"][2] = _fill(draft["ingredients"][2], "1 C. brown sugar")

    with caplog.at_level("DEBUG"):
        _put(api_client, user, job_id, draft)

    records = [r for r in caplog.records if r.levelno >= logging.WARNING and str(job_id) in r.getMessage()]
    assert [(record.levelname, record.exc_info) for record in records] == [("WARNING", None)]
    assert "(ValueError)" in records[0].getMessage()
    assert "brown" not in caplog.text and "sugar" not in caplog.text
    assert "Traceback" not in caplog.text
