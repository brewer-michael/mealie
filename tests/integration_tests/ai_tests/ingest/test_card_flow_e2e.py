"""
A recipe card from the phone to a recipe, across every seam (docs/ai/PHASE2.md §19, §18): the multipart upload with a
Bearer header, Done, a real dispatcher reading it with a fake provider answering the card schemas, the batch's one
"ready" notification through the real listener to a mocked Apprise, the review (GET, then a PUT filling the blank the
card leaves), and the commit: the recipe with the card as assets and cover, and one `recipe_created`. Then Done tapped
while the last card is still uploading: one batch, one notification. Runs on SQLite and PostgreSQL.
"""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from mealie.core.config import get_app_dirs
from mealie.repos.repository_recipe_ingest import utcnow
from mealie.services import ocr
from mealie.services.ai.ingest import events
from mealie.services.event_bus_service.event_types import EventTypes
from tests.integration_tests.ai_tests.ingest.card_flow_testing import (
    BATCHES,
    JOBS,
    READY_EVENT,
    RECIPE_CREATED_EVENT,
    Dispatched,
    InFlightUpload,
    Notified,
    all_settled,
    apprise_sent,  # noqa: F401  (the fixture)
    batch_columns,
    bus_events,  # noqa: F401  (the fixture)
    dispatch_once,
    dispatch_until,
    job_columns,
    job_status,
    make_card_reader,
    make_notifier,
    only_households,
    photo,
    quiet_other_phases,
    seal,
    sent_to,
    start_batch,
    upload,
)
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import BANANA_RECIPE, FakeCardAI, banana_answers
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser

BLANK_STEP = "Microwave in bowl or large mug for [blank] minutes or until firm in center."
FILLED_STEP = "Microwave in bowl or large mug for 2 minutes or until firm in center."


@pytest.fixture()
def user(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch) -> TestUser:
    """A household whose group reads cards with an image provider (a fake answering the card schemas), no Tesseract"""
    make_card_reader(unique_user_fn_scoped)
    monkeypatch.setattr(ocr, "is_available", lambda: False)  # CI has no Tesseract: the image path, deterministically
    only_households(monkeypatch, unique_user_fn_scoped.household_id)
    quiet_other_phases(monkeypatch)
    return unique_user_fn_scoped


@pytest.fixture()
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> FakeCardAI:
    return FakeCardAI(banana_answers()).install(monkeypatch)


def job_url(job_id: str) -> str:
    return f"{JOBS}/{job_id}"


def recipe_created(bus: list[Dispatched], user: TestUser) -> list[Dispatched]:
    return [e for e in bus if e.event_type == EventTypes.recipe_created and str(e.household_id) == user.household_id]


# ==================================================================================================================
# One card, end to end


def test_a_two_sided_card_from_the_phone_to_a_recipe(
    api_client: TestClient,
    user: TestUser,
    fake_ai: FakeCardAI,
    apprise_sent: list[Notified],  # noqa: F811
    bus_events: list[Dispatched],  # noqa: F811
):
    # Home Assistant's notifier is ticked for "Recipe cards ready" and, upstream, for new recipes; another isn't
    home_assistant = make_notifier(api_client, user, cards_ready=True, recipe_created=True)
    other = make_notifier(api_client, user, cards_ready=False)

    # the phone: a batch, the card's front and back in one multipart request with the Bearer header, then Done
    batch_id = start_batch(api_client, user)
    accepted = upload(api_client, user, photo("front"), photo("back"), batchId=batch_id, position=0)
    assert accepted["batchId"] == batch_id
    assert accepted["rejected"] == []
    [queued] = accepted["jobs"]
    job_id = queued["id"]
    assert (queued["status"], queued["pageCount"]) == ("processing", 2)
    assert queued["reviewPath"].endswith(f"/recipes/cards/{job_id}")

    sealed = seal(api_client, user, batch_id)
    assert sealed["sealedAt"] is not None
    assert sent_to(apprise_sent, home_assistant, READY_EVENT) == []  # the card is still being read

    # the dispatcher reads it
    dispatch_until(lambda: job_status(job_id) != "processing")
    assert job_status(job_id) == "ready"
    assert fake_ai.schemas()[0] == "OpenAIRecipeCardTranscription"
    assert fake_ai.calls[0].images == 2  # both sides, in one read
    assert job_columns(job_id)["task_state"] is None

    # one "ready" notification, to the notifier that opted in, with counts and a link only
    [ready] = sent_to(apprise_sent, home_assistant, READY_EVENT)
    assert sent_to(apprise_sent, other, READY_EVENT) == []
    assert ready.thread.startswith("ai-ingest-")  # sent by the card's own task, once the batch had nothing left to read
    assert ready.title == "Recipe cards ready"
    assert ready.body == "1 card is ready to review (1 needs a look)."
    document = ready.document()
    assert document["batchId"] == batch_id
    assert document["jobIds"] == [job_id]
    assert (document["readyCount"], document["needsAttentionCount"], document["failedCount"]) == (1, 1, 0)
    assert document["reviewUrl"].endswith(f"/recipes/cards/review?batch={batch_id}")
    assert "Banana" not in ready.body and all("Banana" not in url for url in ready.urls)
    assert batch_columns(batch_id)["notified_at"] is not None

    # the review page: the draft, the blank flagged as an error
    response = api_client.get(job_url(job_id), headers=user.token)
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["status"] == "ready"
    assert job["draftVersion"] == 1
    assert len(job["pages"]) == 2
    draft = job["draft"]
    assert draft["name"] == BANANA_RECIPE["name"]
    assert [ingredient["originalText"] for ingredient in draft["ingredients"]] == [
        line["text"] for line in BANANA_RECIPE["ingredients"]
    ]
    assert [step["text"] for step in draft["steps"]][-1] == BLANK_STEP
    errors = [flag for flag in job["flags"] if flag["severity"] == "error"]
    assert [(flag["kind"], flag["field"]) for flag in errors] == [("blank", "steps")]
    assert job["errorCount"] == 1

    # the blank blocks the commit until it's filled (or kept as written)
    refused = api_client.post(job_url(f"{job_id}/commit"), json={"draftVersion": 1}, headers=user.token)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "unresolved_flags"

    # the reviewer types the microwave time into the blank, and attaches the card photo and makes it the cover, which
    # a household whose recipes are public (Mealie's default) leaves off unless asked
    assert job["cardPhotoDefault"] is job["cardCoverDefault"] is not job["householdRecipesPublic"]
    assert draft["attachCardPhoto"] is draft["useCardAsCover"] is None
    draft["steps"][-1]["text"] = FILLED_STEP
    draft["attachCardPhoto"] = draft["useCardAsCover"] = True
    # (the review page says which draft schema it was built for: an older build's `true` would read as unset)
    body = {"draftVersion": 1, "draft": draft, "clientDraftSchema": 3}
    saved = api_client.put(job_url(job_id), json=body, headers=user.token)
    assert saved.status_code == 200, saved.text
    assert saved.json()["draftVersion"] == 2
    assert saved.json()["errorCount"] == 0

    # Commit & next
    committed = api_client.post(job_url(f"{job_id}/commit"), json={"draftVersion": 2}, headers=user.token)
    assert committed.status_code == 201, committed.text
    out = committed.json()
    assert out["nextJobId"] is None
    recipe_id, slug = out["recipeId"], out["slug"]
    assert slug == "banana-mug-cake"

    row = job_columns(job_id)
    assert row["status"] == "committed"
    assert row["recipe_id"] == UUID(recipe_id)
    token = row["commit_asset_token"]

    recipe = api_client.get(api_routes.recipes_slug(slug), headers=user.token).json()
    assert recipe["id"] == recipe_id
    assert recipe["recipeInstructions"][-1]["text"] == FILLED_STEP
    assert len(recipe["recipeIngredient"]) == len(BANANA_RECIPE["ingredients"])

    # the card as the recipe's assets, shown on it
    assert [(asset["name"], asset["fileName"]) for asset in recipe["assets"]] == [
        ("Recipe card", f"recipe-card-{token}-1.jpg"),
        ("Recipe card (back)", f"recipe-card-{token}-2.jpg"),
    ]
    assert recipe["settings"]["showAssets"] is True
    recipe_dir = get_app_dirs().RECIPE_DATA_DIR / recipe_id
    assert sorted(path.name for path in (recipe_dir / "assets").iterdir()) == [
        f"recipe-card-{token}-1.jpg",
        f"recipe-card-{token}-2.jpg",
    ]

    # and its front as the cover, with the cover key set
    assert recipe["image"]
    assert (recipe_dir / "images" / "original.webp").is_file()

    # exactly one recipe_created, which reached the notifiers that want it as upstream's own do
    [created] = recipe_created(bus_events, user)
    assert created.document_data.recipe_slug == slug
    [created_notification] = sent_to(apprise_sent, home_assistant, RECIPE_CREATED_EVENT)
    assert sent_to(apprise_sent, other, RECIPE_CREATED_EVENT) == []
    assert not any(event.event_type.name == READY_EVENT for event in bus_events)  # never through the event bus
    assert created_notification is not ready

    # a double tap gets the same recipe, and nothing is sent again
    again = api_client.post(job_url(f"{job_id}/commit"), json={"draftVersion": 2}, headers=user.token)
    assert again.status_code == 200, again.text
    assert again.json()["recipeId"] == recipe_id
    events.housekeeping(utcnow())
    dispatch_once()

    assert len(recipe_created(bus_events, user)) == 1
    assert len(sent_to(apprise_sent, home_assistant, RECIPE_CREATED_EVENT)) == 1
    assert len(sent_to(apprise_sent, home_assistant, READY_EVENT)) == 1


def test_home_assistant_can_read_the_ready_notifications_data(
    api_client: TestClient,
    user: TestUser,
    fake_ai: FakeCardAI,
    apprise_sent: list[Notified],  # noqa: F811
):
    home_assistant = make_notifier(api_client, user, cards_ready=True)
    batch_id = start_batch(api_client, user)
    [queued] = upload(api_client, user, photo("front"), batchId=batch_id, position=0)["jobs"]
    seal(api_client, user, batch_id)
    dispatch_until(lambda: job_status(queued["id"]) != "processing")
    [ready] = sent_to(apprise_sent, home_assistant, READY_EVENT)

    # the automation reads `trigger.json.event_type`, then `trigger.json.document_data | from_json`
    [received] = ready.received
    assert received["event_type"] == READY_EVENT
    document = ready.received_document()
    assert document == ready.document()
    assert document["reviewUrl"].endswith(f"/recipes/cards/review?batch={batch_id}")


# ==================================================================================================================
# Done while the last card is still uploading


@pytest.mark.parametrize("read_before_done", [False, True], ids=["seal-then-read", "read-then-seal"])
def test_done_while_the_last_card_uploads_gives_one_batch_and_one_notification(
    api_client: TestClient,
    user: TestUser,
    fake_ai: FakeCardAI,
    apprise_sent: list[Notified],  # noqa: F811
    read_before_done: bool,
):
    """
    Done is tapped while the third two-sided card is still on its way. The phone's queue holds the seal until that
    upload has answered (`use-recipe-ingest-uploads.ts`), so the late card joins the batch; the batch notifies once,
    whether its last card is read after the seal (the card's task sends it) or before (the seal sends it).
    """
    home_assistant = make_notifier(api_client, user, cards_ready=True)
    batch_id = start_batch(api_client, user)

    first = upload(api_client, user, photo("1 front"), photo("1 back"), batchId=batch_id, position=0)
    second = upload(api_client, user, photo("2 front"), photo("2 back"), batchId=batch_id, position=1)
    early = [first["jobs"][0]["id"], second["jobs"][0]["id"]]

    # the third card is in flight, and Done is tapped: the queue marks the batch sealing and waits for it
    last_upload = InFlightUpload(api_client, user, photo("3 front"), photo("3 back"), batchId=batch_id, position=2)
    assert len(api_client.get(f"{BATCHES}/{batch_id}", headers=user.token).json()["jobs"]) == 2  # not in yet

    # meanwhile the first two are read; the batch isn't sealed, so nothing is sent
    dispatch_until(all_settled(early))
    assert [job_status(job_id) for job_id in early] == ["ready", "ready"]
    assert batch_columns(batch_id)["sealed_at"] is None
    assert sent_to(apprise_sent, home_assistant, READY_EVENT) == []

    # the last card arrives: it joins the same batch, and only then does the queue send Done
    third = last_upload.finish()
    assert third["batchId"] == batch_id
    last = third["jobs"][0]["id"]
    job_ids = [*early, last]

    if read_before_done:
        dispatch_until(all_settled([last]))
        assert sent_to(apprise_sent, home_assistant, READY_EVENT) == []  # every card is read, Done not sent yet
        seal(api_client, user, batch_id)
        [ready] = sent_to(apprise_sent, home_assistant, READY_EVENT)
        assert not ready.thread.startswith("ai-ingest-")  # the seal sent it
    else:
        seal(api_client, user, batch_id)
        assert sent_to(apprise_sent, home_assistant, READY_EVENT) == []  # the last card is still being read
        dispatch_until(all_settled([last]))
        [ready] = sent_to(apprise_sent, home_assistant, READY_EVENT)
        assert ready.thread.startswith("ai-ingest-")  # the last card's task sent it

    # one batch with the three cards in capture order
    batch = api_client.get(f"{BATCHES}/{batch_id}", headers=user.token).json()
    assert [(job["id"], job["position"], job["status"]) for job in batch["jobs"]] == [
        (job_id, position, "ready") for position, job_id in enumerate(job_ids)
    ]
    assert batch["sealedAt"] is not None and batch["notifiedAt"] is not None

    assert ready.body == "3 cards are ready to review (3 need a look)."
    document = ready.document()
    assert document["batchId"] == batch_id
    assert document["jobIds"] == job_ids

    # nothing sends it again: another seal, housekeeping, another dispatcher pass
    seal(api_client, user, batch_id)
    events.housekeeping(utcnow())
    dispatch_once()
    assert len(sent_to(apprise_sent, home_assistant, READY_EVENT)) == 1
