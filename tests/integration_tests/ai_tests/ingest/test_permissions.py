"""
Who can do what to a recipe card (docs/ai/PHASE2.md §9): every job route and page image is household-scoped, so
another household's card is a 404, in the same group or another; and discarding is for the uploader, anyone for an
inbox card or one sent with an API token, otherwise the household's managers. Runs on SQLite and PostgreSQL.
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import JOBS, assert_code, household_member, job_row, job_url, seed_job, use_fake_flags

from mealie.schema.recipe_ingest import IngestSource, IngestStatus
from mealie.services.ai.ingest import storage
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


def _every_route(api_client: TestClient, job_id: UUID, headers: dict[str, str]) -> dict[str, Any]:
    region = {"page": 0, "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5, "target": {"field": "name"}}
    draft = {"name": "Mine now"}
    return {
        "get": api_client.get(job_url(job_id), headers=headers),
        "state": api_client.get(job_url(job_id, "state"), headers=headers),
        "save": api_client.put(job_url(job_id), json={"draftVersion": 1, "draft": draft}, headers=headers),
        "reextract": api_client.post(job_url(job_id, "reextract"), headers=headers),
        "reread": api_client.post(job_url(job_id, "reread"), json=region, headers=headers),
        "retry": api_client.post(job_url(job_id, "retry"), headers=headers),
        "cancel": api_client.post(job_url(job_id, "cancel"), headers=headers),
        "rotate": api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=headers),
        "page": api_client.get(job_url(job_id, "pages", 0, "page"), headers=headers),
        "view": api_client.get(job_url(job_id, "pages", 0, "view"), headers=headers),
        "thumb": api_client.get(job_url(job_id, "pages", 0, "thumb"), headers=headers),
        "commit": api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1}, headers=headers),
        "discard": api_client.delete(job_url(job_id), headers=headers),
    }


@pytest.mark.parametrize("stranger", ["h2_user", "g2_user"])
def test_another_households_card_is_a_404_everywhere(
    stranger: str, api_client: TestClient, unique_user: TestUser, request: pytest.FixtureRequest
):
    other: TestUser = request.getfixturevalue(stranger)
    job_id = seed_job(unique_user)
    failed = seed_job(unique_user, status=IngestStatus.failed)
    before = job_row(job_id)

    for name, response in [
        *_every_route(api_client, job_id, other.token).items(),
        ("retry a failed card", api_client.post(job_url(failed, "retry"), headers=other.token)),
    ]:
        assert response.status_code == 404, name
        assert response.json()["detail"] == {"code": "not_found"}, name

    listed = api_client.get(JOBS, params={"perPage": -1}, headers=other.token).json()["items"]
    assert str(job_id) not in {item["id"] for item in listed}
    counts = api_client.get(f"{JOBS}/counts", headers=other.token).json()
    assert counts == {"processing": 0, "ready": 0, "needsAttention": 0, "failed": 0}

    # nothing changed
    after = job_row(job_id)
    for column in ("status", "draft_version", "row_version", "task_state", "pages", "draft"):
        assert after[column] == before[column], column
    assert storage.job_dir(UUID(unique_user.group_id), job_id).is_dir()
    assert job_row(failed)["status"] == "failed"

    # while the household itself still sees it all
    assert api_client.get(job_url(job_id, "pages", 0, "thumb"), headers=unique_user.token).status_code == 200


def test_who_can_discard(api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser):
    manager = unique_user_fn_scoped  # registered their own group and household: can manage it
    member = household_member(api_client, admin_token, manager)
    other_member = household_member(api_client, admin_token, manager)

    managers_card = seed_job(manager)
    others_app_card = seed_job(manager, source=IngestSource.app, created_by=other_member.user_id)
    members_card = seed_job(member, created_by=member.user_id)
    inbox_card = seed_job(manager, source=IngestSource.inbox, created_by=None)
    # sent by Home Assistant or a Shortcut with another user's API token (often one shared "kitchen" user)
    api_card = seed_job(manager, source=IngestSource.api, created_by=other_member.user_id)

    # someone else's card from the app: its uploader or a manager
    assert_code(api_client.delete(job_url(managers_card), headers=member.token), 403, "forbidden")
    assert_code(api_client.delete(job_url(others_app_card), headers=member.token), 403, "forbidden")
    listed = api_client.get(JOBS, params={"perPage": -1}, headers=member.token).json()["items"]
    can_discard = {item["id"]: item["canDiscard"] for item in listed}
    assert can_discard[str(others_app_card)] is False
    assert can_discard[str(api_card)] is True
    assert can_discard[str(inbox_card)] is True
    assert api_client.get(job_url(api_card), headers=member.token).json()["permissions"]["canDiscard"] is True
    assert job_row(managers_card)["status"] == "ready"
    assert storage.job_dir(UUID(manager.group_id), managers_card).is_dir()

    assert api_client.delete(job_url(members_card), headers=member.token).status_code == 204  # the uploader
    assert api_client.delete(job_url(inbox_card), headers=member.token).status_code == 204  # anyone
    assert api_client.delete(job_url(api_card), headers=member.token).status_code == 204  # anyone
    assert api_client.delete(job_url(others_app_card), headers=other_member.token).status_code == 204  # the uploader
    assert api_client.delete(job_url(managers_card), headers=manager.token).status_code == 204
    for job_id in (members_card, inbox_card, api_card, others_app_card, managers_card):
        assert job_row(job_id) == {}
        assert not storage.job_dir(UUID(manager.group_id), job_id).exists()


def test_any_member_can_review_and_commit(api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser):
    owner = unique_user_fn_scoped
    member = household_member(api_client, admin_token, owner)
    job_id = seed_job(owner, draft=None, status=IngestStatus.ready)

    assert api_client.get(job_url(job_id), headers=member.token).status_code == 200
    draft = api_client.get(job_url(job_id), headers=member.token).json()["draft"]
    draft["steps"][1]["text"] = "Microwave for 2 minutes."
    saved = api_client.put(job_url(job_id), json={"draftVersion": 1, "draft": draft}, headers=member.token)
    assert saved.status_code == 200
    rotated = api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=member.token)
    assert rotated.status_code == 200
    committed = api_client.post(job_url(job_id, "commit"), json={"draftVersion": 2}, headers=member.token)
    assert committed.status_code == 201, committed.text
