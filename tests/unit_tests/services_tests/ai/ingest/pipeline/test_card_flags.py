"""`compute_flags` (docs/ai/PHASE2.md §4.6): every kind, stable ids, resolutions, edits and the reading flags on save"""

from typing import Any
from uuid import uuid4

import pytest

from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftNote,
    CardDraftRef,
    CardDraftStep,
    CardFlag,
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    ExtractionMeta,
    ExtractionUnsure,
    FlagResolution,
    IngestReadPath,
)
from mealie.services.ai.ingest.flag_rules import count_unresolved, is_clean
from mealie.services.ai.ingest.pipeline.flags import compute_flags, flag_id, ingredient_hash, ingredient_line


def ingredient(
    text: str,
    *,
    quantity: float | None = None,
    unit: str | None = None,
    food: str | None = None,
    note: str = "",
    confidence: float | None = 0.99,
    linked: bool = True,
) -> CardDraftIngredient:
    """An ingredient as extraction stores it, its units and foods linked unless `linked=False`"""
    line = CardDraftIngredient(
        original_text=text,
        quantity=quantity,
        unit=CardDraftRef(id=uuid4() if linked else None, name=unit) if unit else None,
        food=CardDraftRef(id=uuid4() if linked else None, name=food) if food else None,
        note=note,
        display=text,
        parse_confidence=confidence,
    )
    line.extracted_hash = ingredient_hash(line)
    return line


def draft(**kwargs: Any) -> CardDraft:
    defaults: dict[str, Any] = {
        "name": "Banana Mug Cake",
        "ingredients": [ingredient("1 banana", quantity=1, food="banana")],
        "steps": [CardDraftStep(text="Mash and mix.")],
    }
    return CardDraft(**{**defaults, **kwargs})


def kinds(flags: list[CardFlag]) -> set[CardFlagKind]:
    return {flag.kind for flag in flags}


def only(flags: list[CardFlag], kind: CardFlagKind) -> CardFlag:
    (flag,) = [flag for flag in flags if flag.kind == kind]
    return flag


# ==========================================
# Every kind


def scenarios() -> list[tuple[CardDraft, ExtractionMeta | None, str | None]]:
    """Drafts that together raise every kind"""
    sugar = ingredient("1 T. sugar", quantity=1, unit="tablespoon", food="sugar")
    chocolate = ingredient("1 sq chocolate", quantity=1, food="sq chocolate", confidence=0.6)
    flour = ingredient("11/2 cups flour", quantity=5.5, unit="cup", food="flour")
    milk = ingredient("25 cups milk", quantity=25, unit="cup", food="milk", linked=False)
    marked = ingredient("2 cups [illegible] flour", note="2 cups [illegible] flour", confidence=None)
    steps = [
        CardDraftStep(text="Bake at 600°F for 20 minutes."),
        CardDraftStep(text="Microwave for [blank] minutes."),
        CardDraftStep(text="Microwave for 2 minutes more."),
    ]
    read = ExtractionMeta(
        read_path=IngestReadPath.ocr,
        ocr_confidence=49.4,
        language="English",
        unsure=[ExtractionUnsure(text="1 sq chocolate", alternatives=["1 oz chocolate"], reason="faded")],
        cross_read_lines=["1 t. sugar", "Microwave for [blank] minutes more."],
        cross_read_failed=True,
    )
    transcription = "1 T. sugar\n11/2 cups flour\n25 cups milk\n[illegible] [illegible]\nBake at 600°F\nfor [blank]"
    return [
        (
            draft(name="", ingredients=[sugar, chocolate, flour, milk, marked], steps=steps),
            read,
            transcription,
        ),
        (draft(ingredients=[], steps=[]), ExtractionMeta(language="French"), None),
        (draft(ingredients=[ingredient("pain", food="pain")]), ExtractionMeta(language="fr"), None),
    ]


def test_every_kind_is_raised():
    raised: set[CardFlagKind] = set()
    for card, extraction, transcription in scenarios():
        raised |= kinds(compute_flags(card, extraction, {}, transcription=transcription))

    assert raised == set(CardFlagKind)


def test_each_kind_says_what_it_found():
    card, extraction, transcription = scenarios()[0]
    flags = compute_flags(card, extraction, {}, transcription=transcription)
    sugar, chocolate, flour, milk, marked = (str(line.reference_id) for line in card.ingredients)
    bake, gap, more = (str(step.id) for step in card.steps)
    by_id = {flag.id: flag for flag in flags}

    def check(kind: CardFlagKind, field: str, ref: str | None, severity: CardFlagSeverity, source: CardFlagSource):
        flag = by_id[flag_id(kind, field, ref)]
        assert (flag.kind, flag.field, flag.ref, flag.severity, flag.source) == (kind, field, ref, severity, source)
        return flag

    error, warning, info = CardFlagSeverity.error, CardFlagSeverity.warning, CardFlagSeverity.info
    S = CardFlagSource

    check(CardFlagKind.missing_name, "name", None, error, S.validator)
    check(CardFlagKind.illegible, "ingredients", marked, error, S.marker)
    check(CardFlagKind.blank, "steps", gap, error, S.marker)
    assert check(CardFlagKind.blank, "steps", more, error, S.cross_read).params == {"value": "2"}
    unsure = check(CardFlagKind.unsure, "ingredients", chocolate, warning, S.model)
    assert (unsure.params["text"], unsure.alternatives) == ("1 sq chocolate", ["1 oz chocolate"])
    assert check(CardFlagKind.not_on_card, "steps", bake, warning, S.validator).params == {"value": "20"}
    check(CardFlagKind.marker_dropped, "card", None, warning, S.validator)
    disagreement = check(CardFlagKind.read_disagreement, "ingredients", sugar, warning, S.cross_read)
    assert disagreement.params == {"text": "1 t. sugar", "value": "T"}
    assert disagreement.alternatives == ["1 t. sugar"]
    assert check(CardFlagKind.check_parse, "ingredients", chocolate, warning, S.parser).params == {"confidence": 60}
    assert check(CardFlagKind.unit_unclear, "ingredients", chocolate, warning, S.parser).params == {"token": "sq"}
    typo = check(CardFlagKind.implausible_amount, "ingredients", flour, warning, S.validator)
    assert (typo.params, typo.alternatives) == ({"value": "11/2", "suggestion": "1 1/2"}, ["1 1/2"])
    assert check(CardFlagKind.implausible_amount, "ingredients", milk, warning, S.validator).params == {
        "value": "25 cup"
    }
    assert check(CardFlagKind.implausible_temperature, "steps", bake, warning, S.validator).params == {"value": "600°F"}
    assert check(CardFlagKind.read_by_ocr, "card", None, warning, S.ocr).params == {"confidence": 49}
    check(CardFlagKind.cross_read_failed, "card", None, info, S.cross_read)
    assert check(CardFlagKind.shorthand_read, "ingredients", sugar, info, S.parser).params == {
        "from": "T.",
        "to": "tbsp",
    }
    assert check(CardFlagKind.new_food, "ingredients", milk, info, S.parser).params == {"name": "milk"}
    assert check(CardFlagKind.new_unit, "ingredients", milk, info, S.parser).params == {"name": "cup"}

    # numbers that are on the card aren't flagged, and a marker line isn't parsed
    assert flag_id(CardFlagKind.not_on_card, "ingredients", flour) not in by_id
    assert flag_id(CardFlagKind.check_parse, "ingredients", marked) not in by_id

    empty = compute_flags(*scenarios()[1][:2], {})
    assert {(flag.kind, flag.params.get("section")) for flag in empty} >= {
        (CardFlagKind.empty_section, "ingredients"),
        (CardFlagKind.empty_section, "steps"),
    }
    assert only(compute_flags(*scenarios()[2][:2], {}), CardFlagKind.not_parsed).field == "card"


def test_flags_come_in_reading_order():
    card, extraction, transcription = scenarios()[0]
    flags = compute_flags(card, extraction, {}, transcription=transcription)

    fields = [flag.field for flag in flags]
    assert fields[: fields.count("card")] == ["card"] * fields.count("card")
    order = ["card", "name", "ingredients", "steps"]
    assert list(dict.fromkeys(fields)) == order
    refs = [flag.ref for flag in flags if flag.field == "ingredients"]
    expected = [str(line.reference_id) for line in card.ingredients]
    assert list(dict.fromkeys(refs)) == [ref for ref in expected if ref in refs]


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Bake at 350°F.", False),
        ("Bake at 180°C.", False),
        ("Bake at 350 degrees.", False),
        ("Bake at 180°.", False),
        ("Bake at 600F.", True),
        ("Bake at 350°C.", True),
        ("Warm to 40 degrees.", True),
        ("Add 12 C. flour.", False),  # cups, not Celsius
    ],
)
def test_implausible_temperatures(text: str, flagged: bool):
    flags = compute_flags(draft(steps=[CardDraftStep(text=text)]), None, {})
    assert (CardFlagKind.implausible_temperature in kinds(flags)) is flagged


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("1 T. sugar", True),
        ("1/4 t. salt", True),
        ("1 C sugar", True),
        ("1 pkg yeast", True),
        ("1 Tbsp sugar", False),
        ("1 tsp salt", False),
    ],
)
def test_shorthand_read_only_when_the_unit_was_written_differently(text: str, flagged: bool):
    line = ingredient(text, quantity=1, unit="unit", food="food")
    assert (CardFlagKind.shorthand_read in kinds(compute_flags(draft(ingredients=[line]), None, {}))) is flagged


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        (ingredient("1 egg", quantity=1, food="egg"), False),  # the token is the food
        (ingredient("2 or 3 eggs", quantity=2, food="eggs"), False),
        (ingredient("1 lg onion", quantity=1, food="onion"), False),
        (ingredient("1 T. coconut oil", quantity=1, food="T. coconut oil"), True),  # the lost "T." (F6)
        (ingredient("1 tablespoon oil", quantity=1, unit="tablespoon", food="oil"), False),
    ],
)
def test_unit_unclear(line: CardDraftIngredient, flagged: bool):
    flags = compute_flags(draft(ingredients=[line]), None, {})
    assert (CardFlagKind.unit_unclear in kinds(flags)) is flagged


def test_short_unsure_texts_match_whole_tokens_only():
    lines = [ingredient("1 T. sugar", quantity=1, unit="tablespoon", food="sugar"), ingredient("Tea", food="tea")]
    unsure = [ExtractionUnsure(text="T.", alternatives=["t."]), ExtractionUnsure(text="4", alternatives=["9"])]
    flags = compute_flags(draft(ingredients=lines, recipe_servings=4), ExtractionMeta(unsure=unsure), {})

    flagged = {(flag.field, flag.ref) for flag in flags if flag.kind == CardFlagKind.unsure}
    assert flagged == {("ingredients", str(lines[0].reference_id)), ("recipeServings", None)}


# ==========================================
# Resolutions and edits


def test_resolutions_apply_where_they_may():
    card = draft(name="", steps=[CardDraftStep(text="Bake for [blank] minutes at 600°F.")])
    step = str(card.steps[0].id)
    resolutions = {
        flag_id(CardFlagKind.blank, "steps", step): FlagResolution.kept,
        flag_id(CardFlagKind.missing_name, "name"): FlagResolution.kept,  # can only be fixed
        flag_id(CardFlagKind.implausible_temperature, "steps", step): FlagResolution.dismissed,
        flag_id(CardFlagKind.blank, "steps", "elsewhere"): FlagResolution.kept,
    }

    flags = {flag.kind: flag for flag in compute_flags(card, None, resolutions)}

    assert flags[CardFlagKind.blank].resolution == FlagResolution.kept
    assert flags[CardFlagKind.missing_name].resolution is None
    assert flags[CardFlagKind.implausible_temperature].resolution == FlagResolution.dismissed
    assert count_unresolved(flags.values()) == (1, 0)

    # an error can't be dismissed, a warning can't be kept
    swapped = {
        flag_id(CardFlagKind.blank, "steps", step): FlagResolution.dismissed,
        flag_id(CardFlagKind.implausible_temperature, "steps", step): FlagResolution.kept,
    }
    assert all(flag.resolution is None for flag in compute_flags(card, None, swapped))


def test_a_marker_flag_resolves_itself_once_the_marker_is_gone():
    card = draft(steps=[CardDraftStep(text="Microwave for [blank] minutes.")])
    assert CardFlagKind.blank in kinds(compute_flags(card, None, {}))

    card.steps[0].text = "Microwave for 2 minutes."
    assert compute_flags(card, None, {}) == []
    assert is_clean(compute_flags(card, None, {}))


def test_parse_flags_drop_off_an_edited_line():
    line = ingredient("1 sq chocolate", quantity=1, food="sq chocolate", confidence=0.5, linked=False)
    card = draft(ingredients=[line])
    assert {CardFlagKind.check_parse, CardFlagKind.unit_unclear, CardFlagKind.new_food} <= kinds(
        compute_flags(card, None, {})
    )

    line.quantity = 2
    flags = compute_flags(card, None, {})
    assert kinds(flags) == {CardFlagKind.new_food}  # still not one of the group's foods
    assert ingredient_line(line) == "2 sq chocolate"


def test_a_line_typed_in_full_reads_from_its_fields():
    line = CardDraftIngredient(
        quantity=1.5, unit=CardDraftRef(name="cup"), food=CardDraftRef(name="flour"), note="sifted"
    )
    assert ingredient_line(line) == "1 1/2 cup flour sifted"


# ==========================================
# Reading flags on save


def _banana(step_text: str) -> CardDraft:
    return draft(
        ingredients=[ingredient("1 T. coconut oil", quantity=1, unit="tablespoon", food="coconut oil")],
        steps=[CardDraftStep(text="Mash and mix."), CardDraftStep(text=step_text)],
    )


def test_a_number_typed_into_a_blank_raises_no_reading_flags():
    transcription = "1 T. coconut oil\nMash and mix.\nMicrowave for [blank] minutes."
    extraction = ExtractionMeta(
        cross_read_lines=["1 T. coconut oil", "Mash and mix.", "Microwave for [blank] minutes."]
    )
    card = _banana("Microwave for [blank] minutes.")
    extracted = compute_flags(card, extraction, {}, transcription=transcription)
    assert [flag.kind for flag in extracted if flag.severity != CardFlagSeverity.info] == [CardFlagKind.blank]

    # the reviewer types "2" into the blank
    card.steps[1].text = "Microwave for 2 minutes."
    saved = compute_flags(card, extraction, {}, transcription=transcription, previous=extracted)
    assert is_clean(saved)
    assert saved == [flag for flag in saved if flag.kind == CardFlagKind.shorthand_read]

    # an extraction that produced the same draft is flagged: the number may have been made up
    fresh = kinds(compute_flags(card, extraction, {}, transcription=transcription))
    assert {CardFlagKind.not_on_card, CardFlagKind.marker_dropped, CardFlagKind.blank} <= fresh


def test_reading_flags_stay_while_they_hold():
    transcription = "1 T. coconut oil\nMash and mix.\nMicrowave for minutes."
    extraction = ExtractionMeta(unsure=[ExtractionUnsure(text="Mash and mix", alternatives=["Mash and fix"])])
    card = _banana("Microwave for 2 minutes.")
    extracted = compute_flags(card, extraction, {}, transcription=transcription)
    step = str(card.steps[1].id)
    assert {flag.id for flag in extracted} >= {flag_id(CardFlagKind.not_on_card, "steps", step)}

    resolutions = {flag_id(CardFlagKind.unsure, "steps", str(card.steps[0].id)): FlagResolution.dismissed}
    saved = compute_flags(card, extraction, resolutions, transcription=transcription, previous=extracted)
    assert {flag.kind for flag in saved} >= {CardFlagKind.not_on_card, CardFlagKind.unsure}
    assert only(saved, CardFlagKind.unsure).resolution == FlagResolution.dismissed

    # without the transcription, a not-on-card flag carries over while its number is still there
    carried = compute_flags(card, extraction, {}, previous=extracted)
    assert only(carried, CardFlagKind.not_on_card).params == {"value": "2"}

    card.steps[1].text = "Microwave for a few minutes."
    assert CardFlagKind.not_on_card not in kinds(compute_flags(card, extraction, {}, previous=extracted))
    assert CardFlagKind.not_on_card not in kinds(
        compute_flags(card, extraction, {}, transcription=transcription, previous=extracted)
    )


def test_notes_and_single_fields_are_keyed_without_an_index_of_their_own():
    card = draft(
        description="A [illegible] cake",
        total_time="[blank] minutes",
        notes=[CardDraftNote(text="fine"), CardDraftNote(title="[illegible]", text="Serve warm")],
        attribution="From [illegible]",
    )
    ids = [flag.id for flag in compute_flags(card, None, {})]
    assert ids == ["illegible:description:", "blank:totalTime:", "illegible:notes:1", "illegible:attribution:"]
