"""
Draft saves with the real flag rules (docs/ai/PHASE2.md §4.6, §6.6, §19): a save checks the draft against the card's
transcription again and keeps reading flags only where an extraction raised them, so the warnings about a number the
reader invented survive the reviewer's other edits, and a number the reviewer types into a blank raises nothing. The
other review tests use a stand-in for the rules; these don't. Runs on SQLite and PostgreSQL.
"""

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from test_jobs_api import banana_draft, job_row, job_url, seed_job, set_columns

from mealie.db.db_setup import session_context
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftStep,
    ExtractionMeta,
    ExtractionUnsure,
    IngestTaskKind,
    IngestTaskState,
)
from mealie.services.ai.ingest.pipeline.flags import compute_flags
from mealie.services.ai.ingest.runner import finalize
from mealie.services.ai.ingest.runner.types import ExtractResult
from tests.utils.fixture_schemas import TestUser

MASH = "Mash the banana in a mug and stir in everything else."
CARD = (
    f"Banana Mug Cake\nPrep 5 minutes\n1 T. coconut oil (melted)\n1/4 t. salt\n{MASH}\nMicrowave for [blank] minutes."
)
"""The banana card as it reads: the microwave time is a blank on the card itself"""


def _draft(microwave: str) -> CardDraft:
    return banana_draft(steps=[CardDraftStep(text=MASH), CardDraftStep(text=microwave)])


def _extracted(user: TestUser, draft: CardDraft, extraction: ExtractionMeta, **columns: Any) -> UUID:
    """A ready job as an extraction of the banana card leaves it, with the real rules' flags"""
    flags = compute_flags(draft, extraction, {}, transcription=CARD)
    return seed_job(user, draft=draft, flags=flags, transcription=CARD, extraction=extraction, **columns)


def _image_read(**fields: Any) -> ExtractionMeta:
    return ExtractionMeta(read_path="image", provider="Claude", model="claude-sonnet", **fields)


def _reading_flags(flags: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """(kind, field, source) of the flags that compare the draft with what was read"""
    reading = {"unsure", "not_on_card", "marker_dropped", "read_disagreement"}
    return sorted(
        (flag["kind"], flag["field"], flag["source"])
        for flag in flags
        if flag["kind"] in reading or (flag["kind"] == "blank" and flag["source"] == "cross_read")
    )


def _put(api_client: TestClient, user: TestUser, job_id: UUID, draft: dict[str, Any], **body: Any) -> dict[str, Any]:
    version = job_row(job_id)["draft_version"]
    response = api_client.put(
        job_url(job_id), json={"draftVersion": version, "draft": draft, **body}, headers=user.token
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_save_keeps_the_warnings_about_an_invented_number(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """The reader filled the card's blank with "2": the reviewer edits something else, and the warnings stay"""
    user = unique_user_fn_scoped
    job_id = _extracted(user, _draft("Microwave for 2 minutes."), _image_read())
    extracted = api_client.get(job_url(job_id), headers=user.token).json()
    expected = [("marker_dropped", "card", "validator"), ("not_on_card", "steps", "validator")]
    assert _reading_flags(extracted["flags"]) == expected
    assert extracted["warningCount"] == 2

    draft = {**extracted["draft"], "name": "Banana Mug Cake for One"}
    saved = _put(api_client, user, job_id, draft)
    assert _reading_flags(saved["flags"]) == expected
    assert (saved["errorCount"], saved["warningCount"]) == (0, 2)
    assert job_row(job_id)["warning_count"] == 2
    assert api_client.get(f"{job_url('counts')}", headers=user.token).json()["needsAttention"] == 1

    # "Looks right" on one of them, then another save: it stays dismissed, the other stays open
    not_on_card = next(flag["id"] for flag in saved["flags"] if flag["kind"] == "not_on_card")
    saved = _put(api_client, user, job_id, draft, flagResolutions={not_on_card: "dismissed"})
    saved = _put(api_client, user, job_id, {**draft, "description": "A quick cake for one"})
    by_kind = {flag["kind"]: flag["resolution"] for flag in saved["flags"]}
    assert (by_kind["not_on_card"], by_kind["marker_dropped"]) == ("dismissed", None)
    assert saved["warningCount"] == 1

    # fixing the line ends its warning
    draft["steps"][1]["text"] = "Microwave for [blank] minutes."
    saved = _put(api_client, user, job_id, draft)
    assert ("not_on_card", "steps", "validator") not in _reading_flags(saved["flags"])


def test_typing_into_a_blank_raises_no_reading_flag(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """§19: with the cross-read on, the reviewer types "2" into the banana card's blank and commits"""
    user = unique_user_fn_scoped
    extraction = _image_read(cross_read_lines=CARD.splitlines())
    job_id = _extracted(user, _draft("Microwave for [blank] minutes."), extraction)
    extracted = api_client.get(job_url(job_id), headers=user.token).json()
    assert [(flag["kind"], flag["source"]) for flag in extracted["flags"] if flag["severity"] == "error"] == [
        ("blank", "marker")
    ]

    draft = extracted["draft"]
    draft["steps"][1]["text"] = "Microwave for 2 minutes."
    saved = _put(api_client, user, job_id, draft)
    assert _reading_flags(saved["flags"]) == []  # no cross-read blank, no not_on_card for the typed "2"
    assert saved["errorCount"] == 0

    response = api_client.post(
        job_url(job_id, "commit"), json={"draftVersion": saved["draftVersion"]}, headers=user.token
    )
    assert response.status_code == 201, response.text


def test_a_commit_carrying_the_draft_uses_the_same_rules(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    extraction = _image_read(cross_read_lines=CARD.splitlines())
    job_id = _extracted(user, _draft("Microwave for [blank] minutes."), extraction)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    draft["steps"][1]["text"] = "Microwave for 2 minutes."

    response = api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1, "draft": draft}, headers=user.token)
    assert response.status_code == 201, response.text
    row = job_row(job_id)
    assert (row["status"], row["error_count"]) == ("committed", 0)


# ==================================================================================================================
# A whole-card re-extract of an edited draft


NEW_READING = ExtractionMeta(
    read_path="image",
    provider="Claude",
    model="claude-sonnet",
    unsure=[ExtractionUnsure(text="coconut oil", alternatives=["coconut milk"], reason="faded")],
    cross_read_lines=CARD.splitlines(),
)


def _reextracted(user: TestUser) -> UUID:
    """
    An edited job whose re-extract finished: the first reading had the card upright but wrong ("1 minute"), the new
    one reads the blank, but its draft invents "2" in it, and becomes a whole-card proposal
    """
    first = "\n".join([*CARD.splitlines()[:-1], "Microwave for 1 minute."])
    old = _draft("Microwave for 1 minute.")
    old_extraction = _image_read()
    old_flags = compute_flags(old, old_extraction, {}, transcription=first)
    token = uuid4()
    job_id = seed_job(
        user,
        draft=old.model_copy(update={"name": "Banana Mug Cake for One"}),  # the reviewer's edit
        flags=old_flags,
        transcription=first,
        extraction=old_extraction,
        draft_version=2,
        extracted_version=1,
    )
    set_columns(
        job_id,
        task_kind=IngestTaskKind.extract.value,
        task_state=IngestTaskState.running.value,
        lease_token=token,
    )

    new = _draft("Microwave for 2 minutes.")
    result = ExtractResult(
        draft=new,
        flags=compute_flags(new, NEW_READING, {}, transcription=CARD),
        transcription=CARD,
        extraction=NEW_READING,
        pages=job_row(job_id)["pages"],
    )
    with session_context() as session:
        assert finalize.finalize_extract(session, job_id, token, result).applied == finalize.Applied.proposal
    return job_id


def test_accepting_a_whole_card_proposal_raises_the_new_readings_flags(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    job_id = _reextracted(user)
    job = api_client.get(job_url(job_id), headers=user.token).json()
    assert _reading_flags(job["flags"]) == []  # the kept draft's
    [proposal] = job["proposals"]
    assert proposal["kind"] == "full"

    saved = _put(api_client, user, job_id, proposal["draft"], resolvedProposalIds=[proposal["id"]])
    assert _reading_flags(saved["flags"]) == [
        ("blank", "steps", "cross_read"),
        ("marker_dropped", "card", "validator"),
        ("not_on_card", "steps", "validator"),
        ("unsure", "ingredients", "model"),
    ]
    assert saved["errorCount"] == 1  # the cross-read's blank blocks commit until fixed or kept
    assert job_row(job_id)["proposals"] == []

    # they're the job's flags now: an unrelated save keeps them
    saved = _put(api_client, user, job_id, {**proposal["draft"], "name": "Banana Mug Cake for One"})
    assert len(_reading_flags(saved["flags"])) == 4
    assert saved["errorCount"] == 1


def test_dismissing_a_whole_card_proposal_keeps_the_drafts_own_flags(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    job_id = _reextracted(user)
    job = api_client.get(job_url(job_id), headers=user.token).json()
    [proposal] = job["proposals"]

    saved = _put(api_client, user, job_id, job["draft"], resolvedProposalIds=[proposal["id"]])
    assert _reading_flags(saved["flags"]) == []
    assert saved["errorCount"] == 0
    assert job_row(job_id)["proposals"] == []
