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
    # no group has a food named "sq chocolate": the lost unit is glued to a new food
    chocolate = ingredient("1 sq chocolate", quantity=1, food="sq chocolate", confidence=0.6, linked=False)
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
    assert disagreement.params == {"text": "1 t. sugar", "value": "tbsp"}
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
        ("Bake at 40 degrees.", True),
        ("Add 12 C. flour.", False),  # cups, not Celsius
        # rising, cooling and warm water aren't oven temperatures: only too hot is implausible there
        ("Let rise in a warm place (80°) until doubled.", False),
        ("Cool to 70° before slicing.", False),
        ("Dissolve yeast in warm water (110°F).", False),
        ("Cook to 238° (soft ball stage).", False),
        ("Heat the oil to 975°.", True),
        ("Let rise in a warm place (80°) until doubled, then bake at 350° for 30 minutes.", False),
        ("Bake at 350° for 1 hr. Cool to 70°.", False),
        # a misread oven temperature is still caught, wherever the oven is named in the sentence
        ("Bake at 35° for 30 minutes.", True),
        ("Bake 30 min. at 35°.", True),  # an abbreviation's dot doesn't end the sentence
        ("Preheat oven to 35 degrees.", True),
        ("Put in a 35° oven until golden.", True),
        ("Let rise in a warm place (80°) until doubled, then bake at 35° for 30 minutes.", True),
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
        # two-word foods, as the parser reads them, whether or not the group has the food yet
        (ingredient("1 egg yolk", quantity=1, food="egg yolk", linked=False), False),
        (ingredient("2 egg whites", quantity=2, food="egg whites"), False),
        (ingredient("1 red pepper, chopped", quantity=1, food="red pepper", note="chopped", linked=False), False),
        (ingredient("1 bay leaf", quantity=1, food="bay leaves", linked=False), False),
        (ingredient("1 pie crust", quantity=1, food="pie crust"), False),
        (ingredient("1 hot dog", quantity=1, food="hot dog", linked=False), False),
        (ingredient("1 key lime pie", quantity=1, food="key lime pie"), False),  # the group's own food
        # lost units, as the parser reads them, are still flagged
        (ingredient("1 doz. eggs", quantity=1, food="doz eggs", linked=False), True),
        (ingredient("1 doz eggs", quantity=1, food="doz eggs", linked=False), True),
        (ingredient("1 env. yeast", quantity=1, food="env. yeast", linked=False), True),
        (ingredient("1 sq chocolate", quantity=1, food="chocolate", note="sq"), True),
        (ingredient("1 sq chocolate", quantity=1, food="sq chocolate", linked=False), True),
        (ingredient("1 egg. yolk", quantity=1, food="egg. yolk", linked=False), True),  # an abbreviation's dot
    ],
)
def test_unit_unclear(line: CardDraftIngredient, flagged: bool):
    flags = compute_flags(draft(ingredients=[line]), None, {})
    assert (CardFlagKind.unit_unclear in kinds(flags)) is flagged


BANANA_LINES = [
    ingredient("1 banana", quantity=1, food="banana"),
    ingredient("1 T. coconut oil (melted)", quantity=1, unit="tablespoon", food="coconut oil", note="melted"),
    ingredient("1/4 t. salt", quantity=0.25, unit="teaspoon", food="salt"),
    ingredient("1/2 t vanilla", quantity=0.5, unit="teaspoon", food="vanilla", linked=False),
    ingredient("1/3 C. almond flour", quantity=1 / 3, unit="cup", food="almond flour", linked=False),
    ingredient("1 egg", quantity=1, food="egg"),
    ingredient("Cinnamon to taste", food="Cinnamon", note="to taste", linked=False),
]


@pytest.mark.parametrize(
    ("line", "dropped"),
    [
        # as Mealie's NLP parser reads them: one quantity, so the rest would be gone from the recipe
        (ingredient("2-3 T. milk", quantity=2, unit="tablespoon", food="milk"), "2-3"),
        (ingredient("1 to 2 c. water", quantity=1, unit="cup", food="water"), "1 to 2"),
        (ingredient("3-4 apples", quantity=3, food="apples"), "3-4"),
        (ingredient("1/2 - 3/4 c. sugar", quantity=0.5, unit="cup", food="sugar"), "1/2 - 3/4"),
        (ingredient("2 or 3 eggs", quantity=2, food="egg"), "3"),
        (ingredient("1 dozen eggs", quantity=1, food="egg"), "1 dozen"),
        (ingredient("1 (16 oz.) can tomatoes", quantity=1, unit="can", food="tomatoes", confidence=0.909), "16"),
        (ingredient("1 (8 oz) pkg cream cheese", quantity=1, food="cream cheese", confidence=0.881), "8"),
        (ingredient("2 T. butter + 1 T. oil", quantity=2, unit="tablespoon", food="butter + 1 T. oil"), "1"),
    ],
)
def test_an_amount_the_parsed_fields_lost_is_flagged(line: CardDraftIngredient, dropped: str):
    """The parser keeps one quantity, and commit writes the parsed fields: a lost range end or size is checked"""
    flags = compute_flags(draft(ingredients=[*BANANA_LINES, line]), None, {})

    flag = only(flags, CardFlagKind.check_parse)
    assert (flag.ref, flag.severity, flag.params) == (
        str(line.reference_id),
        CardFlagSeverity.warning,
        {"value": dropped},
    )
    assert not is_clean(flags)

    # it's a parse flag: once the reviewer edits the line, it says what they typed
    line.quantity = 2.5
    assert CardFlagKind.check_parse not in kinds(compute_flags(draft(ingredients=[line]), None, {}))


@pytest.mark.parametrize(
    "line",
    [
        ingredient("1 1/2 c. flour", quantity=1.5, unit="cup", food="flour"),
        ingredient("1½ T. honey", quantity=1.5, unit="tablespoon", food="honey"),
        ingredient("1 9-inch pie shell", quantity=1, food="pie shell", note="9 inch"),  # the size is in the note
        ingredient("Juice of 1 lemon", quantity=1, food="lemon", note="Juice of"),
        ingredient("1 doz. eggs", quantity=1, food="doz eggs", linked=False),  # unit_unclear says so instead
        ingredient("2 eggs, beaten", quantity=2, food="egg", note="beaten"),
    ],
)
def test_an_amount_the_parsed_fields_keep_is_not(line: CardDraftIngredient):
    flags = compute_flags(draft(ingredients=[*BANANA_LINES, line]), None, {})
    assert CardFlagKind.check_parse not in kinds(flags)


def test_the_banana_card_reads_clean():
    """The real card's lines raise nothing highlighted, besides the gap it leaves on purpose"""
    card = draft(
        ingredients=BANANA_LINES,
        steps=[
            CardDraftStep(text="Mash banana and mix ingredients thoroughly."),
            CardDraftStep(text="Microwave in bowl or large mug for [blank] minutes or until firm in center."),
        ],
    )
    transcription = "\n".join([line.original_text for line in BANANA_LINES] + [step.text for step in card.steps])

    flags = compute_flags(card, ExtractionMeta(language="English"), {}, transcription=transcription)

    highlighted = [flag for flag in flags if flag.severity != CardFlagSeverity.info]
    assert [(flag.kind, flag.field) for flag in highlighted] == [(CardFlagKind.blank, "steps")]


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
    flags = compute_flags(card, None, {})
    assert [(flag.id.split("#")[0], flag.field, flag.ref) for flag in flags] == [
        ("illegible:description:", "description", None),
        ("blank:totalTime:", "totalTime", None),
        ("illegible:notes:1", "notes", "1"),  # the note's position, and a digest of what it says
        ("illegible:attribution:", "attribution", None),
    ]
    assert flags[2].id.startswith("illegible:notes:1#")


def _note_flag(flags: list[CardFlag], ref: str) -> CardFlag:
    (flag,) = [flag for flag in flags if flag.field == "notes" and flag.ref == ref]
    return flag


def test_a_kept_note_flag_stays_with_its_note():
    """A note has no id of its own; keeping one note's blank never keeps another's when notes move"""
    card = draft(notes=[CardDraftNote(text="Bake [blank] min"), CardDraftNote(text="Serve with [blank]")])
    first = _note_flag(compute_flags(card, None, {}), "0")
    resolutions = {first.id: FlagResolution.kept}

    flags = compute_flags(card, None, resolutions)
    assert _note_flag(flags, "0").resolution == FlagResolution.kept
    assert count_unresolved(flags) == (1, 0)  # the second note's blank
    assert compute_flags(card, None, resolutions) == flags  # stable from save to save

    # the reviewer deletes the first note: the second moves up, and its blank still needs a look
    del card.notes[0]
    flags = compute_flags(card, None, resolutions)
    assert _note_flag(flags, "0").resolution is None
    assert count_unresolved(flags) == (1, 0)

    # likewise when notes are reordered
    card.notes = [CardDraftNote(text="Serve with [blank]"), CardDraftNote(text="Bake [blank] min")]
    assert count_unresolved(compute_flags(card, None, resolutions)) == (2, 0)


def test_a_numbered_list_in_the_transcription_puts_no_number_on_the_card():
    """The transcription numbers the steps in markdown; the "2" invented for the card's gap is still not on it"""
    transcription = "\n".join(
        [
            "# Banana Mug Cake",
            "## Ingredients",
            *(f"- {line.original_text}" for line in BANANA_LINES),
            "## Directions",
            "1. Mash banana and mix ingredients thoroughly.",
            "2. Microwave in bowl or large mug for [blank] minutes or until firm in center.",
        ]
    )
    card = draft(
        ingredients=BANANA_LINES,
        steps=[
            CardDraftStep(text="Mash banana and mix ingredients thoroughly."),
            CardDraftStep(text="Microwave in bowl or large mug for 2 minutes or until firm in center."),
        ],
    )

    flags = compute_flags(card, ExtractionMeta(language="English"), {}, transcription=transcription)

    assert only(flags, CardFlagKind.not_on_card).params == {"value": "2"}
    assert only(flags, CardFlagKind.not_on_card).ref == str(card.steps[1].id)
    # numbers that are on the card aren't flagged, list or not
    card.steps[1].text = "Microwave in bowl or large mug for [blank] minutes or until firm in center."
    assert CardFlagKind.not_on_card not in kinds(compute_flags(card, None, {}, transcription=transcription))


def test_the_cross_read_catches_a_number_in_a_gap_when_a_word_reads_differently():
    card = draft(
        ingredients=[ingredient("2 eggs", quantity=2, food="egg"), ingredient("1/3 C. flour", quantity=1 / 3)],
        steps=[CardDraftStep(text="Microwave in bowl or large mug for 2 minutes or until firm in center.")],
    )
    second = [
        "2 egg",
        "1/3 c. flour",
        "Cream shortening and sugar. Add eggs",
        "thoroughly. Microwave in bowl or lg",
        "mug for [blank] min. or until firm",
        "in center.",
    ]

    flags = compute_flags(card, ExtractionMeta(cross_read_lines=second), {})

    blank = only(flags, CardFlagKind.blank)
    assert (blank.source, blank.field, blank.params) == (CardFlagSource.cross_read, "steps", {"value": "2"})
    # the ingredients agree with their own lines: "C." and "c." are both cups, and "Add eggs" is a step
    assert CardFlagKind.read_disagreement not in kinds(flags)
