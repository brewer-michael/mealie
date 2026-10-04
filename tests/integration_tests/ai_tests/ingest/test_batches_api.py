"""
Batches over the API (docs/ai/PHASE2.md §1.4, §14): app batches created and sealed by the app, positions from the
form, a sealed batch never gaining a card, API uploads auto-joining within 2 minutes, `batchId=new`, the notification
check after a seal, an upload's `done=true` sealing its batch, and the capture page's heartbeat keeping its batch open.
"""

import base64
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch
from mealie.repos.repository_recipe_ingest import utcnow
from mealie.routes.ai.ingest import upload as upload_routes
from mealie.services import ocr
from mealie.services.ai.ingest import batches, events, limits
from tests.integration_tests.ai_tests.ingest.card_flow_testing import (
    READY_EVENT,
    Notified,
    all_settled,
    apprise_sent,  # noqa: F401  (the fixture)
    dispatch_once,
    dispatch_until,
    make_card_reader,
    make_notifier,
    only_households,
    photo,
    quiet_other_phases,
    sent_to,
    upload,
)
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
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import FakeCardAI, banana_answers
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


def _age_batch(batch_id: str, seconds: int, *, created: bool = False) -> None:
    """Moves the batch's last upload (and, `created`, its creation) into the past"""
    then = utcnow() - timedelta(seconds=seconds)
    with session_context() as session:
        session.execute(
            sa.update(RecipeIngestionBatch)
            .where(RecipeIngestionBatch.id == UUID(batch_id))
            .values(last_upload_at=then, **({"created_at": then} if created else {}))
        )
        session.commit()


def _seal_idle() -> list[UUID]:
    """The dispatcher's housekeeping seal, now"""
    with session_context() as session:
        return batches.seal_idle_batches(session, utcnow())


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


# ==================================================================================================================
# done=true: the upload that ends its batch


def test_done_seals_the_batch_with_this_card(api_client: TestClient, unique_user_fn_scoped: TestUser, notified: list):
    user = unique_user_fn_scoped
    configure_card_reading(user)

    first = post_card(api_client, user, jpeg())
    last = post_card(api_client, user, jpeg(), done=True)
    assert last.status_code == 202
    batch_id = last.json()["batchId"]
    assert first.json()["batchId"] == batch_id  # the card joined the batch, then ended it
    assert batch_row(batch_id).sealed_at is not None
    assert notified == [UUID(batch_id)]  # the notification goes out once its cards are read

    shown = api_client.get(f"{BATCHES}/{batch_id}", headers=user.token).json()
    assert shown["sealedAt"] is not None
    assert len(shown["jobs"]) == 2

    # the next card starts a new batch, without waiting two minutes
    after = post_card(api_client, user, jpeg())
    assert after.json()["batchId"] != batch_id


@pytest.mark.parametrize("done", [None, False])
def test_without_done_the_batch_stays_open(api_client: TestClient, reader: TestUser, notified: list, done: bool | None):
    fields = {} if done is None else {"done": done}
    response = post_card(api_client, reader, jpeg(), batchId="new", **fields)
    assert response.status_code == 202
    assert batch_row(response.json()["batchId"]).sealed_at is None
    assert notified == []


def test_done_seals_an_app_batch_too(api_client: TestClient, reader: TestUser, notified: list):
    batch_id = api_client.post(BATCHES, headers=reader.token).json()["id"]
    assert post_card(api_client, reader, jpeg(), batchId=batch_id, position=0).status_code == 202
    assert post_card(api_client, reader, jpeg(), batchId=batch_id, position=1, done="true").status_code == 202
    assert batch_row(batch_id).sealed_at is not None
    assert notified == [UUID(batch_id)]


def test_done_in_the_query_and_in_json(api_client: TestClient, reader: TestUser, notified: list):
    raw = api_client.post(
        f"{INGEST}?batchId=new&done=1", content=jpeg(), headers={**reader.token, "Content-Type": "image/jpeg"}
    )
    assert raw.status_code == 202
    assert batch_row(raw.json()["batchId"]).sealed_at is not None

    image = base64.b64encode(jpeg()).decode()
    as_json = api_client.post(
        INGEST, json={"images": [{"data": image}], "batchId": "new", "done": True}, headers=reader.token
    )
    assert as_json.status_code == 202
    assert batch_row(as_json.json()["batchId"]).sealed_at is not None
    assert notified == [UUID(raw.json()["batchId"]), UUID(as_json.json()["batchId"])]


def test_a_refused_upload_seals_nothing(api_client: TestClient, reader: TestUser, notified: list):
    batch_id = api_client.post(BATCHES, headers=reader.token).json()["id"]
    assert post_card(api_client, reader, jpeg(), batchId=batch_id).status_code == 202

    refused = post_card(api_client, reader, b"not an image", batchId=batch_id, done=True)
    assert refused.status_code == 400
    assert refused.json()["detail"]["code"] == "nothing_accepted"
    assert batch_row(batch_id).sealed_at is None

    bad = post_card(api_client, reader, jpeg(), batchId=batch_id, position="last", done=True)
    assert bad.status_code == 400
    assert batch_row(batch_id).sealed_at is None
    assert notified == []


@pytest.fixture()
def card_reader(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch) -> TestUser:
    """A household whose group reads cards with a fake image provider, read by the test's own dispatcher"""
    make_card_reader(unique_user_fn_scoped)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    only_households(monkeypatch, unique_user_fn_scoped.household_id)
    quiet_other_phases(monkeypatch)
    FakeCardAI(banana_answers()).install(monkeypatch)
    return unique_user_fn_scoped


def test_done_sends_one_notification_once_its_cards_are_read(
    api_client: TestClient,
    card_reader: TestUser,
    apprise_sent: list[Notified],  # noqa: F811
):
    user = card_reader
    home_assistant = make_notifier(api_client, user, cards_ready=True)

    first = upload(api_client, user, photo("1 front"))
    last = upload(api_client, user, photo("2 front"), done=True)
    batch_id = last["batchId"]
    assert first["batchId"] == batch_id
    assert batch_row(batch_id).sealed_at is not None
    assert sent_to(apprise_sent, home_assistant, READY_EVENT) == []  # its cards are still being read

    job_ids = [first["jobs"][0]["id"], last["jobs"][0]["id"]]
    dispatch_until(all_settled(job_ids))
    assert len(sent_to(apprise_sent, home_assistant, READY_EVENT)) == 1
    assert batch_row(batch_id).notified_at is not None

    # nothing sends it again
    events.housekeeping(utcnow())
    dispatch_once()
    assert len(sent_to(apprise_sent, home_assistant, READY_EVENT)) == 1


# ==================================================================================================================
# The capture page's heartbeat


def test_a_touch_moves_the_last_upload(api_client: TestClient, reader: TestUser):
    batch_id = api_client.post(BATCHES, headers=reader.token).json()["id"]
    _age_batch(batch_id, 5 * 60)
    before = batch_row(batch_id).last_upload_at

    touched = api_client.post(f"{BATCHES}/{batch_id}/touch", headers=reader.token)
    assert touched.status_code == 200
    assert touched.json()["id"] == batch_id
    assert touched.json()["sealedAt"] is None
    after = batch_row(batch_id).last_upload_at
    assert after is not None and before is not None
    assert after - before >= timedelta(seconds=5 * 60 - 5)


def test_an_open_capture_page_keeps_its_batch_open(api_client: TestClient, reader: TestUser):
    # 11 minutes after the batch was started, with no card since: idle by the app's 10 minutes, but the page is open
    batch_id = api_client.post(BATCHES, headers=reader.token).json()["id"]
    _age_batch(batch_id, 11 * 60, created=True)
    assert api_client.post(f"{BATCHES}/{batch_id}/touch", headers=reader.token).status_code == 200

    # two minutes after the heartbeat, the housekeeping seal leaves it open
    _age_batch(batch_id, 2 * 60)
    assert UUID(batch_id) not in _seal_idle()
    assert batch_row(batch_id).sealed_at is None

    # once the page stops touching it, it seals after the app's idle time like any app batch
    _age_batch(batch_id, limits.APP_BATCH_IDLE + 60)
    assert UUID(batch_id) in _seal_idle()


def test_a_sealed_batch_is_409_and_stays_sealed(api_client: TestClient, reader: TestUser, notified: list):
    batch_id = api_client.post(BATCHES, headers=reader.token).json()["id"]
    sealed = api_client.post(f"{BATCHES}/{batch_id}/seal", headers=reader.token).json()
    before = batch_row(batch_id)

    response = api_client.post(f"{BATCHES}/{batch_id}/touch", headers=reader.token)
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "batch_sealed"}  # the page starts a new batch, quietly
    after = batch_row(batch_id)
    assert after.sealed_at is not None
    assert after.sealed_at == before.sealed_at
    assert after.last_upload_at == before.last_upload_at
    assert sealed["sealedAt"] is not None


def test_only_the_callers_own_app_batch_can_be_touched(
    api_client: TestClient, reader: TestUser, h2_user: TestUser, user_tuple: list[TestUser]
):
    def touch(user: TestUser, batch_id: str | UUID) -> int:
        response = api_client.post(f"{BATCHES}/{batch_id}/touch", headers=user.token)
        if response.status_code == 404:
            assert response.json()["detail"] == {"code": "not_found"}
        return response.status_code

    assert touch(reader, uuid4()) == 404

    # another household's
    theirs = api_client.post(BATCHES, headers=h2_user.token).json()["id"]
    _age_batch(theirs, 60)
    assert touch(reader, theirs) == 404
    last_upload = batch_row(theirs).last_upload_at
    assert last_upload is not None and last_upload.replace(tzinfo=None) < utcnow() - timedelta(seconds=30)

    # another member's, in the same household
    owner, other = user_tuple
    mine = api_client.post(BATCHES, headers=owner.token).json()["id"]
    assert touch(other, mine) == 404
    assert touch(owner, mine) == 200

    # an API upload's batch isn't a capture page's
    api_batch = post_card(api_client, reader, jpeg(), batchId="new").json()["batchId"]
    assert touch(reader, api_batch) == 404


def test_a_touch_waits_while_paused(api_client: TestClient, reader: TestUser, paused: object):  # noqa: F811
    response = api_client.post(f"{BATCHES}/{uuid4()}/touch", headers=reader.token)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "paused_for_restore"
