"""
The `recipe_card_queue` voice tool (docs/ai/PHASE2.md §12): counts only, for the caller's household, with speech a
voice assistant can read. Card names are card text and never go out, local-only cards or not.
"""

import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.lang.providers import get_locale_provider
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
    english = get_locale_provider("en-US")
    assert queue_speech(queue, english) == speech
    # what the result keeps of it: at most two sentences, plain, within the limit
    assert (
        RecipeCardQueueResult(
            speech=queue_speech(queue, english), ready=0, needs_attention=0, processing=0, failed=0
        ).speech
        == speech
    )
    # a language without the texts yet speaks English, never the texts' keys
    assert queue_speech(queue, get_locale_provider("de-DE")) == speech


class RecordingTranslator:
    """Answers every text with its key's last part and its values, and records what it was asked"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def t(self, key: str, default=None, **kwargs) -> str:
        self.calls.append((key, kwargs))
        if key.endswith(".list-separator"):
            return " / "
        values = ",".join(f"{name}={value}" for name, value in kwargs.items())
        return f"{key.rsplit('.', 1)[-1]}[{values}]"


def test_speech_goes_through_the_translator():
    translator = RecordingTranslator()
    speech = queue_speech(counts(ready=5, needs_attention=2, processing=3, failed=1), translator)

    voice = "recipe-ingest.voice"
    assert translator.calls[:2] == [(f"{voice}.ready", {"count": 5}), (f"{voice}.need-a-look", {"count": 2})]
    assert (f"{voice}.still-reading", {"count": 3}) in translator.calls
    assert (f"{voice}.failed", {"count": 1}) in translator.calls
    # the list and the sentence are the language's too
    assert speech == (
        "ready[count=5] Details[details=list-and[first=need-a-look[count=2] / still-reading[count=3],"
        "last=failed[count=1]]]"
    )

    translator = RecordingTranslator()
    queue_speech(counts(ready=2, needs_attention=2), translator)
    assert (f"{voice}.all-need-a-look", {"count": 2}) in translator.calls

    translator = RecordingTranslator()
    assert queue_speech(counts(), translator) == "none-waiting[]"


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


def test_the_speech_is_in_the_callers_language(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """The request's language (`Accept-Language`, as MCP clients and Home Assistant send it) reaches the speech"""
    user = unique_user_fn_scoped
    seed(user, "ready", "ready!", "processing")
    german = get_locale_provider("de-DE")  # the provider every de-DE request gets
    monkeypatch.setitem(
        german.translations,  # type: ignore[attr-defined]
        "recipe-ingest",
        {
            "voice": {
                "ready": "Keine Rezeptkarten | {count} Rezeptkarte ist bereit. | {count} Rezeptkarten sind bereit.",
                "need-a-look": "{count} muss geprüft werden | {count} müssen geprüft werden",
                "still-reading": "{count} wird noch gelesen | {count} werden noch gelesen",
                "list-separator": ", ",
                "list-and": "{first} und {last}",
                "details": "{details}.",
            }
        },
    )

    response = api_client.post(
        api_routes.ai_tools_name("recipe_card_queue"), json={}, headers=user.token | {"Accept-Language": "de-DE"}
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["speech"] == "2 Rezeptkarten sind bereit. 1 muss geprüft werden und 1 wird noch gelesen."
    assert result["ready"] == 2 and result["needs_attention"] == 1 and result["processing"] == 1


def test_an_empty_queue(api_client: TestClient, unique_user_fn_scoped: TestUser):
    assert call(api_client, unique_user_fn_scoped) == {
        "speech": "No recipe cards are waiting to be reviewed.",
        "ready": 0,
        "needs_attention": 0,
        "processing": 0,
        "failed": 0,
    }
