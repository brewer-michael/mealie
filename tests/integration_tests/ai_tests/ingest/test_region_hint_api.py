"""
`GET /jobs/{id}/region-hint?field=&ref=` (docs/ai/PHASE2.md §6.5): where on the card a field's text probably is, so
the re-read selection starts at the flagged line rather than as a band across the middle. By Tesseract's lines when
orientation stored them, else by the line's place in the transcription; 404 `not_found` when neither finds the text,
as for a card that isn't there. Runs on SQLite and PostgreSQL.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import assert_code, banana_draft, job_row, job_url, seed_job, set_columns, use_fake_flags

from mealie.schema.recipe_ingest import CardDraftIngredient, CardDraftNote, IngestStatus
from tests.utils.fixture_schemas import TestUser

TRANSCRIPTION = (
    "Banana Mug Cake\nA quick cake\n1 T. coconut oil (melted)\n1/4 t. salt\n"
    "Mash the banana in a mug and stir in everything else.\nMicrowave for [blank] minutes.\nFrom Grandma Jo"
)

LINES = [
    # text, top, height: a card read by Tesseract, top to bottom
    ("Banana Mug Cake", 0.05, 0.05),
    ("A quick cake", 0.12, 0.03),
    ("1 T. coconut oil (melted)", 0.22, 0.03),
    ("1/4 t. salt", 0.26, 0.03),
    ("Mash the banana in a mug and stir in", 0.50, 0.03),
    ("everything else.", 0.54, 0.03),
    ("Microwave for minutes.", 0.60, 0.03),
    ("From Grandma Jo", 0.90, 0.03),
]


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


def _card(user: TestUser, *, ocr: bool, lines: list[tuple[str, float, float]] = LINES, **columns: Any) -> UUID:
    columns.setdefault("transcription", TRANSCRIPTION)
    job_id = seed_job(user, page_count=2, **columns)
    if ocr:
        pages = job_row(job_id)["pages"]
        pages[0]["ocr"] = {
            "text": "\n".join(text for text, _, _ in lines),
            "confidence": 88.0,
            "lines": [{"text": text, "x": 0.08, "y": y, "width": 0.7, "height": h} for text, y, h in lines],
        }
        set_columns(job_id, pages=pages)
    return job_id


def _hint(api_client: TestClient, user: TestUser, job_id: UUID, field: str, ref: Any = None) -> Any:
    params = {"field": field} if ref is None else {"field": field, "ref": str(ref)}
    return api_client.get(job_url(job_id, "region-hint"), params=params, headers=user.token)


def _covers(hint: dict[str, Any], top: float, bottom: float) -> bool:
    return hint["y"] <= top and bottom <= hint["y"] + hint["height"]


def test_tesseracts_lines_place_the_hint_on_the_line(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = _card(user, ocr=True)
    draft = job_row(job_id)["draft"]

    response = _hint(api_client, user, job_id, "ingredients", draft["ingredients"][0]["reference_id"])
    assert response.status_code == 200, response.text
    hint = response.json()
    assert (hint["page"], hint["source"], hint["x"], hint["width"]) == (0, "ocr", 0.05, 0.9)
    assert _covers(hint, 0.22, 0.25)
    assert hint["y"] + hint["height"] <= 0.27  # not the next line

    step = _hint(api_client, user, job_id, "steps", draft["steps"][0]["id"]).json()
    assert step["source"] == "ocr"
    assert _covers(step, 0.50, 0.57)  # a step over two lines

    name = _hint(api_client, user, job_id, "name").json()
    assert (name["source"], name["page"]) == ("ocr", 0)
    assert _covers(name, 0.05, 0.10)

    attribution = _hint(api_client, user, job_id, "attribution").json()
    assert _covers(attribution, 0.90, 0.93)


def test_without_tesseracts_lines_the_line_in_the_transcription_places_it(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    job_id = _card(user, ocr=False)
    draft = job_row(job_id)["draft"]

    first = _hint(api_client, user, job_id, "ingredients", draft["ingredients"][0]["reference_id"]).json()
    last = _hint(api_client, user, job_id, "steps", draft["steps"][1]["id"]).json()
    assert (first["source"], last["source"]) == ("position", "position")
    # without "Front:" and "Back:", a card's seven lines are shared by its pages in order: the third of the front's
    # four, and the second of the back's three
    assert (first["page"], last["page"]) == (0, 1)
    assert _covers(first, 0.625, 0.625) and _covers(last, 0.5, 0.5)


def test_an_edited_line_is_found_by_what_the_card_says(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    banana = banana_draft()
    edited = banana.ingredients[1].model_copy(update={"note": "a pinch", "quantity": None})
    added = CardDraftIngredient(note="1/4 t. salt", display="1/4 t. salt")  # a line the reviewer typed again
    job_id = _card(
        user, ocr=True, draft=banana.model_copy(update={"ingredients": [banana.ingredients[0], edited, added]})
    )

    for ref in (edited.reference_id, added.reference_id):
        hint = _hint(api_client, user, job_id, "ingredients", ref).json()
        assert (hint["source"], _covers(hint, 0.26, 0.29)) == ("ocr", True)


def test_a_note_by_its_id(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    note = CardDraftNote(title="", text="Mash the banana in a mug and stir in everything else.")
    job_id = _card(user, ocr=True, draft=banana_draft(notes=[note]))
    hint = _hint(api_client, user, job_id, "notes", note.id).json()
    assert _covers(hint, 0.50, 0.57)


def test_no_hint_is_a_404(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    typed = CardDraftIngredient(note="2 cups of something nobody wrote", display="")
    job_id = _card(user, ocr=True, draft=banana_draft(ingredients=[typed], description=""))

    assert_code(_hint(api_client, user, job_id, "ingredients", typed.reference_id), 404, "not_found")  # not on the card
    assert_code(_hint(api_client, user, job_id, "ingredients", uuid4()), 404, "not_found")  # no such line
    assert_code(_hint(api_client, user, job_id, "steps", uuid4()), 404, "not_found")
    assert_code(_hint(api_client, user, job_id, "ingredients"), 404, "not_found")  # a list field needs its line
    assert_code(_hint(api_client, user, job_id, "description"), 404, "not_found")  # empty
    assert_code(_hint(api_client, user, job_id, "colour"), 404, "not_found")
    assert api_client.get(job_url(job_id, "region-hint"), headers=user.token).status_code == 422

    no_reading = _card(user, ocr=False, transcription=None)
    assert_code(_hint(api_client, user, no_reading, "name"), 404, "not_found")

    failed = seed_job(user, status=IngestStatus.failed)
    assert_code(_hint(api_client, user, failed, "name"), 404, "not_found")


def test_another_households_card_is_not_found(api_client: TestClient, unique_user: TestUser, h2_user: TestUser):
    job_id = _card(h2_user, ocr=True)
    assert_code(_hint(api_client, unique_user, job_id, "name"), 404, "not_found")


TWO_EGGS = [
    # a short line the card says twice: in the cake and in the frosting
    ("Two Egg Cake", 0.05, 0.05),
    ("For the cake", 0.15, 0.03),
    ("1 egg", 0.20, 0.03),
    ("2 c. flour", 0.25, 0.03),
    ("For the frosting", 0.40, 0.03),
    ("1 egg", 0.45, 0.03),
    ("1 c. powdered sugar", 0.50, 0.03),
    ("Beat well and bake.", 0.70, 0.03),
]


def test_a_line_the_card_says_twice_is_found_as_the_one_it_is(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """The frosting's "1 egg" is the card's second, not the cake's: by which of the draft's "1 egg" lines it is"""
    user = unique_user_fn_scoped
    texts = [text for text, _, _ in TWO_EGGS]
    lines = [
        CardDraftIngredient(title=title, original_text=text, note=text, display=text)
        for title, text in (
            ("For the cake", "1 egg"),
            ("For the cake", "2 c. flour"),
            ("For the frosting", "1 Egg"),  # as the hint compares lines: case and spacing aside
            ("For the frosting", "1 c. powdered sugar"),
        )
    ]
    draft = banana_draft(name="Two Egg Cake", ingredients=lines, description="")
    for ocr in (True, False):
        job_id = _card(user, ocr=ocr, lines=TWO_EGGS, transcription="\n".join(texts), draft=draft)
        cake, frosting = (_hint(api_client, user, job_id, "ingredients", lines[i].reference_id) for i in (0, 2))
        assert cake.status_code == frosting.status_code == 200, (cake.text, frosting.text)
        cake_hint, frosting_hint = cake.json(), frosting.json()
        if ocr:
            # between the Tesseract lines most like the transcription's lines around each
            assert (cake_hint["source"], frosting_hint["source"]) == ("ocr", "ocr")
            assert _covers(cake_hint, 0.20, 0.23) and cake_hint["y"] + cake_hint["height"] <= 0.25
            assert _covers(frosting_hint, 0.45, 0.48) and frosting_hint["y"] >= 0.43
        else:
            # without them, the transcription's lines: the cake's "1 egg" is the front's third of four, the
            # frosting's the back's second
            assert (cake_hint["source"], frosting_hint["source"]) == ("position", "position")
            assert (cake_hint["page"], frosting_hint["page"]) == (0, 1)
            assert _covers(cake_hint, 0.625, 0.625) and _covers(frosting_hint, 0.375, 0.375)
