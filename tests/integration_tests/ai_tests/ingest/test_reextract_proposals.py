"""
Two whole-card re-extracts of an edited draft (docs/ai/PHASE2.md §3.3, §6.6): the job keeps one reading, the newest,
so the newer whole-card proposal replaces the older one. Accepting the older one (still on another screen) is a 409
and the page reloads; accepting the newer one raises the flags of its own reading. With the real flag rules. Runs on
SQLite and PostgreSQL.
"""

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from test_jobs_api import assert_code, banana_draft, job_row, job_url, seed_job, set_columns

from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.schema.recipe.recipe_ingredient import SaveIngredientUnit
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftNote,
    CardDraftRef,
    CardDraftStep,
    CardFlagKind,
    CardFlagSource,
    ExtractionMeta,
    ExtractionUnsure,
    IngestTaskKind,
    IngestTaskState,
)
from mealie.services.ai.ingest.pipeline.flags import compute_flags, ingredient_hash
from mealie.services.ai.ingest.runner import finalize
from mealie.services.ai.ingest.runner.types import ExtractResult
from tests.utils.fixture_schemas import TestUser

MASH = "Mash the banana in a mug and stir in everything else."
LINES = ["Banana Mug Cake", "Prep 5 minutes", "1 T. coconut oil (melted)", "1/4 t. salt", MASH]
SIDEWAYS_READ = "\n".join([*LINES, "Microwave for 2 minutes."])
"""The first re-extract's reading: the card's time read as "2" """
UPRIGHT_READ = "\n".join([*LINES, "Microwave for [blank] minutes."])
"""The second re-extract's reading: the card's time is a blank"""


def _draft(name: str, microwave: str) -> CardDraft:
    return banana_draft(name=name, steps=[CardDraftStep(text=MASH), CardDraftStep(text=microwave)])


def _reading() -> ExtractionMeta:
    return ExtractionMeta(read_path="image", provider="Claude", model="claude-sonnet")


def _reading_flags(flags: list[dict[str, Any]]) -> list[tuple[str, str]]:
    reading = {"unsure", "not_on_card", "marker_dropped", "read_disagreement"}
    return sorted((flag["kind"], flag["field"]) for flag in flags if flag["kind"] in reading)


def _reextract(
    job_id: UUID,
    draft: CardDraft,
    transcription: str,
    ocr: str | None = None,
    extraction: ExtractionMeta | None = None,
) -> None:
    """A re-extract of the job, finished: its result finalized as the runner does (`ocr`: Tesseract's printed text)"""
    token = uuid4()
    set_columns(
        job_id, task_kind=IngestTaskKind.extract.value, task_state=IngestTaskState.running.value, lease_token=token
    )
    extraction = extraction or _reading()
    pages = job_row(job_id)["pages"]
    if ocr is not None:
        pages = [{**page, "ocr": {"text": ocr, "confidence": 95.0}} for page in pages]
    result = ExtractResult(
        draft=draft,
        flags=compute_flags(draft, extraction, {}, transcription=transcription),
        transcription=transcription,
        extraction=extraction,
        pages=pages,
    )
    with session_context() as session:
        assert finalize.finalize_extract(session, job_id, token, result).applied == finalize.Applied.proposal


def _edited_job(user: TestUser) -> UUID:
    """A ready job the reviewer already edited (so a re-extract proposes rather than replaces)"""
    draft = _draft("Banana Mug Cake for One", "Microwave for 1 minute.")
    first = "\n".join([*LINES, "Microwave for 1 minute."])
    flags = compute_flags(draft, _reading(), {}, transcription=first)
    return seed_job(
        user, draft=draft, flags=flags, transcription=first, extraction=_reading(), draft_version=2, extracted_version=1
    )


def test_the_newer_reading_replaces_the_older_proposal(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = _edited_job(user)

    _reextract(job_id, _draft("Banana Mug Cake (first)", "Microwave for 2 minutes."), SIDEWAYS_READ)
    [older] = api_client.get(job_url(job_id), headers=user.token).json()["proposals"]
    assert older["draft"]["name"] == "Banana Mug Cake (first)"

    _reextract(job_id, _draft("Banana Mug Cake (second)", "Microwave for 2 minutes."), UPRIGHT_READ)
    job = api_client.get(job_url(job_id), headers=user.token).json()
    [newer] = job["proposals"]
    assert newer["kind"] == "full" and newer["id"] != older["id"]
    assert newer["draft"]["name"] == "Banana Mug Cake (second)"
    assert job_row(job_id)["transcription"] == UPRIGHT_READ
    version = job["draftVersion"]

    # another screen still shows the older proposal and accepts it: refused, and that screen reloads
    stale = api_client.put(
        job_url(job_id),
        json={"draftVersion": version, "draft": older["draft"], "resolvedProposalIds": [older["id"]]},
        headers=user.token,
    )
    assert assert_code(stale, 409, "version_conflict")["current"] == version
    assert job_row(job_id)["draft"]["name"] == "Banana Mug Cake for One"

    # accepting the newer one raises its own reading's flags: the "2" isn't on the card it read
    saved = api_client.put(
        job_url(job_id),
        json={"draftVersion": version, "draft": newer["draft"], "resolvedProposalIds": [newer["id"]]},
        headers=user.token,
    )
    assert saved.status_code == 200, saved.text
    assert _reading_flags(saved.json()["flags"]) == [("marker_dropped", "card"), ("not_on_card", "steps")]
    assert job_row(job_id)["proposals"] == []


def test_accepting_the_only_reading_that_had_the_number_raises_no_flag(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """The newest reading is the one with the "2" on the card: accepting it flags nothing about the "2" """
    user = unique_user_fn_scoped
    job_id = _edited_job(user)
    _reextract(job_id, _draft("Banana Mug Cake (first)", "Microwave for [blank] minutes."), UPRIGHT_READ)
    _reextract(job_id, _draft("Banana Mug Cake (second)", "Microwave for 2 minutes."), SIDEWAYS_READ)

    job = api_client.get(job_url(job_id), headers=user.token).json()
    [newer] = job["proposals"]
    saved = api_client.put(
        job_url(job_id),
        json={"draftVersion": job["draftVersion"], "draft": newer["draft"], "resolvedProposalIds": [newer["id"]]},
        headers=user.token,
    )
    assert saved.status_code == 200, saved.text
    assert _reading_flags(saved.json()["flags"]) == []


def test_accepting_a_reading_raises_its_unit_and_ocr_flags(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """
    The accepted reading's flags are the ones its re-extract raised: a short word that is one of the group's own units
    ("stk" for its "stick") is a lost unit, and on a printed card Tesseract's different number is a disagreement
    """
    user = unique_user_fn_scoped
    with session_context() as session:
        repos = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        repos.ingredient_units.create(SaveIngredientUnit(name="stick", abbreviation="stk", group_id=user.group_id))
    job_id = _edited_job(user)

    bake = "Bake at 375° for 1 hour, then cool 10 minutes."
    butter = CardDraftIngredient(
        original_text="2 stk butter",
        quantity=2,
        food=CardDraftRef(name="stk butter"),
        display="2 stk butter",
        parse_confidence=0.9,
    )
    butter.extracted_hash = ingredient_hash(butter)
    proposed = banana_draft(name="Banana Cake", steps=[CardDraftStep(text=MASH), CardDraftStep(text=bake)])
    proposed.ingredients.append(butter)
    printed = "\n".join(["Banana Cake", "Prep 5 minutes", "1 T. coconut oil (melted)", "1/4 t. salt", "2 stk butter"])
    printed += f"\n{MASH}\n{bake}"
    _reextract(job_id, proposed, printed, ocr=printed.replace("375", "350"))

    job = api_client.get(job_url(job_id), headers=user.token).json()
    [proposal] = job["proposals"]
    saved = api_client.put(
        job_url(job_id),
        json={"draftVersion": job["draftVersion"], "draft": proposal["draft"], "resolvedProposalIds": [proposal["id"]]},
        headers=user.token,
    )
    assert saved.status_code == 200, saved.text
    flags = saved.json()["flags"]
    [lost_unit] = [flag for flag in flags if flag["kind"] == CardFlagKind.unit_unclear.value]
    assert lost_unit["params"]["token"] == "stk"
    [ocr] = [flag for flag in flags if flag["source"] == CardFlagSource.ocr.value]
    assert (ocr["kind"], ocr["params"]["value"], ocr["params"]["read"]) == ("read_disagreement", "375", "350")


def test_a_reading_of_notes_alone_keeps_its_note_flags_when_accepted_with_an_edit(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """
    A reading with notes and no ingredients or steps (a card of tips) is found in the accepted draft by its note ids
    (PL-08): its `unsure` flag on the note is raised even when the same save also changes the name
    """
    user = unique_user_fn_scoped
    job_id = _edited_job(user)
    tip = "Keeps 3 days in the fridge."
    proposed = CardDraft(name="Banana Tips", notes=[CardDraftNote(title="Storage", text=tip)])
    unsure = ExtractionUnsure(text="3 days", reason="faded", alternatives=["5 days"])
    extraction = _reading().model_copy(update={"unsure": [unsure]})
    _reextract(job_id, proposed, f"Banana Tips\nStorage\n{tip}", extraction=extraction)

    job = api_client.get(job_url(job_id), headers=user.token).json()
    [proposal] = job["proposals"]
    accepted = {**proposal["draft"], "name": "Banana Storage Tips"}
    saved = api_client.put(
        job_url(job_id),
        json={"draftVersion": job["draftVersion"], "draft": accepted, "resolvedProposalIds": [proposal["id"]]},
        headers=user.token,
    )
    assert saved.status_code == 200, saved.text
    [flag] = [flag for flag in saved.json()["flags"] if flag["kind"] == CardFlagKind.unsure.value]
    note_id = proposal["draft"]["notes"][0]["id"]
    assert (flag["field"], flag["ref"], flag["params"]["text"]) == ("notes", note_id, "3 days")
