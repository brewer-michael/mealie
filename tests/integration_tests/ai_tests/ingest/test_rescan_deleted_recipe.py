"""
Scanning a card again after its recipe was deleted (docs/ai/PHASE2.md §2 Duplicates): a committed card counts as
already scanned only while its recipe exists. Through the real API: the card is uploaded, read by a dispatcher with a
fake provider, reviewed and committed; sent again it's a duplicate; once the recipe is deleted, the same photo is a new
card. Runs on SQLite and PostgreSQL.
"""

import pytest
from fastapi.testclient import TestClient

from mealie.services import ocr
from tests.integration_tests.ai_tests.ingest.card_flow_testing import (
    INGEST,
    JOBS,
    card_files,
    dispatch_until,
    job_status,
    make_card_reader,
    only_households,
    photo,
    quiet_other_phases,
    upload,
)
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import FakeCardAI, banana_answers
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser


@pytest.fixture()
def user(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch) -> TestUser:
    make_card_reader(unique_user_fn_scoped)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    only_households(monkeypatch, unique_user_fn_scoped.household_id)
    quiet_other_phases(monkeypatch)
    FakeCardAI(banana_answers()).install(monkeypatch)
    return unique_user_fn_scoped


def _commit(api_client: TestClient, user: TestUser, job_id: str) -> str:
    """Reviews the read card (filling the blank the banana card leaves) and commits it; the recipe's slug"""
    job = api_client.get(f"{JOBS}/{job_id}", headers=user.token).json()
    draft = job["draft"]
    draft["steps"][-1]["text"] = draft["steps"][-1]["text"].replace("[blank]", "2")
    saved = api_client.put(
        f"{JOBS}/{job_id}", json={"draftVersion": job["draftVersion"], "draft": draft}, headers=user.token
    )
    assert saved.status_code == 200, saved.text
    committed = api_client.post(
        f"{JOBS}/{job_id}/commit", json={"draftVersion": saved.json()["draftVersion"]}, headers=user.token
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["slug"]


def test_a_card_whose_recipe_was_deleted_can_be_scanned_again(api_client: TestClient, user: TestUser):
    card = photo("Banana Mug Cake")
    [first] = upload(api_client, user, card)["jobs"]
    dispatch_until(lambda: job_status(first["id"]) != "processing")
    assert job_status(first["id"]) == "ready"
    slug = _commit(api_client, user, first["id"])

    # while the recipe exists, the card is already scanned
    again = api_client.post(INGEST, files=card_files(card), headers=user.token)
    assert again.status_code == 400, again.text
    [rejected] = again.json()["detail"]["rejected"]
    assert (rejected["reason"], rejected["duplicateOf"]) == ("duplicate", first["id"])

    deleted = api_client.delete(api_routes.recipes_slug(slug), headers=user.token)
    assert deleted.status_code == 200, deleted.text

    # once it's gone, the same photo is a new card, read again
    [second] = upload(api_client, user, card)["jobs"]
    assert second["id"] != first["id"]
    dispatch_until(lambda: job_status(second["id"]) != "processing")
    assert job_status(second["id"]) == "ready"
    assert job_status(first["id"]) == "committed"  # the old card keeps its history

    # and the new card is the duplicate now
    third = api_client.post(INGEST, files=card_files(card), headers=user.token)
    assert third.status_code == 400, third.text
    assert third.json()["detail"]["rejected"][0]["duplicateOf"] == second["id"]
