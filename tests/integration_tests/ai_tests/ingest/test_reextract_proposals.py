"""
Two whole-card re-extracts of an edited draft (docs/ai/PHASE2.md §3.3, §6.6): the job keeps one reading, the newest,
so the newer whole-card proposal replaces the older one. Accepting the older one (still on another screen) is a 409
and the page reloads; accepting the newer one raises the flags of its own reading, a printed card's number Tesseract
may have misread included: its line is read again from the page, before the save's write (§4.5). With the real flag
rules. Runs on SQLite and PostgreSQL.
"""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from test_jobs_api import assert_code, banana_draft, job_row, job_url, seed_job, set_columns

from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestJobsRepo
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
from mealie.services import ocr
from mealie.services.ai.ingest import images, review, storage
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
    ocr: str | dict[str, Any] | None = None,
    extraction: ExtractionMeta | None = None,
) -> None:
    """
    A re-extract of the job, finished: its result finalized as the runner does (`ocr`: Tesseract's printed text, or
    the pages' whole `ocr`, with its lines' boxes)
    """
    token = uuid4()
    set_columns(
        job_id, task_kind=IngestTaskKind.extract.value, task_state=IngestTaskState.running.value, lease_token=token
    )
    extraction = extraction or _reading()
    pages = job_row(job_id)["pages"]
    if ocr is not None:
        page_ocr = ocr if isinstance(ocr, dict) else {"text": ocr, "confidence": 95.0}
        pages = [{**page, "ocr": page_ocr} for page in pages]
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


def _accept_the_reading(api_client: TestClient, user: TestUser, job_id: UUID) -> list[dict[str, Any]]:
    """Accepts the job's whole-card proposal as it stands; the save's flags"""
    job = api_client.get(job_url(job_id), headers=user.token).json()
    [proposal] = job["proposals"]
    saved = api_client.put(
        job_url(job_id),
        json={"draftVersion": job["draftVersion"], "draft": proposal["draft"], "resolvedProposalIds": [proposal["id"]]},
        headers=user.token,
    )
    assert saved.status_code == 200, saved.text
    return saved.json()["flags"]


def _ocr_flags(flags: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    return [
        (flag["kind"], flag["field"], flag["params"]["value"], flag["params"]["read"])
        for flag in flags
        if flag["source"] == CardFlagSource.ocr.value
    ]


BAKE = "Bake at 375° for 1 hour, then cool 10 minutes."
"""A printed card's step whose "375" Tesseract read as "315" (`_printed_reading`)"""


def _printed_reading(job_id: UUID) -> None:
    """A finished re-extract of a printed card whose step's "375" Tesseract read as "315", its lines with boxes"""
    proposed = banana_draft(name="Banana Cake", steps=[CardDraftStep(text=MASH), CardDraftStep(text=BAKE)])
    printed = "\n".join(["Banana Cake", "Prep 5 minutes", "1 T. coconut oil (melted)", "1/4 t. salt", MASH, BAKE])
    read = printed.replace("375", "315").splitlines()
    boxes = [
        {"text": text, "x": 0.1, "y": 0.1 * (row + 1), "width": 0.8, "height": 0.05} for row, text in enumerate(read)
    ]
    _reextract(job_id, proposed, printed, ocr={"text": "\n".join(read), "confidence": 95.0, "lines": boxes})


class _Tesseract:
    """Tesseract's second readings, faked: each line reads `again`; records which, and whether inside a draft's write"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, again: str) -> None:
        self.again = again
        self.writing = False
        self.asked: list[tuple[Path, float, bool]] = []
        write = IngestJobsRepo.update_job_json

        def update_job_json(repo: IngestJobsRepo, *args: Any, **kwargs: Any) -> Any:
            self.writing = True
            try:
                return write(repo, *args, **kwargs)
            finally:
                self.writing = False

        monkeypatch.setattr(IngestJobsRepo, "update_job_json", update_job_json)
        monkeypatch.setattr(ocr, "binary_available", lambda: True)
        monkeypatch.setattr(ocr, "read_line", self.read_line)

    def read_line(self, path: Path, x: float, y: float, width: float, height: float) -> str:
        self.asked.append((path, round(y, 2), self.writing))
        return self.again


@pytest.mark.parametrize(
    ("again", "flagged"),
    [
        ("Bake at 315° for 1 hour, then cool 10 minutes.", [("read_disagreement", "steps", "375", "315")]),
        ("Bake at 375° for 1 hour, then cool 10 minutes.", []),
    ],
)
def test_accepting_a_reading_reads_a_digit_tesseract_confuses_again(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    again: str,
    flagged: list[tuple[str, str, str, str]],
):
    """
    Tesseract read the printed card's "375" as "315", digits it confuses: the re-extract read that line again from
    the page by its box, and so does the save that accepts the reading, before its write (Tesseract takes seconds;
    the write holds the row's transaction). Flagged when the second reading says what the first did.
    """
    user = unique_user_fn_scoped
    job_id = _edited_job(user)
    _printed_reading(job_id)
    tesseract = _Tesseract(monkeypatch, again)

    flags = _accept_the_reading(api_client, user, job_id)
    assert _ocr_flags(flags) == flagged
    # the step's line, from the card's page by its box, once, and never inside the draft's write
    page = storage.page_dir(UUID(user.group_id), job_id, 0) / images.PAGE_FILE
    assert tesseract.asked == [(page, 0.6, False)]


def test_a_reading_accepted_while_the_pages_change_is_read_from_them_again(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """
    The card's pages changed between the save's second readings and its write (another device added a page): the
    lines are read again from the pages as they are now, still outside the write, rather than judged by readings of
    pages that changed
    """
    user = unique_user_fn_scoped
    job_id = _edited_job(user)
    _printed_reading(job_id)
    tesseract = _Tesseract(monkeypatch, "Bake at 315° for 1 hour, then cool 10 minutes.")

    reads = review.ReviewService._adopted_ocr_rereads
    calls = 0

    def adopted_ocr_rereads(service: review.ReviewService, *args: Any) -> review.OCRRereads | None:
        nonlocal calls
        calls += 1
        rereads = reads(service, *args)
        if calls == 1:
            pages = job_row(job_id)["pages"]
            set_columns(job_id, pages=[*pages, {**pages[0], "index": 1}])
        return rereads

    monkeypatch.setattr(review.ReviewService, "_adopted_ocr_rereads", adopted_ocr_rereads)

    flags = _accept_the_reading(api_client, user, job_id)
    assert calls == 2
    assert _ocr_flags(flags) == [("read_disagreement", "steps", "375", "315")]
    # read once for each: the step's line on the front, never inside the draft's write
    page = storage.page_dir(UUID(user.group_id), job_id, 0) / images.PAGE_FILE
    assert tesseract.asked == [(page, 0.6, False)] * 2


def _print_card(path: Path, lines: list[str]) -> None:
    image = Image.new("RGB", (1600, 160 + len(lines) * 150), "white")
    draw = ImageDraw.Draw(image)
    for row, text in enumerate(lines):
        draw.text((80, 80 + row * 150), text, fill="black", font=ImageFont.load_default(size=72))
    image.save(path, "JPEG", quality=95)


@pytest.mark.skipif(not ocr.binary_available(), reason="tesseract is not installed")
def test_accepting_a_reading_keeps_the_ocr_flag_its_second_reading_raised(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """
    With Tesseract: the image reader's "7" for the printed card's "1", which Tesseract reads right, is a digit it
    confuses; read again from the page, the line says "1", and accepting the reading raises the flag
    """
    user = unique_user_fn_scoped
    job_id = _edited_job(user)
    lines = ["Banana Cake", "1 cup butter", "2 cups sugar", "Bake at 350 for 30 minutes."]
    page = storage.page_dir(UUID(user.group_id), job_id, 0) / images.PAGE_FILE
    _print_card(page, lines)
    result = ocr.extract_text(page, require_enabled=False)
    boxes = [
        {"text": line.text, "x": line.x, "y": line.y, "width": line.width, "height": line.height}
        for line in result.lines
    ]
    assert "1 cup butter" in result.text

    def ingredient(text: str, quantity: float, unit: str, food: str) -> CardDraftIngredient:
        line = CardDraftIngredient(
            original_text=text,
            quantity=quantity,
            unit=CardDraftRef(name=unit),
            food=CardDraftRef(name=food),
            display=text,
            parse_confidence=0.95,
        )
        line.extracted_hash = ingredient_hash(line)
        return line

    proposed = banana_draft(
        name="Banana Cake",
        ingredients=[ingredient("7 cup butter", 7, "cup", "butter"), ingredient("2 cups sugar", 2, "cup", "sugar")],
        steps=[CardDraftStep(text=lines[-1])],
    )
    transcription = "\n".join(lines).replace("1 cup butter", "7 cup butter")
    _reextract(
        job_id, proposed, transcription, ocr={"text": result.text, "confidence": result.confidence, "lines": boxes}
    )

    flags = _accept_the_reading(api_client, user, job_id)
    assert _ocr_flags(flags) == [("read_disagreement", "ingredients", "7", "1")]
