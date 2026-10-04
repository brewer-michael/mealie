"""
Saving a draft from the review page (docs/ai/PHASE2.md §4.6, §6.6): versions, flag resolutions, proposals, the error
banner, and errors blocking commit until they're kept. Runs on SQLite and PostgreSQL.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import assert_code, banana_draft, job_row, job_url, seed_job, set_columns, use_fake_flags

from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import update_job_json
from mealie.schema.recipe_ingest import (
    CardDraftStep,
    CardProposal,
    CardProposalKind,
    IngestErrorCode,
    IngestStatus,
    ProposalTarget,
)
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
    assert _put(api_client, user, job_id, 1).status_code == 200

    detail = assert_code(_put(api_client, user, job_id, 1), 409, "version_conflict")
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

    # resolutions are kept by later saves that don't mention them, and taken back with null
    saved = _put(api_client, user, job_id, 2).json()
    assert {flag["id"]: flag["resolution"] for flag in saved["flags"]}[blank] == "kept"
    saved = _put(api_client, user, job_id, 3, flagResolutions={blank: None, check_parse: "kept"}).json()
    by_id = {flag["id"]: flag["resolution"] for flag in saved["flags"]}
    assert by_id[blank] is None
    assert by_id[check_parse] is None  # a warning can't be kept
    assert (saved["errorCount"], saved["warningCount"]) == (1, 1)


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
    response = api_client.post(job_url(job_id, "commit"), json={"draftVersion": 2}, headers=user.token)
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

    assert _put(api_client, user, job_id, 2, clearError=True).status_code == 200
    row = job_row(job_id)
    assert (row["error_code"], row["error_params"]) == (None, None)


def test_a_tasks_proposal_never_conflicts_with_a_save(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """A re-read finishing mid-edit changes `row_version` but not `draftVersion`: the save still lands"""
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    proposal = CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="name"), text="Banana Cake")
    with session_context() as session:
        update_job_json(session, job_id, lambda row: {"proposals": [*(row["proposals"] or []), proposal]})
    assert job_row(job_id)["row_version"] == 1

    response = _put(api_client, user, job_id, 1)
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
