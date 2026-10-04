"""
Batches over the API (docs/ai/PHASE2.md §1.4, §14): app batches created and sealed by the app, positions from the
form, a sealed batch never gaining a card, API uploads auto-joining within 2 minutes, `batchId=new`, and the
notification check after a seal.
"""

from datetime import timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch
from mealie.repos.repository_recipe_ingest import utcnow
from mealie.routes.ai.ingest import upload as upload_routes
from mealie.services.ai.ingest import limits
from tests.integration_tests.ai_tests.ingest.test_upload_api import (
    INGEST,
    batch_row,
    configure_card_reading,
    job_row,
    jpeg,
    no_ocr,  # noqa: F401  (the fixture)
    paused,  # noqa: F401  (the fixture)
    post_card,
)
from tests.utils.fixture_schemas import TestUser

BATCHES = f"{INGEST}/batches"


@pytest.fixture(scope="module")
def reader(unique_user: TestUser) -> TestUser:
    configure_card_reading(unique_user)
    return unique_user


@pytest.fixture()
def notified(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    calls: list[UUID] = []

    def maybe_notify_batch(batch_id: UUID) -> bool:
        calls.append(batch_id)
        return False

    monkeypatch.setattr(upload_routes.events, "maybe_notify_batch", maybe_notify_batch)
    return calls


def _age_batch(batch_id: str, seconds: int) -> None:
    """Moves the batch's last upload into the past"""
    with session_context() as session:
        session.execute(
            sa.update(RecipeIngestionBatch)
            .where(RecipeIngestionBatch.id == UUID(batch_id))
            .values(last_upload_at=utcnow() - timedelta(seconds=seconds))
        )
        session.commit()


def test_an_app_batch_is_created_listed_and_sealed(api_client: TestClient, reader: TestUser, notified: list[UUID]):
    created = api_client.post(BATCHES, json={}, headers=reader.token)
    assert created.status_code == 201
    batch = created.json()
    assert batch["source"] == "app"
    assert batch["sealedAt"] is None
    assert batch["jobs"] == []
    assert batch["counts"] == {"processing": 0, "ready": 0, "needsAttention": 0, "failed": 0}
    batch_id = batch["id"]

    # cards arrive out of order; the form's position is their capture order
    second = post_card(api_client, reader, jpeg(), batchId=batch_id, position=1)
    first = post_card(api_client, reader, jpeg(), jpeg(), batchId=batch_id, position=0)
    assert second.status_code == first.status_code == 202
    assert first.json()["batchId"] == second.json()["batchId"] == batch_id
    assert job_row(first.json()["jobs"][0]["id"]).source == "app"  # the batch's source

    listed = api_client.get(f"{BATCHES}/{batch_id}", headers=reader.token).json()
    assert [job["id"] for job in listed["jobs"]] == [first.json()["jobs"][0]["id"], second.json()["jobs"][0]["id"]]
    assert [job["position"] for job in listed["jobs"]] == [0, 1]
    assert listed["counts"]["processing"] == 2

    sealed = api_client.post(f"{BATCHES}/{batch_id}/seal", json={}, headers=reader.token)
    assert sealed.status_code == 200
    assert sealed.json()["sealedAt"] is not None
    assert notified == [UUID(batch_id)]

    # sealing again changes nothing and sends nothing
    again = api_client.post(f"{BATCHES}/{batch_id}/seal", headers=reader.token)
    assert again.status_code == 200
    assert again.json()["sealedAt"] == sealed.json()["sealedAt"]
    assert notified == [UUID(batch_id)]


def test_a_card_sent_to_a_sealed_batch_starts_a_new_one(api_client: TestClient, reader: TestUser, notified: list):
    batch_id = api_client.post(BATCHES, headers=reader.token).json()["id"]
    assert post_card(api_client, reader, jpeg(), batchId=batch_id, position=0).status_code == 202
    api_client.post(f"{BATCHES}/{batch_id}/seal", headers=reader.token)

    late = post_card(api_client, reader, jpeg(), batchId=batch_id, position=1)
    assert late.status_code == 202
    new_batch_id = late.json()["batchId"]
    assert new_batch_id != batch_id
    assert batch_row(new_batch_id).source == "app"
    assert batch_row(new_batch_id).sealed_at is None
    assert job_row(late.json()["jobs"][0]["id"]).position == 1

    # the sealed batch never gained the card
    assert len(api_client.get(f"{BATCHES}/{batch_id}", headers=reader.token).json()["jobs"]) == 1

    # the next late card follows the app into its new batch
    later = post_card(api_client, reader, jpeg(), batchId=batch_id, position=2)
    assert later.json()["batchId"] == new_batch_id


def test_api_uploads_join_a_batch_within_two_minutes(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    configure_card_reading(user)

    first = post_card(api_client, user, jpeg())
    second = post_card(api_client, user, jpeg())
    batch_id = first.json()["batchId"]
    assert second.json()["batchId"] == batch_id
    assert batch_row(batch_id).source == "api"
    assert [job_row(r.json()["jobs"][0]["id"]).position for r in (first, second)] == [0, 1]

    # batchId=new forces a new batch, which later uploads then join
    forced = post_card(api_client, user, jpeg(), batchId="new")
    assert forced.json()["batchId"] != batch_id
    assert post_card(api_client, user, jpeg()).json()["batchId"] == forced.json()["batchId"]

    # two idle minutes later, a new one
    _age_batch(forced.json()["batchId"], limits.AUTO_BATCH_IDLE + 1)
    _age_batch(batch_id, limits.AUTO_BATCH_IDLE + 1)
    late = post_card(api_client, user, jpeg())
    assert late.json()["batchId"] not in (batch_id, forced.json()["batchId"])


def test_batches_of_another_household_are_404(api_client: TestClient, reader: TestUser, h2_user: TestUser):
    theirs = api_client.post(BATCHES, headers=h2_user.token).json()["id"]
    assert api_client.get(f"{BATCHES}/{theirs}", headers=reader.token).status_code == 404
    response = api_client.post(f"{BATCHES}/{theirs}/seal", headers=reader.token)
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "not_found"}
    assert batch_row(theirs).sealed_at is None
    assert api_client.get(f"{BATCHES}/{theirs}", headers=h2_user.token).status_code == 200


def test_batch_routes_need_a_user(api_client: TestClient):
    assert api_client.post(BATCHES).status_code == 401


def test_batches_wait_while_paused(api_client: TestClient, reader: TestUser, paused: object):  # noqa: F811
    response = api_client.post(BATCHES, headers=reader.token)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "paused_for_restore"
