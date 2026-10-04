"""
The `recipe_card_queue` voice tool (docs/ai/PHASE2.md §12): counts only, for the caller's household, with speech a
voice assistant can read. Card names are card text and never go out, local-only cards or not.
"""

import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import IngestSource, IngestStatus, RecipeIngestionJobCounts
from mealie.services.ai.tools import get_tool
from mealie.services.ai.tools.ingest import RecipeCardQueueResult, queue_speech
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser

SECRET_TITLE = "Aunt Edna's Private Pickles"


def counts(ready: int = 0, needs_attention: int = 0, processing: int = 0, failed: int = 0) -> RecipeIngestionJobCounts:
    return RecipeIngestionJobCounts(ready=ready, needs_attention=needs_attention, processing=processing, failed=failed)


@pytest.mark.parametrize(
    ("queue", "speech"),
    [
        (counts(), "No recipe cards are waiting to be reviewed."),
        (counts(ready=1), "1 recipe card is ready to review."),
        (counts(ready=1, needs_attention=1), "1 recipe card is ready to review. It needs a closer look."),
        (counts(ready=7, needs_attention=2), "7 recipe cards are ready to review. 2 need a closer look."),
        (counts(ready=3, needs_attention=3), "3 recipe cards are ready to review. All of them need a closer look."),
        (counts(ready=4, needs_attention=1), "4 recipe cards are ready to review. 1 needs a closer look."),
        (counts(processing=1), "No recipe cards are ready to review yet. 1 is still being read."),
        (
            counts(processing=3, failed=1),
            "No recipe cards are ready to review yet. 3 are still being read and 1 couldn't be read.",
        ),
        (
            counts(ready=5, needs_attention=2, processing=2, failed=1),
            "5 recipe cards are ready to review. 2 need a closer look, 2 are still being read and 1 couldn't be read.",
        ),
    ],
)
def test_speech(queue: RecipeIngestionJobCounts, speech: str):
    assert queue_speech(queue) == speech
    # what the result keeps of it: at most two sentences, plain, within the limit
    assert (
        RecipeCardQueueResult(speech=queue_speech(queue), ready=0, needs_attention=0, processing=0, failed=0).speech
        == speech
    )


def test_the_tool_is_a_read_tool_without_arguments():
    tool = get_tool("recipe_card_queue")
    assert tool is not None
    assert tool.writes is False
    assert tool.input_schema == {"type": "object", "properties": {}, "additionalProperties": False}
    assert set(RecipeCardQueueResult.model_fields) == {"speech", "ready", "needs_attention", "processing", "failed"}


def seed(user: TestUser, *statuses: str, local_only: bool = False) -> None:
    """Jobs titled with card text; `ready!` is a ready card with something to check"""
    with session_context() as session:
        repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
        batch_id = repos.batches.create(source=IngestSource.app, created_by=user.user_id)
        for position, status in enumerate(statuses):
            repos.jobs.create(
                {
                    "batch_id": batch_id,
                    "position": position,
                    "source": IngestSource.app.value,
                    "status": IngestStatus(status.rstrip("!")).value,
                    "title": SECRET_TITLE,
                    "local_only": local_only,
                    "source_sha256": uuid4().hex * 2,
                    "error_count": 1 if status.endswith("!") else 0,
                }
            )


def call(api_client: TestClient, user: TestUser) -> dict:
    response = api_client.post(api_routes.ai_tools_name("recipe_card_queue"), json={}, headers=user.token)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_counts_only_for_the_callers_household(
    api_client: TestClient, unique_user_fn_scoped: TestUser, h2_user: TestUser
):
    user = unique_user_fn_scoped
    seed(user, "ready", "ready!", "processing", "failed", "committed")
    seed(user, "ready", local_only=True)

    result = call(api_client, user)
    assert result == {
        "speech": "3 recipe cards are ready to review. 1 needs a closer look, 1 is still being read and 1 couldn't "
        "be read.",
        "ready": 3,
        "needs_attention": 1,
        "processing": 1,
        "failed": 1,
    }
    # the counts match the cards page's
    assert api_client.get("/api/ai/ingest/jobs/counts", headers=user.token).json() == {
        "processing": 1,
        "ready": 3,
        "needsAttention": 1,
        "failed": 1,
    }
    # no card text at all, from a local-only card or any other
    assert SECRET_TITLE not in json.dumps(result)
    assert "Pickles" not in json.dumps(result)

    # another household (in another group here) sees only its own
    other = call(api_client, h2_user)
    assert other["ready"] == 0
    assert SECRET_TITLE not in json.dumps(other)


def test_an_empty_queue(api_client: TestClient, unique_user_fn_scoped: TestUser):
    assert call(api_client, unique_user_fn_scoped) == {
        "speech": "No recipe cards are waiting to be reviewed.",
        "ready": 0,
        "needs_attention": 0,
        "processing": 0,
        "failed": 0,
    }
