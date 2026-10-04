"""
`POST /jobs/{id}/rebuild` and `/parse-lines` (docs/ai/PHASE2.md §3.1, §14): building the recipe again from the
reviewer's corrected transcription, and parsing chosen ingredient lines with the AI parser. Each queues the card's
extract task in its mode, under the same rules as a re-extract and a re-read: a card being reviewed, with no task.
What the tasks do is tested with the runner (`runner/test_task_modes.py`). Runs on SQLite and PostgreSQL.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import JOBS, assert_code, banana_draft, job_row, job_url, seed_job, set_columns, use_fake_flags

from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardFlag,
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    FlagResolution,
    IngestStatus,
    IngestTaskKind,
)
from mealie.schema.recipe_ingest.ingest_requests import MAX_PARSE_LINES, MAX_TRANSCRIPTION
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.runner.dispatcher import dispatcher
from tests.utils.fixture_schemas import TestUser

TRANSCRIPTION = "Banana Mug Cake\n1 T. coconut oil (melted)\n1/4 t. salt\nMicrowave for 2 minutes."


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


@pytest.fixture
def wakes(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Each time the dispatcher is woken for a queued task"""
    woken: list[bool] = []
    monkeypatch.setattr(dispatcher, "wake", lambda: woken.append(True))
    return woken


def _refs(job_id: UUID) -> list[str]:
    return [line["reference_id"] for line in job_row(job_id)["draft"]["ingredients"]]


def _rebuild(api_client: TestClient, user: TestUser, job_id: UUID, transcription: Any = TRANSCRIPTION) -> Any:
    return api_client.post(job_url(job_id, "rebuild"), json={"transcription": transcription}, headers=user.token)


def _parse(api_client: TestClient, user: TestUser, job_id: UUID, refs: list[str]) -> Any:
    return api_client.post(job_url(job_id, "parse-lines"), json={"refs": refs}, headers=user.token)


def test_rebuild_queues_the_extract_task_with_the_corrected_text(
    api_client: TestClient, unique_user_fn_scoped: TestUser, wakes: list[bool]
):
    user = unique_user_fn_scoped
    job_id = seed_job(user, attempts=2)

    response = _rebuild(api_client, user, job_id)
    assert response.status_code == 202, response.text
    state = response.json()
    assert (state["status"], state["task"]["kind"], state["task"]["state"]) == ("ready", "extract", "queued")
    assert state["draftVersion"] == 1  # the draft is the reviewer's until the result lands

    row = job_row(job_id)
    assert (row["task_kind"], row["task_state"], row["task_priority"]) == ("extract", "queued", limits.PRIORITY_EXTRACT)
    assert row["task_payload"] == {"mode": "rebuild", "transcription": TRANSCRIPTION}
    assert row["attempts"] == 0
    assert row["transcription"].startswith("Banana Mug Cake")  # the stored reading changes only with the result
    assert wakes == [True]


def test_parse_lines_queues_the_lines_as_they_read_now(
    api_client: TestClient, unique_user_fn_scoped: TestUser, wakes: list[bool]
):
    user = unique_user_fn_scoped
    edited = CardDraftIngredient(note="2 EL Zucker", display="2 EL Zucker")
    job_id = seed_job(user, draft=banana_draft(ingredients=[*banana_draft().ingredients, edited]))
    refs = _refs(job_id)

    response = _parse(api_client, user, job_id, [refs[2], refs[0], refs[2]])
    assert response.status_code == 202, response.text
    assert (response.json()["task"]["kind"], response.json()["task"]["state"]) == ("extract", "queued")

    row = job_row(job_id)
    assert (row["task_kind"], row["task_priority"]) == ("extract", limits.PRIORITY_REREAD)  # a reviewer is waiting
    assert row["task_payload"] == {
        "mode": "parse_lines",
        "lines": [
            {"ref": refs[2], "text": "2 EL Zucker"},
            {"ref": refs[0], "text": "1 tablespoon coconut oil melted"},  # as it reads now
        ],
    }
    assert wakes == [True]


def test_a_page_loaded_while_the_task_runs_learns_what_it_does(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """
    The task's mode and the lines it parses come with the job, its state and the list, so a page loaded meanwhile (a
    reload, another device) says "Rebuilding" or "Parsing" rather than reading the whole card; never the payload's text
    """
    user = unique_user_fn_scoped

    def described(job_id: UUID) -> list[dict[str, Any]]:
        job = api_client.get(job_url(job_id), headers=user.token).json()
        state = api_client.get(job_url(job_id, "state"), headers=user.token).json()
        listed = api_client.get(JOBS, headers=user.token).json()["items"]
        [summary] = [item for item in listed if item["id"] == str(job_id)]
        return [job["task"], state["task"], summary["task"]]

    rebuilt = seed_job(user)
    assert _rebuild(api_client, user, rebuilt).status_code == 202
    for task in described(rebuilt):
        assert (task["kind"], task["mode"], task["refs"]) == ("extract", "rebuild", [])
        assert "transcription" not in task

    parsed = seed_job(user)
    refs = _refs(parsed)
    assert _parse(api_client, user, parsed, [refs[1], refs[0]]).status_code == 202
    for task in described(parsed):
        assert (task["kind"], task["mode"], task["refs"]) == ("extract", "parse_lines", [refs[1], refs[0]])

    reread = seed_job(user)
    assert api_client.post(job_url(reread, "reextract"), headers=user.token).status_code == 202
    for task in described(reread):
        assert (task["kind"], task["mode"], task["refs"]) == ("extract", "reextract", [])

    region = seed_job(user)
    body = {"page": 0, "x": 0, "y": 0, "width": 1, "height": 0.5, "target": {"field": "name"}}
    assert api_client.post(job_url(region, "reread"), json=body, headers=user.token).status_code == 202
    for task in described(region):
        assert (task["kind"], task["mode"], task["refs"]) == ("reread", None, [])

    # a payload this version doesn't know names no mode, rather than failing the page
    set_columns(reread, task_payload={"mode": "something-newer"})
    for task in described(reread):
        assert (task["kind"], task["mode"], task["refs"]) == ("extract", None, [])


def _marker_flag(kind: CardFlagKind, line: CardDraftIngredient, resolution: FlagResolution | None) -> CardFlag:
    ref = str(line.reference_id)
    return CardFlag(
        id=f"{kind.value}:ingredients:{ref}",
        kind=kind,
        severity=CardFlagSeverity.error,
        source=CardFlagSource.marker,
        field="ingredients",
        ref=ref,
        resolution=resolution,
    )


def test_parse_lines_sends_a_line_kept_with_a_marker_around_it(api_client: TestClient, unique_user_fn_scoped: TestUser):
    # as a save parses it (`review.KeptLine`): the parser reads the line without its marker, which goes back in its note
    user = unique_user_fn_scoped
    kept = CardDraftIngredient(original_text="[blank] C. sugar", note="[blank] C. sugar")
    open_marker = CardDraftIngredient(original_text="1 C. [illegible]", note="1 C. [illegible]")
    draft = banana_draft(ingredients=[kept, open_marker])
    flags = [
        _marker_flag(CardFlagKind.blank, kept, FlagResolution.kept),
        _marker_flag(CardFlagKind.illegible, open_marker, None),  # not kept: sent as it reads
    ]
    job_id = seed_job(user, draft=draft, flags=flags)

    response = _parse(api_client, user, job_id, [str(kept.reference_id), str(open_marker.reference_id)])
    assert response.status_code == 202, response.text
    assert job_row(job_id)["task_payload"]["lines"] == [
        {
            "ref": str(kept.reference_id),
            "text": "[blank] C. sugar",  # what the line must still read when the result lands
            "kept": {
                "text": "[blank] C. sugar",
                "parse_text": "1 C. sugar",
                "markers": ["[blank]"],
                "amount_marker": True,
            },
        },
        {"ref": str(open_marker.reference_id), "text": "1 C. [illegible]"},
    ]


@pytest.mark.parametrize("action", ["rebuild", "parse-lines"])
def test_only_a_card_being_reviewed_with_no_task(api_client: TestClient, unique_user_fn_scoped: TestUser, action: str):
    user = unique_user_fn_scoped

    def send(job_id: UUID) -> Any:
        if action == "rebuild":
            return _rebuild(api_client, user, job_id)
        return _parse(api_client, user, job_id, _refs(job_id)[:1])

    busy = seed_job(user, task_kind=IngestTaskKind.reread.value, task_state="queued", task_priority=1)
    assert_code(send(busy), 409, "busy")
    assert job_row(busy)["task_kind"] == "reread"  # the pending task is left as it is

    for status in (IngestStatus.failed, IngestStatus.committed, IngestStatus.committing):
        job_id = seed_job(user, status=status, draft=banana_draft())
        detail = assert_code(send(job_id), 409, "invalid_status")
        assert detail["status"] == status.value
        assert job_row(job_id)["task_state"] is None

    # one at a time: the first queues, the second is busy
    job_id = seed_job(user)
    assert send(job_id).status_code == 202
    assert_code(send(job_id), 409, "busy")


@pytest.mark.parametrize("action", ["rebuild", "parse-lines"])
def test_another_households_card_is_not_found(
    api_client: TestClient, unique_user: TestUser, h2_user: TestUser, action: str
):
    job_id = seed_job(h2_user)
    if action == "rebuild":
        response = _rebuild(api_client, unique_user, job_id)
    else:
        response = _parse(api_client, unique_user, job_id, _refs(job_id)[:1])
    assert_code(response, 404, "not_found")
    assert job_row(job_id)["task_state"] is None


def test_a_card_kept_local_is_rebuilt_under_its_policy(api_client: TestClient, unique_user_fn_scoped: TestUser):
    # the worker applies the card's own local-only, and its group's, to every task it runs
    user = unique_user_fn_scoped
    job_id = seed_job(user, local_only=True)
    assert _rebuild(api_client, user, job_id).status_code == 202
    row = job_row(job_id)
    assert (row["local_only"], row["task_state"]) == (True, "queued")


def test_parse_lines_names_lines_the_draft_has(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    refs = _refs(job_id)

    assert_code(_parse(api_client, user, job_id, [refs[0], str(uuid4())]), 422, "unknown_target")
    assert _parse(api_client, user, job_id, []).status_code == 422
    too_many = [str(uuid4()) for _ in range(MAX_PARSE_LINES + 1)]
    assert _parse(api_client, user, job_id, too_many).status_code == 422
    assert _parse(api_client, user, job_id, ["not-an-id"]).status_code == 422
    assert job_row(job_id)["task_state"] is None

    no_lines = seed_job(user, draft=CardDraft(name="Toast"))
    assert_code(_parse(api_client, user, no_lines, [str(uuid4())]), 422, "unknown_target")


def test_rebuild_takes_some_text_within_the_limit(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    for invalid in ("", "   \n\t ", "x" * (MAX_TRANSCRIPTION + 1), None):
        assert _rebuild(api_client, user, job_id, invalid).status_code == 422
    extra = api_client.post(
        job_url(job_id, "rebuild"), json={"transcription": TRANSCRIPTION, "mode": "translate"}, headers=user.token
    )
    assert extra.status_code == 422
    assert job_row(job_id)["task_state"] is None


def test_the_routes_answer_503_while_ingestion_is_off(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    from mealie.services.ai.ingest.settings import get_ingest_settings

    user = unique_user_fn_scoped
    job_id = seed_job(user)
    monkeypatch.setattr(get_ingest_settings(), "ENABLED", False)
    assert _rebuild(api_client, user, job_id).status_code == 503
    assert _parse(api_client, user, job_id, _refs(job_id)[:1]).status_code == 503
