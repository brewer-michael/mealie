"""
Saving a draft from the review page (docs/ai/PHASE2.md §4.6, §6.6): versions, flag resolutions, proposals, the error
banner, and errors blocking commit until they're kept. Runs on SQLite and PostgreSQL.
"""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import assert_code, banana_draft, job_row, job_url, seed_job, set_columns, use_fake_flags

from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import update_job_json
from mealie.schema.recipe_ingest import (
    CardDraftNote,
    CardDraftStep,
    CardProposal,
    CardProposalKind,
    IngestErrorCode,
    IngestStatus,
    ProposalTarget,
)
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


def _put(api_client: TestClient, user: TestUser, job_id: UUID, version: int, **body: Any):
    draft = body.pop("draft", None) or api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    return api_client.put(job_url(job_id), json={"draftVersion": version, "draft": draft, **body}, headers=user.token)


def test_save_bumps_the_version_and_recomputes_flags(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    assert job_row(job_id)["error_count"] == 1

    # the reviewer types the microwave time into the blank
    draft["steps"][1]["text"] = "Microwave for 2 minutes."
    draft["name"] = "Banana Mug Cake for One"
    response = _put(api_client, user, job_id, 1, draft=draft)
    assert response.status_code == 200
    saved = response.json()
    assert saved["draftVersion"] == 2
    assert saved["errorCount"] == 0
    assert [flag["kind"] for flag in saved["flags"]] == ["new_food", "new_food"]

    row = job_row(job_id)
    assert row["draft_version"] == 2
    assert row["extracted_version"] == 1  # an edited draft: a re-extract will propose rather than replace
    assert row["title"] == "Banana Mug Cake for One"
    assert row["draft"]["steps"][1]["text"] == "Microwave for 2 minutes."
    assert (row["error_count"], row["warning_count"]) == (0, 0)

    job = api_client.get(job_url(job_id), headers=user.token).json()
    assert job["draftVersion"] == 2
    assert job["title"] == "Banana Mug Cake for One"


def test_a_stale_version_is_a_409_with_the_current_one(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    assert _put(api_client, user, job_id, 1, draft={**draft, "name": "Banana Cake"}).status_code == 200

    detail = assert_code(_put(api_client, user, job_id, 1, draft=draft), 409, "version_conflict")
    assert detail["current"] == 2
    assert job_row(job_id)["draft_version"] == 2


def test_only_a_ready_draft_can_be_saved(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user, status=IngestStatus.committed)
    draft = banana_draft().model_dump(mode="json", by_alias=True)
    detail = assert_code(_put(api_client, user, job_id, 1, draft=draft), 409, "invalid_status")
    assert detail["status"] == "committed"

    response = _put(api_client, user, uuid4(), 1, draft=draft)
    assert_code(response, 404, "not_found")


def test_the_draft_is_the_whitelist(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft.update({"id": str(uuid4()), "slug": "hijack", "assets": [{"name": "x"}], "settings": {"public": True}})

    assert _put(api_client, user, job_id, 1, draft=draft).status_code == 200
    stored = job_row(job_id)["draft"]
    assert not {"id", "slug", "assets", "settings"} & set(stored)

    # the request itself is strict
    response = _put(api_client, user, job_id, 2, draft=draft, recipeId=str(uuid4()))
    assert response.status_code == 422


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_quantity_is_refused(api_client: TestClient, unique_user_fn_scoped: TestUser, bad: float):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["ingredients"][0]["quantity"] = bad
    # the test client won't send NaN, but Python's JSON parser on the server reads it
    body = json.dumps({"draftVersion": 1, "draft": draft})
    headers = {**user.token, "Content-Type": "application/json"}

    assert api_client.put(job_url(job_id), content=body, headers=headers).status_code == 422
    assert job_row(job_id)["draft_version"] == 1


def test_flag_resolutions(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    draft = banana_draft()
    draft.ingredients[1].parse_confidence = 0.5  # a warning to dismiss
    job_id = seed_job(user, draft=draft)
    flags = {flag["kind"]: flag for flag in api_client.get(job_url(job_id), headers=user.token).json()["flags"]}
    blank, check_parse, new_food = flags["blank"]["id"], flags["check_parse"]["id"], flags["new_food"]["id"]
    assert job_row(job_id)["warning_count"] == 1

    resolutions = {
        blank: "kept",  # an error that can be kept as written
        check_parse: "dismissed",  # a warning: "Looks right"
        new_food: "dismissed",  # an info can't be dismissed
        "unsure:steps:nowhere": "dismissed",  # no such flag
    }
    saved = _put(api_client, user, job_id, 1, flagResolutions=resolutions).json()
    by_id = {flag["id"]: flag["resolution"] for flag in saved["flags"]}
    assert by_id[blank] == "kept"
    assert by_id[check_parse] == "dismissed"
    assert by_id[new_food] is None
    assert (saved["errorCount"], saved["warningCount"]) == (0, 0)
    assert saved["draftVersion"] == 1  # resolving flags doesn't change the draft

    # resolutions are kept by later saves that don't mention them, and taken back with null
    saved = _put(api_client, user, job_id, 1).json()
    assert {flag["id"]: flag["resolution"] for flag in saved["flags"]}[blank] == "kept"
    saved = _put(api_client, user, job_id, 1, flagResolutions={blank: None, check_parse: "kept"}).json()
    by_id = {flag["id"]: flag["resolution"] for flag in saved["flags"]}
    assert by_id[blank] is None
    assert by_id[check_parse] is None  # a warning can't be kept
    assert (saved["errorCount"], saved["warningCount"]) == (1, 1)


def test_only_a_changed_draft_bumps_the_version(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """
    §3.3: `draftVersion` is bumped only when the draft changes. Resolving flags, settling proposals or dismissing the
    banner keeps it, so the draft still counts as unedited (a re-extract replaces it) and another device's next save
    doesn't conflict.
    """
    user = unique_user_fn_scoped
    proposal = CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="name"), text="Banana Cake")
    job_id = seed_job(user, proposals=[proposal], error_code=IngestErrorCode.provider_failed.value)
    blank = api_client.get(job_url(job_id), headers=user.token).json()["flags"][-1]["id"]

    for body in (
        {"flagResolutions": {blank: "kept"}},
        {"resolvedProposalIds": [str(proposal.id)]},
        {"clearError": True},
    ):
        saved = _put(api_client, user, job_id, 1, **body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["draftVersion"] == 1
    row = job_row(job_id)
    assert (row["draft_version"], row["extracted_version"]) == (1, 1)
    assert (row["proposals"], row["error_code"], row["error_count"]) == ([], None, 0)

    # the other device's edit, made against version 1, still lands
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    saved = _put(api_client, user, job_id, 1, draft={**draft, "name": "Banana Cake"})
    assert saved.json()["draftVersion"] == 2
    assert {flag["id"]: flag["resolution"] for flag in saved.json()["flags"]}[blank] == "kept"
    assert job_row(job_id)["extracted_version"] == 1


def test_a_missing_name_can_only_be_fixed(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user, draft=banana_draft(name="", steps=[CardDraftStep(text="Bake.")]))
    flag_id = api_client.get(job_url(job_id), headers=user.token).json()["flags"][0]["id"]

    saved = _put(api_client, user, job_id, 1, flagResolutions={flag_id: "kept"}).json()
    assert saved["flags"][0]["kind"] == "missing_name"
    assert saved["flags"][0]["resolution"] is None
    assert saved["errorCount"] == 1


def test_errors_block_commit_until_kept(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    blank = api_client.get(job_url(job_id), headers=user.token).json()["flags"][-1]

    detail = assert_code(
        api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1}, headers=user.token),
        422,
        "unresolved_flags",
    )
    assert [flag["id"] for flag in detail["flags"]] == [blank["id"]]
    assert job_row(job_id)["status"] == "ready"

    assert _put(api_client, user, job_id, 1, flagResolutions={blank["id"]: "kept"}).status_code == 200
    response = api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1}, headers=user.token)
    assert response.status_code == 201, response.text


def test_resolved_proposals_are_removed(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    used = CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="name"), text="Banana Cake")
    pending = CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="description"), text="Quick")
    job_id = seed_job(user, proposals=[used, pending])

    saved = _put(api_client, user, job_id, 1, resolvedProposalIds=[str(used.id)])
    assert saved.status_code == 200
    assert [proposal["id"] for proposal in job_row(job_id)["proposals"]] == [str(pending.id)]
    state = api_client.get(job_url(job_id, "state"), headers=user.token).json()
    assert state["proposalIds"] == [str(pending.id)]


def test_clear_error_dismisses_the_banner(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user, error_code=IngestErrorCode.provider_failed.value, error_params={"detail": "x"})

    assert _put(api_client, user, job_id, 1).status_code == 200
    assert job_row(job_id)["error_code"] == "provider_failed"  # only when asked

    assert _put(api_client, user, job_id, 1, clearError=True).status_code == 200
    row = job_row(job_id)
    assert (row["error_code"], row["error_params"]) == (None, None)
    assert row["draft_version"] == 1


def test_a_save_answers_the_possible_duplicates_of_the_saved_name(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """The banner follows a rename: the save's answer says whether the saved name is a recipe's or a waiting card's"""
    user = unique_user_fn_scoped
    response = api_client.post(api_routes.recipes, json={"name": "Zucchini Bread"}, headers=user.token)
    assert response.status_code == 201, response.text
    recipe = api_client.get(api_routes.recipes_slug(response.json()), headers=user.token).json()
    waiting = seed_job(user, draft=banana_draft(name="Apple Crisp"))
    job_id = seed_job(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]

    renamed = _put(api_client, user, job_id, 1, draft={**draft, "name": "Zucchini Bread"}).json()
    assert renamed["duplicateOf"] == {"id": recipe["id"], "slug": "zucchini-bread", "name": "Zucchini Bread"}
    assert (renamed["duplicateJob"], renamed["duplicateName"]) == (None, "Zucchini Bread (1)")

    again = _put(api_client, user, job_id, 2, draft={**draft, "name": "apple  crisp"}).json()
    assert (again["duplicateOf"], again["duplicateName"]) == (None, None)
    assert again["duplicateJob"] == {"id": str(waiting), "title": "Apple Crisp"}

    away = _put(api_client, user, job_id, 3, draft={**draft, "name": "Grandma's Banana Mug Cake"}).json()
    assert (away["duplicateOf"], away["duplicateJob"], away["duplicateName"]) == (None, None, None)
    assert away["ingredients"] is None  # nothing was parsed


def test_a_tasks_proposal_never_conflicts_with_a_save(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """A re-read finishing mid-edit changes `row_version` but not `draftVersion`: the save still lands"""
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    proposal = CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="name"), text="Banana Cake")
    with session_context() as session:
        update_job_json(session, job_id, lambda row: {"proposals": [*(row["proposals"] or []), proposal]})
    assert job_row(job_id)["row_version"] == 1

    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    response = _put(api_client, user, job_id, 1, draft={**draft, "name": "Banana Cake"})
    assert response.status_code == 200
    assert response.json()["draftVersion"] == 2
    assert [p["id"] for p in job_row(job_id)["proposals"]] == [str(proposal.id)]


def test_duplicate_ingredient_and_step_ids_are_made_unique(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["ingredients"].append(dict(draft["ingredients"][0]))
    draft["steps"].append(dict(draft["steps"][0]))

    assert _put(api_client, user, job_id, 1, draft=draft).status_code == 200
    stored = job_row(job_id)["draft"]
    assert len({ingredient["reference_id"] for ingredient in stored["ingredients"]}) == 3
    assert len({step["id"] for step in stored["steps"]}) == 3


def test_ready_only_while_not_committing(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    set_columns(job_id, status=IngestStatus.committing.value)
    assert_code(_put(api_client, user, job_id, 1), 409, "invalid_status")


# ==================================================================================================================
# Note ids (flags on notes are keyed to them)


def _notes(job_id: UUID) -> list[dict[str, Any]]:
    return job_row(job_id)["draft"]["notes"]


def test_notes_keep_their_ids_through_saves(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user, draft=banana_draft(notes=[CardDraftNote(text="Grandma's favourite")]))
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    note_id = draft["notes"][0]["id"]
    assert note_id == _notes(job_id)[0]["id"]

    # the text is edited and a note is added without an id: the first keeps its id, the new one gets one
    draft["notes"][0]["text"] = "Grandma's favourite, every Sunday"
    draft["notes"].append({"title": "Tip", "text": "Use a big mug"})
    assert _put(api_client, user, job_id, 1, draft=draft).status_code == 200
    stored = _notes(job_id)
    assert stored[0]["id"] == note_id
    assert stored[1]["id"] and stored[1]["id"] != note_id

    # what the page reads back is what it saves next, and the ids stay
    again = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    assert [note["id"] for note in again["notes"]] == [note["id"] for note in stored]
    assert _put(api_client, user, job_id, 2, draft=again).status_code == 200
    assert [note["id"] for note in _notes(job_id)] == [note["id"] for note in stored]


def test_a_pasted_note_gets_a_fresh_id(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user, draft=banana_draft(notes=[CardDraftNote(text="Tip")]))
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    pasted = draft["notes"][0]["id"]
    draft["notes"].append(dict(draft["notes"][0]))  # the same id twice

    assert _put(api_client, user, job_id, 1, draft=draft).status_code == 200
    stored = job_row(job_id)["draft"]
    assert stored["notes"][0]["id"] == pasted  # the first keeps it
    assert stored["notes"][1]["id"] not in (pasted, None)

    # ids are unique across ingredients, steps and notes: a step's id on a note is replaced on the note
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["notes"][1]["id"] = draft["steps"][0]["id"]
    assert _put(api_client, user, job_id, 2, draft=draft).status_code == 200
    stored = job_row(job_id)["draft"]
    ids = [line["reference_id"] for line in stored["ingredients"]]
    ids += [step["id"] for step in stored["steps"]] + [note["id"] for note in stored["notes"]]
    assert len(set(ids)) == len(ids) == 6
    assert stored["steps"][0]["id"] == draft["steps"][0]["id"]


def test_a_draft_stored_before_notes_had_ids(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """A version 1 draft reads with the same note ids every time; its first save keeps them, and isn't an edit"""
    user = unique_user_fn_scoped
    job_id = seed_job(user, draft=banana_draft(notes=[CardDraftNote(text="Tip"), CardDraftNote(text="Tip")]))
    stored = job_row(job_id)["draft"]
    stored["schema_version"] = 1
    for note in stored["notes"]:
        del note["id"]
    set_columns(job_id, draft=stored)

    first = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    second = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    ids = [note["id"] for note in first["notes"]]
    assert ids == [note["id"] for note in second["notes"]]
    assert len(set(ids)) == 2

    saved = _put(api_client, user, job_id, 1, draft=first)
    assert saved.status_code == 200
    assert saved.json()["draftVersion"] == 1  # nothing was edited: the card still counts as unedited
    row = job_row(job_id)
    assert row["draft"]["schema_version"] == 3
    assert [note["id"] for note in row["draft"]["notes"]] == ids
