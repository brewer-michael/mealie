"""`compute_flags` (docs/ai/PHASE2.md §4.6): every kind, stable ids, resolutions, edits and the reading flags on save"""

import hashlib
import json
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
    PageOCR,
)
from mealie.services.ai.ingest.flag_rules import count_unresolved, is_clean
from mealie.services.ai.ingest.pipeline import flags as card_flags
from mealie.services.ai.ingest.pipeline.flags import (
    MAX_ANALYSED_LINE,
    ORGANIZERS_STEP,
    compute_flags,
    flag_id,
    ingredient_hash,
    ingredient_line,
    is_unedited,
    keep_alternatives,
    keep_lost_amounts,
    ocr_check_lines,
)
from mealie.services.recipe.import_workflow.steps import ResolveOrganizersStep


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
        # a card in another language whose line the AI parser didn't parse
        (draft(ingredients=[ingredient("pain", confidence=None)]), ExtractionMeta(language="fr"), None),
    ]


def test_every_kind_is_raised():
    raised: set[CardFlagKind] = set()
    for card, extraction, transcription in scenarios():
        raised |= kinds(compute_flags(card, extraction, {}, transcription=transcription))
    # a food linked by a near-miss name, judged by the names of the group's foods
    onions = ingredient("2 rd onions", quantity=2, food="red onion")
    assert onions.food is not None and onions.food.id is not None
    raised |= kinds(compute_flags(draft(ingredients=[onions]), None, {}, linked={onions.food.id: ["red onion"]}))
    # tag suggestions that failed
    failed = ExtractionMeta(step_outcomes={"build-recipe": "completed", "resolve-organizers": "failed"})
    raised |= kinds(compute_flags(draft(), failed, {}))

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
    # where each value is in the text the flag was computed on ("Microwave for 2 minutes more.")
    assert check(CardFlagKind.blank, "steps", more, error, S.cross_read).params == {
        "value": "2",
        "start": 14,
        "end": 15,
    }
    unsure = check(CardFlagKind.unsure, "ingredients", chocolate, warning, S.model)
    assert (unsure.params["text"], unsure.alternatives) == ("1 sq chocolate", ["1 oz chocolate"])
    not_on_card = check(CardFlagKind.not_on_card, "steps", bake, warning, S.validator)
    assert not_on_card.params == {"value": "20", "start": 18, "end": 20}
    check(CardFlagKind.marker_dropped, "card", None, warning, S.validator)
    disagreement = check(CardFlagKind.read_disagreement, "ingredients", sugar, warning, S.cross_read)
    assert disagreement.params == {"text": "1 t. sugar", "value": "tbsp", "start": 2, "end": 3}
    assert disagreement.alternatives == ["1 t. sugar"]
    assert check(CardFlagKind.check_parse, "ingredients", chocolate, warning, S.parser).params == {"confidence": 60}
    unclear = check(CardFlagKind.unit_unclear, "ingredients", chocolate, warning, S.parser)
    assert unclear.params == {"token": "sq", "start": 2, "end": 4}
    typo = check(CardFlagKind.implausible_amount, "ingredients", flour, warning, S.validator)
    assert (typo.params, typo.alternatives) == (
        {"value": "11/2", "suggestion": "1 1/2", "start": 0, "end": 4},
        ["1 1/2"],
    )
    assert check(CardFlagKind.implausible_amount, "ingredients", milk, warning, S.validator).params == {
        "value": "25 cup",
        "start": 0,
        "end": 2,
    }
    temperature = check(CardFlagKind.implausible_temperature, "steps", bake, warning, S.validator)
    assert temperature.params == {"value": "600°F", "start": 8, "end": 13}
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


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("failed", "failed"),
        ("skipped:local_only", "local_only"),
        ("skipped:limit_reached", "limit_reached"),
        ("completed", None),
        (None, None),  # the group has no organizers: none were asked for
        ("skipped", None),  # suggestions turned off
    ],
)
def test_skipped_tag_suggestions_are_said(outcome: str | None, reason: str | None):
    outcomes = {"compile-source": "completed", "build-recipe": "completed"}
    if outcome is not None:
        outcomes["resolve-organizers"] = outcome
    extraction = ExtractionMeta(step_outcomes=outcomes)

    flags = [flag for flag in compute_flags(draft(), extraction, {}) if flag.kind == CardFlagKind.organizers_skipped]

    if reason is None:
        assert flags == []
    else:
        (flag,) = flags
        assert (flag.id, flag.field, flag.ref) == ("organizers_skipped:card:", "card", None)
        assert (flag.severity, flag.source, flag.params) == (
            CardFlagSeverity.info,
            CardFlagSource.model,
            {"reason": reason},
        )
        assert is_clean(flags)  # shown quietly: nothing to fix
        # it says so on every save, whatever the reviewer does
        assert compute_flags(draft(), extraction, {}, previous=flags)[0] == flag


def test_the_organizer_step_is_named_as_upstream_names_it():
    assert ORGANIZERS_STEP == ResolveOrganizersStep.name


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
        # terse cards name no oven; a misread temperature is still caught
        ("350° - 30 min.", False),
        ("35° - 30 min.", True),
        ("35° 30 min", True),
        ("Pour into greased pan. 35° for 1 hr.", True),
        ("Cover with foil; 35° for 2 hrs.", True),
        ("Cook at 35° for 1 hour.", True),
        ("Fry in deep fat at 37°.", True),
        ("Let rise until doubled and bake at 37° for 30 min.", True),  # the nearest word says what it's for
        ("Let rise 1 hr. 35° for 30 min.", True),
        ("Bake at 3500°F for 40 minutes.", True),  # an invented digit
        # cooling or rising, whatever the stop before it or the case after it
        ("Bake at 350° for 1 hr; cool to 70°.", False),
        ("bake 1 hr at 350°. cool to 70° before slicing", False),
        ("Bake at 350° for 1 hr, then cool to room temp (70°).", False),
        ("Let rise in oven (85°) until doubled.", False),
        ("Dissolve 1 pkg. yeast in 1/4 c. 110°F water.", False),  # a measure's dot doesn't end the clause
        ("Roast until internal temperature reaches 145°F.", False),
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
        (ingredient("1 Tb. butter", quantity=1, food="Tb. butter", linked=False), True),
        (ingredient("2 pk yeast", quantity=2, food="pk yeast", linked=False), True),
        (ingredient("2 tbl butter", quantity=2, food="tbl butter", linked=False), True),  # a letter from "tbs"
        (ingredient("2 lbs beef", quantity=2, food="lbs beef", linked=False), True),  # a letter from "lb"
        (ingredient("2 TB butter", quantity=2, food="TB butter", linked=False), True),  # capitals, but a unit
        # short words that aren't units are part of the food, however unusual
        (ingredient("2 TV dinners", quantity=2, food="TV dinners", linked=False), False),
        (ingredient("2 new potatoes", quantity=2, food="new potatoes", linked=False), False),
        (ingredient("2 dry figs", quantity=2, food="dry figs", linked=False), False),
        (ingredient("1 wax bean", quantity=1, food="wax bean", linked=False), False),
        (ingredient("1 big onion", quantity=1, food="big onion", linked=False), False),
        (ingredient("2 ripe bananas", quantity=2, food="ripe bananas", linked=False), False),
        # a longer abbreviation of a unit, with its dot, as the parser leaves one it doesn't know in the food
        (ingredient("2 tbls. sugar", quantity=2, food="tbls. sugar", linked=False), True),
        (ingredient("1 tblsp. flour", quantity=1, food="tblsp. flour", linked=False), True),
        (ingredient("1 teasp. salt", quantity=1, food="teasp. salt", linked=False), True),
        (ingredient("2 pkgs. yeast", quantity=2, food="pkgs. yeast", linked=False), True),
        (ingredient("3 envs. gelatin", quantity=3, food="envs. gelatin", linked=False), True),
        (ingredient("2 pkges. yeast", quantity=2, food="pkges. yeast", linked=False), True),
        (ingredient("3 sqrs. chocolate", quantity=3, food="sqrs. chocolate", linked=False), True),
        (ingredient("2 Tbsps. butter", quantity=2, food="Tbsps. butter", linked=False), True),
        # a container's, which the parser leaves in the food too (commit would create "btls. ketchup")
        (ingredient("2 btls. ketchup", quantity=2, food="btls. ketchup", linked=False), True),
        (ingredient("2 pkts. yeast", quantity=2, food="pkts. yeast", linked=False), True),
        (ingredient("2 cart. eggs", quantity=2, food="cart. eggs", linked=False), True),
        (ingredient("2 ctns. buttermilk", quantity=2, food="ctns. buttermilk", linked=False), True),
        (ingredient("2 cntrs. yogurt", quantity=2, food="cntrs. yogurt", linked=False), True),
        (ingredient("2 jars. salsa", quantity=2, food="jars. salsa", linked=False), True),
        (ingredient("12 cones.", quantity=12, food="cones", linked=False), False),
        # a food read with the line's full stop, and a size, aren't
        (ingredient("2 eggs.", quantity=2, food="egg"), False),
        (ingredient("2 pears.", quantity=2, food="pears.", linked=False), False),
        (ingredient("1 lemon.", quantity=1, food="lemon."), False),
        (ingredient("2 limes.", quantity=2, food="limes.", linked=False), False),
        (ingredient("1 tomato.", quantity=1, food="tomato.", linked=False), False),
        (ingredient("2 Med. Potatoes", quantity=2, food="Potatoes", note="Med.", linked=False), False),
        (ingredient("2 Large. eggs", quantity=2, food="Large. eggs", linked=False), False),
        (ingredient("2 tablespoon. butter", quantity=2, food="tablespoon. butter", linked=False), False),
    ],
)
def test_unit_unclear(line: CardDraftIngredient, flagged: bool):
    flags = compute_flags(draft(ingredients=[line]), None, {})
    assert (CardFlagKind.unit_unclear in kinds(flags)) is flagged


def test_unit_unclear_knows_the_groups_own_units():
    """A short word that is one of the group's units ("stk" for its "stick") is a lost unit; extraction passes them"""
    line = ingredient("2 stk butter", quantity=2, food="stk butter", linked=False)
    card = draft(ingredients=[line])
    assert CardFlagKind.unit_unclear not in kinds(compute_flags(card, None, {}))

    extracted = compute_flags(card, None, {}, units=["stick", "sticks", "stk"])
    assert only(extracted, CardFlagKind.unit_unclear).params == {"token": "stk", "start": 2, "end": 5}

    # a save doesn't know the group's units: the flag stays while the line is as the parser read it
    resolutions = {only(extracted, CardFlagKind.unit_unclear).id: FlagResolution.dismissed}
    saved = compute_flags(card, None, resolutions, previous=extracted)
    assert only(saved, CardFlagKind.unit_unclear).resolution == FlagResolution.dismissed
    line.food = CardDraftRef(name="butter")
    assert CardFlagKind.unit_unclear not in kinds(compute_flags(card, None, {}, previous=saved))


@pytest.mark.parametrize(
    ("line", "word"),
    [
        (ingredient("1 c. sugar (scant)", quantity=1, unit="cup scant", food="sugar", linked=False), "scant"),
        (ingredient("1 c. heaping flour", quantity=1, unit="cup", food="heaping flour", linked=False), "heaping"),
        (ingredient("1 med. onion", quantity=1, food="med. onion", linked=False), "med."),
    ],
)
def test_a_size_word_in_a_unit_or_food_is_checked(line: CardDraftIngredient, word: str):
    """A parsed unit "cup scant" or food "heaping flour" would be created at commit: the line is checked"""
    flag = only(compute_flags(draft(ingredients=[line]), None, {}), CardFlagKind.check_parse)
    start = line.original_text.index(word)
    assert flag.params == {"value": word, "start": start, "end": start + len(word)}


BANANA_LINES = [
    ingredient("1 banana", quantity=1, food="banana"),
    ingredient("1 T. coconut oil (melted)", quantity=1, unit="tablespoon", food="coconut oil", note="melted"),
    ingredient("1/4 t. salt", quantity=0.25, unit="teaspoon", food="salt"),
    ingredient("1/2 t vanilla", quantity=0.5, unit="teaspoon", food="vanilla", linked=False),
    ingredient("1/3 C. almond flour", quantity=1 / 3, unit="cup", food="almond flour", linked=False),
    ingredient("1 egg", quantity=1, food="egg"),
    ingredient("Cinnamon to taste", food="Cinnamon", note="to taste", linked=False),
]


LOST_AT = {
    "2-3 T. milk": 0,
    "1 to 2 c. water": 0,
    "3-4 apples": 0,
    "1/2 - 3/4 c. sugar": 0,
    "2 or 3 eggs": 5,
    "1 dozen eggs": 0,
    "1 (16 oz.) can tomatoes": 3,
    "1 (8 oz) pkg cream cheese": 3,
    "2 T. butter + 1 T. oil": 14,
    "2 c. flour (or 1 1/2 c. bread flour)": 15,
    "1 c. buttermilk (or 1 c. milk + 1 T. vinegar)": 20,
    "1 c. sugar, 1 c. flour": 12,
    "1 c. sugar, 1 c. brown sugar": 12,
    "1 t. salt, 1 t. soda": 11,
}
"""Where each line's lost amount is written"""


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
        # a second ingredient run into the food, its amount in the note, or the same amount twice
        (
            ingredient(
                "2 c. flour (or 1 1/2 c. bread flour)",
                quantity=2,
                unit="cup",
                food="flour bread flour",
                note="or 1 1/2 c.",
                confidence=0.946,
                linked=False,
            ),
            "1 1/2",
        ),
        (
            ingredient(
                "1 c. buttermilk (or 1 c. milk + 1 T. vinegar)",
                quantity=1,
                unit="cup",
                food="buttermilk vinegar",
                note="or 1 c. milk + 1 T.",
                confidence=0.979,
                linked=False,
            ),
            "1",
        ),
        (
            ingredient(
                "1 c. sugar, 1 c. flour", quantity=1, unit="cup", food="sugar flour", confidence=0.858, linked=False
            ),
            "1",
        ),
        (ingredient("1 c. sugar, 1 c. brown sugar", quantity=1, unit="cup", food="sugar", confidence=0.963), "1"),
        (
            ingredient(
                "1 t. salt, 1 t. soda",
                quantity=1,
                unit="tsp",
                food="salt soda",
                note="1 t.",
                confidence=0.89,
                linked=False,
            ),
            "1",
        ),
    ],
)
def test_an_amount_the_parsed_fields_lost_is_flagged(line: CardDraftIngredient, dropped: str):
    """The parser keeps one quantity, and commit writes the parsed fields: a lost range end or size is checked"""
    flags = compute_flags(draft(ingredients=[*BANANA_LINES, line]), None, {})

    flag = only(flags, CardFlagKind.check_parse)
    start = LOST_AT[line.original_text]  # the amount that was lost, not another one written the same
    assert (flag.ref, flag.severity, flag.params) == (
        str(line.reference_id),
        CardFlagSeverity.warning,
        {"value": dropped, "start": start, "end": start + len(dropped)},
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
        ingredient("2-1/4 c. flour", quantity=2.25, unit="cup", food="flour"),  # a mixed number, not a range
        # numbers that are part of the food's name, as the parser keeps them
        ingredient("1 c. 2% milk", quantity=1, unit="cup", food="2% milk", linked=False),
        ingredient("2 c. V8 juice", quantity=2, unit="cup", food="V8 juice", linked=False),
        ingredient("1 c. 7-Up", quantity=1, unit="cup", food="7-Up", linked=False),
        ingredient("1/2 tsp. 5-spice powder", quantity=0.5, unit="tsp", food="5-spice powder", linked=False),
        ingredient('1 9" pie shell, baked', quantity=1, food='9" pie shell', note="baked", linked=False),
        ingredient("1 lb. 80/20 ground beef", quantity=1, unit="pound", food="80/20 ground beef", linked=False),
        # a substitution the note keeps, and two amounts of one food
        ingredient("1 c. butter (or 1 c. margarine)", quantity=1, unit="cup", food="butter", note="or 1 c. margarine"),
        ingredient("1 pkg. yeast (or 2 1/4 tsp.)", quantity=1, unit="package", food="yeast", note="or 2 1/4 tsps"),
        ingredient("2 T. + 1 t. sugar", quantity=2, unit="tablespoon", food="sugar", note="(1 t)"),
        ingredient("1 c. plus 2 T. flour", quantity=1, unit="cup", food="flour", note="(2 tbsps)"),
        ingredient("1 c. sugar plus 2 T. more", quantity=1, unit="cup", food="sugar", note="plus 2 tbsps more"),
        ingredient("1 c. butter or 1 c. margarine", quantity=1, unit="cup", food="butter", note="or 1 cups margarine"),
        ingredient(
            "1 c. buttermilk (or 1 c. milk + 1 T. vinegar)",
            quantity=1,
            unit="cup",
            food="buttermilk",
            note="or 1 cups milk + 1 tbsps vinegar",
        ),
        # "and" in a food's name, and a share that isn't an amount (the parser's own note looked like a kept amount)
        ingredient("1 c. half and half", quantity=1, unit="cup", food="half and half"),
        ingredient("1 lb. ground beef, 80% lean", quantity=1, unit="pound", food="ground beef", note="80% lean"),
    ],
)
def test_an_amount_the_parsed_fields_keep_is_not(line: CardDraftIngredient):
    flags = compute_flags(draft(ingredients=[*BANANA_LINES, line]), None, {})
    assert CardFlagKind.check_parse not in kinds(flags)


@pytest.mark.parametrize(
    ("line", "quantity", "unit", "food", "note", "kept", "value"),
    [
        # the parser kept the word joining the amount on its own: the note says it once ("plus, plus 2 T." before)
        ("1 c. sugar plus 2 T.", 1, "cup", "sugar", "plus", "plus 2 T.", "2"),
        ("1 c. sugar plus 2 T.", 1, "cup", "sugar", "melted, plus", "melted, plus 2 T.", "2"),
        ("1 c. sugar + 2 T.", 1, "cup", "sugar", "", "+ 2 T.", "2"),
        # a size the note leads with (taken out before parsing) isn't the amount's unit too ("lg., or 2 lg." before)
        ("3 eggs or 2 lg.", 3, None, "egg", "lg.", "lg., or 2", "2"),
        ("2 or 3 eggs", 2, None, "egg", "", "or 3 eggs", "3"),
        # a package's size joined to its unit by a hyphen: the note keeps the size, not the rest of the line again
        # ("8-oz. pkg. cream cheese" before); nothing is lost then
        ("1 8-oz. pkg. cream cheese", 1, None, "pkg. cream cheese", "", "8-oz.", None),
    ],
)
def test_what_the_note_keeps_of_a_lost_amount(
    line: str, quantity: float, unit: str | None, food: str, note: str, kept: str, value: str | None
):
    assert keep_lost_amounts(line, quantity, unit, food, note)[0] == kept

    # and the flag still finds the amount from the note parsing made
    parsed = ingredient(line, quantity=quantity, unit=unit, food=food, note=kept)
    flags = compute_flags(draft(ingredients=[parsed]), ExtractionMeta(language="English"), {})
    assert [flag.params["value"] for flag in flags if flag.kind == CardFlagKind.check_parse] == (
        [value] if value else []
    )


@pytest.mark.parametrize(
    ("line", "value", "start"),
    [
        # a second ingredient joined to the line, which the parser kept only in its note: the recipe would have no
        # ingredient for it (no shopping list item, no scaling), and the card was clean
        (ingredient("2 c. flour and 1 t. soda", quantity=2, unit="cup", food="flour", note="and 1 tsps soda"), "1", 15),
        (
            ingredient(
                "1 t. salt and 1/2 t. pepper", quantity=1, unit="teaspoon", food="salt", note="and 1/2 tsps pepper"
            ),
            "1/2",
            14,
        ),
        (
            ingredient(
                "2 cups flour and 1 teaspoon soda", quantity=2, unit="cup", food="flour", note="and 1 teaspoons soda"
            ),
            "1",
            17,
        ),
        (
            ingredient(
                "1 c. sugar plus 1 t. cinnamon", quantity=1, unit="cup", food="sugar", note="plus 1 tsps cinnamon"
            ),
            "1",
            16,
        ),
        (
            ingredient(
                "1 c. flour, 1 t. salt and 1 t. soda",
                quantity=1,
                unit="cup",
                food="flour",
                note="1 t. salt and 1 tsps soda",
            ),
            "1",
            12,
        ),
        (
            ingredient(
                "2 c. chopped apples and 1/2 c. raisins",
                quantity=2,
                unit="cup",
                food="apple",
                note="chopped, and 1/2 cups raisins",
            ),
            "1/2",
            24,
        ),
        # or split off by the parser, the note keeping it as the line has it (`keep_alternatives`)
        (
            ingredient(
                "2 c. flour and 1 t. baking powder", quantity=2, unit="cup", food="flour", note="and 1 t. baking powder"
            ),
            "1",
            15,
        ),
    ],
)
def test_a_second_ingredient_the_note_keeps_is_flagged(line: CardDraftIngredient, value: str, start: int):
    """Its amount is in the note, so nothing counts as lost; its food isn't the line's, so it's checked all the same"""
    flags = compute_flags(draft(ingredients=[*BANANA_LINES, line]), None, {})

    flag = only(flags, CardFlagKind.check_parse)
    assert (flag.ref, flag.params) == (
        str(line.reference_id),
        {"value": value, "start": start, "end": start + len(value)},
    )
    assert not is_clean(flags)


@pytest.mark.parametrize(
    ("line", "note", "names", "fields", "kept"),
    [
        # the parser's substitutions, as the line writes them, from the word that joins them
        ("1/2 c. butter or margarine, softened", "softened", [["margarine"]], ["butter"], "softened, or margarine"),
        ("1 c. chopped pecans or walnuts", "chopped", [["walnut", "walnuts"]], ["pecan"], "chopped, or walnuts"),
        ("1 c. chicken or beef broth", "", [["beef broth"]], ["chicken broth"], "or beef broth"),
        ("salt and pepper to taste", "to taste", [["pepper"]], ["salt"], "to taste, and pepper"),
        ("1/2 c. butter/margarine", "", [["margarine"]], ["butter"], "or margarine"),
        # in place of the parser's own "and 1 tsps", which lost the food
        ("2 c. flour and 1 t. baking powder", "and 1 tsps", [["baking powder"]], ["flour"], "and 1 t. baking powder"),
        # a food linked by another name: the line's words ("turkey" of "ground turkey", "oleo" for "margarine")
        ("1 lb. ground beef or turkey", "", [["ground turkey"]], ["ground beef"], "or turkey"),
        ("1 c. butter or oleo", "", [["margarine"]], ["butter"], "or oleo"),
        # a card in another language: the short word before it
        ("1 Tasse Butter oder Margarine", "", [["Margarine"]], ["Butter", "Tasse"], "oder Margarine"),
        # an amount and a food: the amount is the note's already (`keep_lost_amounts`), the food isn't
        ("1 c. (8 oz.) sour cream or yogurt", "", [["8 ounce yogurt"]], ["sour cream", "(8 oz.)"], "or yogurt"),
    ],
)
def test_what_the_note_keeps_of_an_alternative_the_parser_split_off(
    line: str, note: str, names: list[list[str]], fields: list[str], kept: str
):
    assert keep_alternatives(line, note, names, [*fields, note]) == (kept, True)


@pytest.mark.parametrize(
    ("line", "names", "fields"),
    [
        # only an amount, which the note keeps as written ("(8 oz.)"), or a food the note or the food holds already
        ("1 pkg. (8 oz.) cream cheese", [["8 ounce"]], ["cream cheese", "(8 oz.)"]),
        ("1 pkg. (1/4 oz.) yeast", [["¹/₄ ounce"]], ["yeast", "(1/4 oz.)"]),
        ("1 c. sugar, 1 c. brown sugar", [["1 cup brown sugar"]], ["sugar", "1 c. brown sugar"]),
        ("1 c. butter or margarine", [["margarine"]], ["butter", "or margarine"]),
    ],
)
def test_an_alternative_the_fields_hold_adds_nothing(line: str, names: list[list[str]], fields: list[str]):
    assert keep_alternatives(line, "x", names, fields) == ("x", False)


def test_an_alternative_the_parser_split_off_is_checked():
    """
    The parser moved "margarine" out of "1/2 c. butter or margarine" (its substitutions, which nothing wrote): the note
    keeps it, and while the line is as parsed it's checked, naming it, so the reviewer sees the split
    """
    line = ingredient(
        "1/2 c. butter or margarine, softened", quantity=0.5, unit="cup", food="butter", note="softened, or margarine"
    )
    line.extracted_hash = ingredient_hash(line, split=True)
    assert is_unedited(line)

    flags = compute_flags(draft(ingredients=[*BANANA_LINES, line]), None, {})

    flag = only(flags, CardFlagKind.check_parse)
    assert (flag.ref, flag.params) == (str(line.reference_id), {"alternative": "margarine", "start": 17, "end": 26})
    assert not is_clean(flags)
    # with what else the line lost
    joined = ingredient(
        "2 c. flour and 1 t. baking powder", quantity=2, unit="cup", food="flour", note="and 1 t. baking powder"
    )
    joined.extracted_hash = ingredient_hash(joined, split=True)
    flag = only(compute_flags(draft(ingredients=[joined]), None, {}), CardFlagKind.check_parse)
    assert flag.params == {"value": "1", "start": 15, "end": 16, "alternative": "baking powder"}

    # the same note when the parser kept the alternative itself isn't a split; an edited line says what was typed
    kept = ingredient("1/2 c. butter or margarine", quantity=0.5, unit="cup", food="butter", note="or margarine")
    line.note = "softened"
    for item in (kept, line):
        assert CardFlagKind.check_parse not in kinds(compute_flags(draft(ingredients=[item]), None, {}))


MEASURES = [
    ("1 pkg. (8 oz.) cream cheese", 1, "package", "cream cheese", "(8 oz.)"),
    ("1 can (10 3/4 oz.) cream of mushroom soup", 1, "can", "cream of mushroom soup", "(10 3/4 oz.)"),
    ("1/2 c. (1 stick) butter", 0.5, "cup", "butter", "(1 stick)"),
    ("1 c. (8 oz.) sour cream", 1, "cup", "sour cream", "(8 oz.)"),
    ("2 c. (16 oz.) cottage cheese", 2, "cup", "cottage cheese", "(16 oz.)"),
    ("1 lb. (2 c.) butter", 1, "pound", "butter", "(2 c.)"),
    ("1 T. (1/2 oz.) gelatin", 1, "tbsp", "gelatin", "(1/2 oz.)"),
    ("1 large can (28 oz.) tomatoes", 1, "can", "tomatoes", "large, (28 oz.)"),
]


@pytest.mark.parametrize(("line", "quantity", "unit", "food", "note"), MEASURES)
def test_a_measure_in_parentheses_after_the_unit_is_kept_and_not_checked(
    line: str, quantity: float, unit: str, food: str, note: str
):
    """
    The same amount in another measure, which the note keeps as written: the fields read the line right, as for a
    package size before the unit ("1 (8 oz.) pkg.", taken out before parsing), so one of the commonest printed card
    lines doesn't need a tap
    """
    base = "large" if line.startswith("1 large") else ""  # what `prepare_line` took out leads the note
    assert keep_lost_amounts(line, quantity, unit, food, base)[0] == note

    parsed = ingredient(line, quantity=quantity, unit=unit, food=food, note=note)
    assert CardFlagKind.check_parse not in kinds(
        compute_flags(draft(ingredients=[parsed]), ExtractionMeta(language="English"), {})
    )


@pytest.mark.parametrize(
    ("line", "quantity", "unit", "food", "note", "value"),
    [
        # an alternative, a range or the line's own unit in the parentheses: a second amount, which needs a look
        ("1 c. (or 2) eggs", 1, "cup", "egg", "(or 2)", "2"),
        ("1 can (8-10 oz.) beans", 1, "can", "beans", "(8-10 oz.)", "8-10"),
        ("2 c. (3 c.) flour", 2, "cup", "flour", "(3 c.)", "3"),
        # not right after the unit, or after a unit the parser read otherwise
        ("1 c. sugar (8 oz.)", 1, "cup", "sugar", "(8 oz.)", "8"),
        ("1 pkg. (8 oz.) cream cheese", 1, "ounce", "cream cheese", "(8 oz.)", "8"),
    ],
)
def test_any_other_amount_in_parentheses_is_checked(
    line: str, quantity: float, unit: str, food: str, note: str, value: str
):
    assert keep_lost_amounts(line, quantity, unit, food, "")[0] == note

    parsed = ingredient(line, quantity=quantity, unit=unit, food=food, note=note)
    flag = only(
        compute_flags(draft(ingredients=[parsed]), ExtractionMeta(language="English"), {}), CardFlagKind.check_parse
    )
    assert flag.params["value"] == value


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


# ==========================================
# Links that aren't an exact name match


def _names(line: CardDraftIngredient, *, food: list[str] | None = None, unit: list[str] | None = None) -> dict:
    """`compute_flags`' `linked` for a line: the names its linked food and unit go by in the group"""
    linked = {}
    if food is not None and line.food and line.food.id:
        linked[line.food.id] = food
    if unit is not None and line.unit and line.unit.id:
        linked[line.unit.id] = unit
    return linked


TABLESPOON = ["tablespoon", "tablespoons", "tbsp"]
CUP = ["cup", "cups", "c"]


@pytest.mark.parametrize(
    ("line", "food", "unit"),
    [
        (ingredient("2 red onions", quantity=2, food="red onion"), ["red onion", "red onions"], None),
        (ingredient("2 red onions", quantity=2, food="red onion"), ["red onion"], None),  # no plural in the group
        (ingredient("3 scallions, sliced", quantity=3, food="green onion"), ["green onion", "scallion"], None),
        (ingredient("1 T. sugar", quantity=1, unit="tablespoon", food="sugar"), ["sugar"], TABLESPOON),
        (ingredient("1/3 C. almond flour", quantity=1 / 3, unit="cup", food="almond flour"), ["almond flour"], CUP),
        (ingredient("1 lb. ground beef", quantity=1, unit="pound", food="ground beef"), None, ["pound", "lb"]),
        (ingredient("1 c. confectioners' sugar", quantity=1, unit="cup", food="confectioners sugar"), [], CUP),
        (ingredient("2 c. all-purpose flour", quantity=2, unit="cup", food="all purpose flour"), [], CUP),
        (ingredient("1 Jalapeño, minced", quantity=1, food="jalapeno"), ["jalapeno"], None),
        (ingredient("2 c. cherries", quantity=2, unit="cup", food="cherry"), ["cherry"], CUP),
        (ingredient("2 bay leaves", quantity=2, food="bay leaf"), ["bay leaf"], None),
        (ingredient("1 doz. eggs", quantity=1, unit="dozen", food="egg"), ["egg"], ["dozen"]),
        # a size word the parser took out doesn't hide the food's name
        (ingredient("1 med onion", quantity=1, food="onion", note="med"), ["onion"], None),
        (ingredient("1 c. sugar (scant)", quantity=1, unit="cup", food="sugar", note="scant"), ["sugar"], CUP),
        # a group's unit with only a name (as commit creates them): the card's abbreviation is the unit's own
        (ingredient("1 tsp. salt", quantity=1, unit="teaspoon", food="salt"), None, ["teaspoon", "teaspoons"]),
        (ingredient("1/4 t. salt", quantity=0.25, unit="teaspoon", food="salt"), None, ["teaspoon"]),
        (ingredient("1 T. coconut oil (melted)", quantity=1, unit="tablespoon", food="oil"), None, ["tablespoon"]),
        (ingredient("2 Tbsp. butter", quantity=2, unit="tablespoon", food="butter"), None, ["tablespoon"]),
        (ingredient("2 tbs. sugar", quantity=2, unit="tablespoon", food="sugar"), None, ["tablespoon", "tbsp"]),
        (ingredient("1 lb. ground beef", quantity=1, unit="pound", food="ground beef"), None, ["pound"]),
        (ingredient("2 lbs. potatoes", quantity=2, unit="pound", food="potato"), None, ["pound", "pounds"]),
        (ingredient("8 oz. cheese", quantity=8, unit="ounce", food="cheese"), None, ["ounce"]),
        (ingredient("1 qt. milk", quantity=1, unit="quart", food="milk"), None, ["quart"]),
        (
            ingredient("2 fl. oz. lemon juice", quantity=2, unit="fluid ounce", food="lemon juice"),
            None,
            ["fluid ounce"],
        ),
        (ingredient("250 g flour", quantity=250, unit="gram", food="flour"), None, ["gram"]),
        # and the card's "pkg." is a group's "pack"
        (ingredient("1 pkg. dry yeast", quantity=1, unit="pack", food="dry yeast"), None, ["pack", "packs"]),
    ],
)
def test_an_exact_or_alias_link_is_not_flagged(
    line: CardDraftIngredient, food: list[str] | None, unit: list[str] | None
):
    linked = _names(line, food=food, unit=unit)
    assert linked
    flags = compute_flags(draft(ingredients=[line]), ExtractionMeta(language="English"), {}, linked=linked)
    assert CardFlagKind.linked_fuzzy not in kinds(flags)


def test_a_food_or_unit_linked_by_a_near_miss_name_is_flagged():
    onions = ingredient("2 rd onions", quantity=2, food="red onion")
    ref = str(onions.reference_id)

    flags = compute_flags(
        draft(ingredients=[onions]), None, {}, linked=_names(onions, food=["red onion", "red onions"])
    )

    flag = only(flags, CardFlagKind.linked_fuzzy)
    assert (flag.id, flag.field, flag.ref) == (f"linked_fuzzy:ingredients:{ref}", "ingredients", ref)
    assert (flag.severity, flag.source) == (CardFlagSeverity.warning, CardFlagSource.parser)
    # the linked name, and where the words it was matched from are
    assert flag.params == {"name": "red onion", "kind": "food", "start": 2, "end": 11}
    assert count_unresolved(flags) == (0, 1)

    # a food and a unit on one line: two flags, the unit's with an id of its own
    both = ingredient("2 cps rd onions", quantity=2, unit="cup", food="red onion")
    flags = compute_flags(draft(ingredients=[both]), None, {}, linked=_names(both, food=["red onion"], unit=CUP))
    by_kind = {flag.params["kind"]: flag for flag in flags if flag.kind == CardFlagKind.linked_fuzzy}
    ref = str(both.reference_id)
    assert by_kind["food"].id == f"linked_fuzzy:ingredients:{ref}"
    assert by_kind["unit"].id == f"linked_fuzzy:ingredients:{ref}#unit"
    assert by_kind["unit"].ref == ref and by_kind["unit"].params["name"] == "cup"


def test_a_unit_linked_by_a_near_miss_is_flagged_whatever_its_spellings():
    """ "sq." read as "square" and matched to the group's quart is no spelling of a quart's"""
    line = ingredient("2 sq. chocolate", quantity=2, unit="quart", food="chocolate")
    linked = _names(line, food=["chocolate"], unit=["quart", "quarts", "qt"])

    flags = compute_flags(draft(ingredients=[line]), ExtractionMeta(language="English"), {}, linked=linked)

    assert [flag.params["name"] for flag in flags if flag.kind == CardFlagKind.linked_fuzzy] == ["quart"]


def test_a_fuzzy_link_follows_the_line():
    """Computed on every save from the linked names: dismissable, gone once the reviewer picks another food"""
    onions = ingredient("2 rd onions", quantity=2, food="red onion")
    assert onions.food is not None and onions.food.id is not None
    card = draft(ingredients=[onions])
    linked = _names(onions, food=["red onion"])
    extracted = compute_flags(card, None, {}, linked=linked)
    flag = only(extracted, CardFlagKind.linked_fuzzy)

    # "Looks right"
    dismissed = compute_flags(card, None, {flag.id: FlagResolution.dismissed}, previous=extracted, linked=linked)
    assert only(dismissed, CardFlagKind.linked_fuzzy).resolution == FlagResolution.dismissed
    assert is_clean(dismissed)

    # a save without the linked names keeps it while the line is as parsed
    assert only(compute_flags(card, None, {}, previous=extracted), CardFlagKind.linked_fuzzy) == flag
    # a food that isn't the group's (any more) isn't judged
    assert CardFlagKind.linked_fuzzy not in kinds(compute_flags(card, None, {}, linked={}))

    # the reviewer picks another food: the line is theirs now
    onions.food = CardDraftRef(id=uuid4(), name="yellow onion")
    for flags in (
        compute_flags(card, None, {}, previous=extracted),
        compute_flags(card, None, {}, previous=extracted, linked={onions.food.id: ["yellow onion"]}),
    ):
        assert CardFlagKind.linked_fuzzy not in kinds(flags)

    # a line that wasn't parsed has no link of the parser's to check
    text = ingredient("2 rd onions", food="red onion", confidence=None)
    assert text.food is not None and text.food.id is not None
    flags = compute_flags(draft(ingredients=[text]), None, {}, linked={text.food.id: ["red onion"]})
    assert CardFlagKind.linked_fuzzy not in kinds(flags)


def test_a_fuzzy_link_on_a_card_in_another_language_is_judged_as_written():
    """Shorthand is written out only on English cards; elsewhere the names are looked for in the line as it is"""
    line = ingredient("1 c. sucre", quantity=1, unit="cup", food="sugar")
    linked = _names(line, food=["sugar"], unit=CUP)
    flags = compute_flags(draft(ingredients=[line]), ExtractionMeta(language="fr"), {}, linked=linked)
    assert [flag.params["kind"] for flag in flags if flag.kind == CardFlagKind.linked_fuzzy] == ["food"]


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
    assert only(carried, CardFlagKind.not_on_card).params == {"value": "2", "start": 14, "end": 15}
    # where the number is now, after an edit before it
    card.steps[1].text = "Then microwave for 2 minutes."
    assert only(compute_flags(card, extraction, {}, previous=extracted), CardFlagKind.not_on_card).params == {
        "value": "2",
        "start": 19,
        "end": 20,
    }

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
    note = str(card.notes[1].id)
    flags = compute_flags(card, None, {})
    assert [(flag.id, flag.field, flag.ref) for flag in flags] == [
        ("illegible:description:", "description", None),
        ("blank:totalTime:", "totalTime", None),
        (f"illegible:notes:{note}", "notes", note),  # the note's id, like a step's
        ("illegible:attribution:", "attribution", None),
    ]


def _note_flag(flags: list[CardFlag], note: CardDraftNote) -> CardFlag:
    (flag,) = [flag for flag in flags if flag.field == "notes" and flag.ref == str(note.id)]
    return flag


def test_a_kept_note_flag_stays_with_its_note():
    """A note's resolution is stored by its id: it follows the note when it's edited or moved, never another note"""
    bake, serve = CardDraftNote(text="Bake [blank] min"), CardDraftNote(text="Serve with [blank]")
    card = draft(notes=[bake, serve])
    first = _note_flag(compute_flags(card, None, {}), bake)
    assert first.id == f"blank:notes:{bake.id}"
    resolutions = {first.id: FlagResolution.kept}

    flags = compute_flags(card, None, resolutions)
    assert _note_flag(flags, bake).resolution == FlagResolution.kept
    assert count_unresolved(flags) == (1, 0)  # the second note's blank
    assert compute_flags(card, None, resolutions) == flags  # stable from save to save

    # the reviewer edits the kept note's text around its blank: still kept
    bake.text = "Bake [blank] minutes, until golden"
    assert _note_flag(compute_flags(card, None, resolutions), bake).resolution == FlagResolution.kept

    # the notes are reordered: each keeps its own
    card.notes = [serve, bake]
    flags = compute_flags(card, None, resolutions)
    assert (_note_flag(flags, bake).resolution, _note_flag(flags, serve).resolution) == (FlagResolution.kept, None)
    assert count_unresolved(flags) == (1, 0)

    # another note is deleted: the kept one isn't reopened
    card.notes = [bake]
    flags = compute_flags(card, None, resolutions)
    assert _note_flag(flags, bake).resolution == FlagResolution.kept
    assert count_unresolved(flags) == (0, 0)

    # a new note saying what the deleted one said is a new note
    card.notes = [CardDraftNote(text="Serve with [blank]"), bake]
    assert count_unresolved(compute_flags(card, None, resolutions)) == (1, 0)


def test_a_note_keeps_its_unsure_flag_and_resolution_when_notes_change():
    """A note's reading flags and their resolutions stay with its id, wherever it moves and whatever is deleted"""
    double, freezes = CardDraftNote(text="Double for a 9x13 pan"), CardDraftNote(text="Freezes for 3 months")
    card = draft(notes=[double, freezes])
    extraction = ExtractionMeta(unsure=[ExtractionUnsure(text="3 months", alternatives=["8 months"], reason="faded")])
    extracted = compute_flags(card, extraction, {})
    unsure = only(extracted, CardFlagKind.unsure)
    assert (unsure.field, unsure.ref, unsure.alternatives) == ("notes", str(freezes.id), ["8 months"])
    assert only(compute_flags(card, extraction, {}, previous=extracted), CardFlagKind.unsure) == unsure

    # "Looks right"; then the reviewer deletes the first note: the second moves up, still dismissed
    resolutions = {unsure.id: FlagResolution.dismissed}
    del card.notes[0]
    saved = compute_flags(card, extraction, resolutions, previous=extracted)
    moved = only(saved, CardFlagKind.unsure)
    assert (moved.id, moved.ref, moved.resolution) == (unsure.id, str(freezes.id), FlagResolution.dismissed)
    assert count_unresolved(saved) == (0, 0)
    assert compute_flags(card, extraction, resolutions, previous=saved) == saved  # and on the next save

    # moved back below a new note, then its text edited around the words: still the same flag, still dismissed
    card.notes = [CardDraftNote(text="Serve warm"), freezes]
    freezes.text = "Freezes well for 3 months"
    edited = compute_flags(card, extraction, resolutions, previous=saved)
    assert (only(edited, CardFlagKind.unsure).id, only(edited, CardFlagKind.unsure).resolution) == (
        unsure.id,
        FlagResolution.dismissed,
    )

    # the uncertain words gone: the reading flag no longer holds
    freezes.text = "Freezes well"
    assert CardFlagKind.unsure not in kinds(compute_flags(card, extraction, resolutions, previous=edited))


def test_note_flags_stored_before_notes_had_ids_still_apply():
    """
    A job's flags stored before notes had ids are keyed `"<kind>:notes:<position>#<digest>"`: on the first save their
    resolutions apply to the note still at that position saying that, and their reading flags find the note saying it
    """
    bake, freezes = CardDraftNote(text="Bake [blank] min"), CardDraftNote(text="Freezes for 3 months")
    card = draft(notes=[bake, freezes])
    extraction = ExtractionMeta(unsure=[ExtractionUnsure(text="3 months", alternatives=["8 months"], reason="faded")])

    def legacy(flag: CardFlag, index: int, note: CardDraftNote) -> CardFlag:
        digest = hashlib.sha256(json.dumps([note.title, note.text]).encode()).hexdigest()[:8]
        ref = f"{index}#{digest}"
        return flag.model_copy(update={"id": flag_id(flag.kind, "notes", ref), "ref": str(index)})

    current = compute_flags(card, extraction, {})
    blank = legacy(_note_flag(current, bake), 0, bake)
    unsure = legacy(only(current, CardFlagKind.unsure), 1, freezes)
    stored = [blank.model_copy(update={"resolution": FlagResolution.kept}), unsure]
    resolutions = {blank.id: FlagResolution.kept}

    # the first save after the upgrade: keyed to the notes' ids, the blank still kept, the unsure flag still raised
    saved = compute_flags(card, extraction, resolutions, previous=stored)
    assert (_note_flag(saved, bake).id, _note_flag(saved, bake).resolution) == (
        f"blank:notes:{bake.id}",
        FlagResolution.kept,
    )
    assert only(saved, CardFlagKind.unsure).id == f"unsure:notes:{freezes.id}"

    # a reading flag finds its note wherever it moved, and an old resolution never lands on another note
    card.notes = [freezes, CardDraftNote(text="Bake [blank] min")]
    moved = compute_flags(card, extraction, resolutions, previous=stored)
    assert only(moved, CardFlagKind.unsure).ref == str(freezes.id)
    assert count_unresolved(moved) == (1, 1)


def test_a_steps_own_list_number_is_never_not_on_card():
    """The build step or a re-read may leave "3." on a step: it's the step's number, not an amount"""
    transcription = "1 banana\nMash and mix.\nBake 20 minutes."
    card = draft(steps=[CardDraftStep(text="Mash and mix."), CardDraftStep(text="3. Bake 20 minutes.")])

    flags = compute_flags(card, ExtractionMeta(language="English"), {}, transcription=transcription)

    assert CardFlagKind.not_on_card not in kinds(flags)
    # a number after it still counts
    card.steps[1].text = "3. Bake 25 minutes."
    flag = only(compute_flags(card, ExtractionMeta(), {}, transcription=transcription), CardFlagKind.not_on_card)
    assert flag.params == {"value": "25", "start": 8, "end": 10}


def test_a_flag_points_at_the_occurrence_it_means():
    """Two "2"s in a step, or two "1"s on a line: the flag's position is the one its rule matched"""
    step = CardDraftStep(text="Add 1/2 c. milk. Microwave 2 minutes.")
    card = draft(steps=[step])
    second = ["Add 1/2 c. milk. Microwave [blank] minutes."]

    flags = compute_flags(card, ExtractionMeta(cross_read_lines=second), {})

    blank = only(flags, CardFlagKind.blank)
    assert blank.params == {"value": "2", "start": 27, "end": 28}
    assert step.text[27:28] == "2" and step.text.index("2") != 27  # the "2" of "1/2" comes first

    line = ingredient("1 c. sugar, 1 c. flour", quantity=1, unit="cup", food="sugar flour", linked=False)
    flag = only(compute_flags(draft(ingredients=[line]), None, {}), CardFlagKind.check_parse)
    assert flag.params["value"] == "1" and (flag.params["start"], flag.params["end"]) == (12, 13)

    unsure = ExtractionUnsure(text="350", alternatives=["380"])
    card = draft(steps=[CardDraftStep(text="Bake 35 minutes at 350.")])
    flag = only(compute_flags(card, ExtractionMeta(unsure=[unsure]), {}), CardFlagKind.unsure)
    assert (flag.params["start"], flag.params["end"]) == (19, 22)

    card = draft(steps=[CardDraftStep(text="Mix [blank] cups, then [illegible].")])
    flags = compute_flags(card, None, {})
    assert only(flags, CardFlagKind.blank).params == {"start": 4, "end": 11}
    assert only(flags, CardFlagKind.illegible).params == {"start": 23, "end": 34}


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

    assert only(flags, CardFlagKind.not_on_card).params == {"value": "2", "start": 35, "end": 36}
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
    assert (blank.source, blank.field, blank.params) == (
        CardFlagSource.cross_read,
        "steps",
        {"value": "2", "start": 35, "end": 36},
    )
    # the ingredients agree with their own lines: "C." and "c." are both cups, and "Add eggs" is a step
    assert CardFlagKind.read_disagreement not in kinds(flags)


# ==========================================
# The OCR check of a printed card's numbers

PRINTED_STEP = "Bake at 375° for 1 hour, then cool 10 minutes."


def test_the_ocr_check_flags_a_clearly_different_number_only():
    card = draft(steps=[CardDraftStep(text=PRINTED_STEP)])
    read = ["Bake at 350° for 1 hour, then cool 10 minutes."]

    flags = compute_flags(card, ExtractionMeta(), {}, ocr_lines=read)

    flag = only(flags, CardFlagKind.read_disagreement)
    assert (flag.source, flag.severity, flag.params["value"], flag.params["read"]) == (
        CardFlagSource.ocr,
        CardFlagSeverity.warning,
        "375",
        "350",
    )
    assert flag.alternatives == ["Bake at 350° for 1 hour, then cool 10 minutes."]

    # numbers Tesseract may have misread, a number it missed, or fractions: no flag
    for unclear in (
        "Bake at 3S0° for 1 hour, then cool 10 minutes.",
        "Bake at 350° for 1 hour, then cool minutes.",
        "Bake at 375° for l hour, then cool 10 minutes.",
    ):
        assert CardFlagKind.read_disagreement not in kinds(
            compute_flags(card, ExtractionMeta(), {}, ocr_lines=[unclear])
        )
    half = draft(steps=[CardDraftStep(text="Add 1/2 c. milk.")])
    assert kinds(compute_flags(half, ExtractionMeta(), {}, ocr_lines=["Add 1/3 c. milk."])) == set()


ITALIC_CARD = [
    "Oatmeal Raisin Cookies\n\nFrom Grandma Jo\n\n- 1 C. butter\n\n-____C.. brown sugar\n- 2 eggs\n\n"
    "- 7 ¢. vanilla\n\n- 7 1/2 C. flour\n\n- 1 t. baking soda\n-3C. oats\n\n- 71 C. raisins",
    "Directions\n\n1. Cream butter and sugar, beat in eggs\nand vanilla.\n\n"
    "2. Stir in flour, soda, oats and\nralsins.\n\n3. Bake at 350 °F for 10 minutes.",
]
"""Tesseract's reading of a printed card set in an italic font (a live run's): its "1"s read as "7" and "71" """


def test_the_ocr_check_skips_digits_tesseract_confuses():
    """
    An italic "1" Tesseract reads as "7" (or "71") was flagged on 3 of 10 printed cards, offering "Use '7 onion'":
    digits it confuses aren't a disagreement. A number it reads otherwise still is.
    """
    lines = ["1 C. butter", "[blank] C. brown sugar", "2 eggs", "1 t. vanilla", "1 1/2 C. flour", "1 t. baking soda"]
    lines += ["3 C. oats", "1 C. raisins"]
    steps = ["Cream butter and sugar, beat in eggs and vanilla.", "Stir in flour, soda, oats and raisins."]
    steps += ["Bake at 350°F for 10 minutes."]
    card = draft(
        name="Oatmeal Raisin Cookies",
        ingredients=[ingredient(line) for line in lines],
        steps=[CardDraftStep(text=step) for step in steps],
    )
    transcription = "\n".join(["# Oatmeal Raisin Cookies", *lines, *steps])
    pages = [PageOCR(text=text, confidence=88.0) for text in ITALIC_CARD]
    ocr_lines = ocr_check_lines(pages, IngestReadPath.image, transcription)
    assert ocr_lines is not None

    assert CardFlagKind.read_disagreement not in kinds(compute_flags(card, ExtractionMeta(), {}, ocr_lines=ocr_lines))
    for read in ("- 7 onion", "- 7_onion", "- 7. onion"):
        onion = draft(ingredients=[ingredient("1 onion")])
        assert CardFlagKind.read_disagreement not in kinds(compute_flags(onion, ExtractionMeta(), {}, ocr_lines=[read]))

    card.steps[2].text = "Bake at 375°F for 10 minutes."  # the image reader's own mistake
    flag = only(compute_flags(card, ExtractionMeta(), {}, ocr_lines=ocr_lines), CardFlagKind.read_disagreement)
    assert (flag.source, flag.params["value"], flag.params["read"]) == (CardFlagSource.ocr, "375", "350")


@pytest.mark.parametrize(
    ("mine", "theirs", "confused"),
    [
        ("1", "7", True),
        ("1", "71", True),
        ("1", "17", True),
        ("350", "360", True),
        ("30", "38", True),
        ("375", "350", False),
        ("350", "380", False),
        ("2", "12", False),  # a digit more that isn't a stroke beside a "1"
        ("71", "1", False),  # the draft's digit more: Tesseract didn't add it
        ("1", "2", False),
    ],
)
def test_the_digits_tesseract_confuses(mine: str, theirs: str, confused: bool):
    assert card_flags._misread_digits(mine, theirs) is confused


def test_the_ocr_checks_flags_stay_while_they_hold_on_a_save():
    """A save has no Tesseract text: the flag stays while the number is still there, then goes"""
    card = draft(steps=[CardDraftStep(text=PRINTED_STEP)])
    extracted = compute_flags(card, ExtractionMeta(), {}, ocr_lines=["Bake at 350° for 1 hour, then cool 10 minutes."])
    flag = only(extracted, CardFlagKind.read_disagreement)

    saved = compute_flags(card, ExtractionMeta(), {flag.id: FlagResolution.dismissed}, previous=extracted)
    assert only(saved, CardFlagKind.read_disagreement).resolution == FlagResolution.dismissed

    card.steps[0].text = "Preheat. " + PRINTED_STEP  # moved along: its position follows
    moved = only(compute_flags(card, ExtractionMeta(), {}, previous=extracted), CardFlagKind.read_disagreement)
    assert (moved.params["start"], moved.params["end"]) == (17, 20)

    card.steps[0].text = PRINTED_STEP.replace("375", "350")  # the reviewer took Tesseract's reading
    assert CardFlagKind.read_disagreement not in kinds(compute_flags(card, ExtractionMeta(), {}, previous=extracted))


def test_the_ocr_check_runs_only_on_a_clear_printed_reading():
    transcription = "# Pound Cake\n- 2 c. flour\nBake at 375° for 1 hour."
    printed = PageOCR(text="Pound Cake\n2 c. flour\nBake at 350° for 1 hour.", confidence=88.0)
    lines = ["Pound Cake", "2 c. flour", "Bake at 350° for 1 hour."]

    assert ocr_check_lines([printed], IngestReadPath.image, transcription) == lines
    assert ocr_check_lines([printed, printed], IngestReadPath.image, transcription) == lines + lines
    # read with OCR already, handwriting on any page, a page Tesseract didn't read, or a corner of the card
    assert ocr_check_lines([printed], IngestReadPath.ocr, transcription) is None
    assert (
        ocr_check_lines([printed, printed.model_copy(update={"confidence": 60.0})], IngestReadPath.image, transcription)
        is None
    )
    assert ocr_check_lines([printed, None], IngestReadPath.image, transcription) is None
    assert ocr_check_lines([PageOCR(text="Pound Cake", confidence=95.0)], IngestReadPath.image, transcription) is None


SPACES = " " * 3000


@pytest.mark.parametrize(
    "line",
    [
        f"2 c. sugar +{SPACES}flour",
        f"2 c. sugar,{SPACES},{SPACES}flour",
        f"1 -{SPACES};{SPACES}sugar",
        f"1{SPACES}/2 c. sugar",
        f"350{SPACES}degrees",
        f"1 c. sugar (or{SPACES}2 T.{SPACES}honey",
    ],
)
def test_long_runs_of_spaces_take_linear_time(line: str, monkeypatch: pytest.MonkeyPatch):
    """No pattern tries a run of spaces every way: a joiner before a few thousand spaces took seconds to minutes"""
    import time

    monkeypatch.setattr(card_flags, "MAX_ANALYSED_LINE", 10**9)  # the lost amounts' patterns too
    started = time.perf_counter()
    keep_lost_amounts(line, 2.0, "cup", "sugar", "")
    draft = CardDraft(
        name="Sugar",
        ingredients=[CardDraftIngredient(original_text=line, quantity=2, unit=CardDraftRef(name="cup"))],
        steps=[CardDraftStep(text=line)],
    )
    compute_flags(draft, None, {}, transcription=line, units=["cup"], ocr_lines=[line])
    assert time.perf_counter() - started < 0.5, line.split()[:3]


@pytest.mark.parametrize(
    "line",
    [
        f"{'9' * 400} cups flour",
        f"1 c. flour ({'9' * 400} oz.)",
        f"1 {'9' * 400}/2 c. sugar",
    ],
)
def test_a_number_too_large_for_a_float_is_no_quantity(line: str):
    """300 digits read off a card: the amount is kept in the note, nothing raises OverflowError"""
    note, lost = keep_lost_amounts(line, 1.0, "cup", "flour", "")
    assert lost
    assert "9" * 400 in note or any("9" * 400 in amount.value for amount in lost)


QUADRATIC = {
    "glyphs": "1" + " ½" * 4000,
    "joiners": "1 c. sugar" + " or 2" * 4000,
    "amounts": "1 c. sugar" + ", 1 c. flour" * 4000,
    "note parts": "1 c. sugar" + " and x" * 1000,
}
"""
Lines whose lost amounts took quadratic time, seconds each: every joiner was searched from the line's start, "½" read
to its end, and each of a long note's parts read again
"""


@pytest.mark.parametrize("line", QUADRATIC.values(), ids=list(QUADRATIC))
def test_lost_amounts_are_found_in_linear_time(line: str, monkeypatch: pytest.MonkeyPatch):
    """Each amount's joiner, parentheses and words are read once, and only a note's last parts can be what was kept"""
    import gc
    import time

    monkeypatch.setattr(card_flags, "MAX_ANALYSED_LINE", 10**9)
    parsed = ingredient(line, quantity=1, unit="cup", food="sugar", note=", ".join(["or x"] * 4000))
    gc.collect()
    gc.disable()  # a collection mid-run isn't what's measured
    try:
        started = time.perf_counter()
        keep_lost_amounts(line, 1.0, "cup", "sugar", "")
        compute_flags(draft(ingredients=[parsed]), ExtractionMeta(language="English"), {})
        elapsed = time.perf_counter() - started
    finally:
        gc.enable()
    assert elapsed < 1.0  # well under 0.1 s; it was 3 to 7 s


def test_a_line_longer_than_any_cards_is_not_searched_for_lost_amounts():
    """A line of a misread page: its text is kept, and the search for what its fields lost has a bound"""
    line = "1 c. sugar" + " or 2" * MAX_ANALYSED_LINE
    assert keep_lost_amounts(line, 1.0, "cup", "sugar", "melted") == ("melted", [])
    parsed = ingredient(line, quantity=1, unit="cup", food="sugar", confidence=0.5)
    flag = only(compute_flags(draft(ingredients=[parsed]), None, {}), CardFlagKind.check_parse)
    assert flag.params == {"confidence": 50}
