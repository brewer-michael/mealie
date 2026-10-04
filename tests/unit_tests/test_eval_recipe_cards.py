import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Generator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx2
import openai
import pytest

from mealie.lang import get_locale_provider
from mealie.schema.group.ai_providers import (
    AIProviderCreate,
    AIProviderOut,
    AIProviderProtocol,
    AIProviderSettingsUpdate,
    AIProviderSlot,
)
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.schema.openai.general import OpenAIText
from mealie.schema.openai.recipe import OpenAIRecipe, OpenAIRecipeIngredient, OpenAIRecipeInstruction
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_ingredient import RecipeIngredient
from mealie.schema.recipe.recipe_step import RecipeStep
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
    IngestReadPath,
    PageMeta,
)
from mealie.scripts import eval_recipe_cards as ev
from mealie.services import ocr
from mealie.services.ai import anthropic_adapter
from mealie.services.ai.errors import AIProviderLocalOnlyError
from mealie.services.ai.local import clear_address_cache
from mealie.services.ai.policy import ai_call_policy
from mealie.services.openai import OpenAINotEnabledException, OpenAIService
from mealie.services.openai.openai import OpenAIImageBase
from mealie.services.recipe.import_workflow import DEFAULT_WORKFLOW_STEPS
from mealie.services.recipe.import_workflow.compilers import ImageCompiler, OCRImageCompiler
from mealie.services.recipe.import_workflow.steps import CompileSourceStep
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

CARDS_DIR = Path(__file__).parents[1] / "data" / "cards"

EXPECTED = ev.ExpectedRecipe(
    name="Banana Mug Cake",
    description_contains=["sugar", "gluten"],
    ingredients=[
        "1 banana",
        "1 T. coconut oil (melted)",
        "1/4 t. salt",
        "1/2 t vanilla",
        "1/3 C. almond flour",
        "1 egg",
        "Cinnamon to taste",
    ],
    instructions=[
        "Mash banana and mix ingredients thoroughly.",
        "Microwave in bowl or large mug for minutes or until firm in center.",
    ],
    must_not_invent=["cook time"],
)

BANANA_STEP = "Microwave in bowl or large mug for [blank] minutes or until firm in center."


def make_recipe(
    *,
    name: str = "Banana Mug Cake",
    description: str = "A sugar free, gluten free single serving cake.",
    ingredients: list[str] | None = None,
    instructions: list[str] | None = None,
    **kwargs,
) -> Recipe:
    if ingredients is None:
        ingredients = [
            "1 banana",
            "1 Tbsp coconut oil, melted",
            "¼ tsp salt",
            "½ t vanilla",
            "1/3 cup almond flour",
            "1 egg",
            "cinnamon, to taste",
        ]
    if instructions is None:
        instructions = list(EXPECTED.instructions)

    return Recipe(
        name=name,
        description=description,
        recipe_ingredient=[RecipeIngredient(note=line) for line in ingredients],
        recipe_instructions=[RecipeStep(text=text) for text in instructions],
        **kwargs,
    )


def banana_expected() -> ev.ExpectedRecipe:
    """The committed banana fixture's expected values (v2: structured lines and its blank)"""
    return ev.load_cards(CARDS_DIR, ["banana-mug-cake"])[0].fixture.expected


def make_draft(
    *,
    ingredients: Sequence[str | CardDraftIngredient] | None = None,
    steps: Sequence[str] | None = None,
    **kwargs: Any,
) -> CardDraft:
    lines = banana_expected().ingredient_lines if ingredients is None else ingredients
    return CardDraft(
        name=kwargs.pop("name", "Banana Mug Cake"),
        description=kwargs.pop("description", "Sugar free, gluten free"),
        ingredients=[
            line if isinstance(line, CardDraftIngredient) else CardDraftIngredient(original_text=line, display=line)
            for line in lines
        ],
        steps=[
            CardDraftStep(text=text) for text in (steps or ["Mash banana and mix ingredients thoroughly.", BANANA_STEP])
        ],
        **kwargs,
    )


def make_flag(
    kind: CardFlagKind, field: str, ref: object = None, severity: CardFlagSeverity = CardFlagSeverity.error, **kwargs
) -> CardFlag:
    ref = str(ref) if ref is not None else None
    return CardFlag(
        id=f"{kind.value}:{field}:{ref or ''}",
        kind=kind,
        severity=severity,
        source=kwargs.pop("source", CardFlagSource.marker),
        field=field,
        ref=ref,
        **kwargs,
    )


def make_extraction(draft: CardDraft, flags: Sequence[CardFlag] = (), read_path: str = "image") -> ev.CardExtraction:
    return ev.CardExtraction(
        draft=draft,
        flags=list(flags),
        transcription=None,
        extraction=ExtractionMeta(read_path=IngestReadPath(read_path)),
    )


# ================================================================
# Scoring


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1½ cups Flour", "1 1/2 cups flour"),
        ("¼ t. salt", "1/4 t salt"),
        ("1 T. coconut oil (melted)", "1 t coconut oil melted"),
        ("  Cinnamon\tto   taste ", "cinnamon to taste"),
        ("1.5 oz butter", "1.5 oz butter"),
        ("salt and/or pepper", "salt and or pepper"),
        ("2⅓ C.", "2 1/3 c"),
        ("for [blank] minutes, [ILLEGIBLE] sugar", "for minutes sugar"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_text(text: str | None, expected: str):
    assert ev.normalize_text(text) == expected


def test_text_similarity_forgives_formatting_but_not_different_ingredients():
    assert ev.text_similarity("1/4 t. salt", "¼ t salt") == 1.0
    assert ev.text_similarity("1/4 t. salt", "1/4 tsp salt") >= ev.INGREDIENT_MATCH_THRESHOLD
    assert ev.text_similarity("Cinnamon to taste", "cinnamon, to taste") == 1.0
    assert ev.text_similarity("1 egg", "1 egg yolk") < ev.INGREDIENT_MATCH_THRESHOLD
    assert ev.text_similarity("1 egg", "1 banana") < ev.INGREDIENT_MATCH_THRESHOLD


def test_match_lines_perfect_extraction():
    result = ev.match_lines(["1 egg", "1 banana"], [["1 banana"], ["1 egg"]])

    assert result.recall == 1.0
    assert result.precision == 1.0
    assert result.missing == []
    assert result.extra == []
    assert [(m.expected, m.actual) for m in result.matches] == [("1 egg", "1 egg"), ("1 banana", "1 banana")]
    # positions, to join a line with its flags
    assert [(m.expected_index, m.actual_index) for m in result.matches] == [(0, 1), (1, 0)]


def test_match_lines_missing_and_extra_lines():
    result = ev.match_lines(["1 egg", "1 banana", "1/4 t. salt", "1/2 t vanilla"], [["1 egg"], ["1 banana"], ["sugar"]])

    assert result.recall == 0.5
    assert result.precision == pytest.approx(2 / 3)
    assert result.missing == ["1/4 t. salt", "1/2 t vanilla"]
    assert result.extra == ["sugar"]
    assert result.extra_indices == [2]


def test_match_lines_is_one_to_one():
    # one extracted line can't stand in for two lines on the card
    result = ev.match_lines(["1 egg", "1 egg"], [["1 egg"]])

    assert result.recall == 0.5
    assert result.precision == 1.0


def test_match_lines_uses_closest_candidate_text():
    result = ev.match_lines(["1/4 t. salt"], [["Salt", "1/4 t salt"]])

    assert result.recall == 1.0
    assert result.matches[0].actual == "Salt"  # reported by the first, preferred, rendering


def test_match_lines_empty_extraction():
    result = ev.match_lines(["1 egg"], [])

    assert result.recall == 0.0
    assert result.precision == 0.0
    assert result.similarity == 0.0


@pytest.mark.parametrize(
    "line, quantity, unit",
    [
        ("1 T. coconut oil (melted)", (1.0,), "tbsp"),
        ("1 t. coconut oil", (1.0,), "tsp"),
        ("1/4 t salt", (0.25,), "tsp"),
        ("¼ tsp salt", (0.25,), "tsp"),
        ("1 Tbsp butter", (1.0,), "tbsp"),
        ("2 tablespoons sugar", (2.0,), "tbsp"),
        ("1/3 C. almond flour", (1 / 3,), "cup"),
        ("2 c flour", (2.0,), "cup"),
        ("1½ cups flour", (1.5,), "cup"),
        ("1 1/2 cups flour", (1.5,), "cup"),
        ("1-1/2 C. flour", (1.5,), "cup"),
        ("2⅓ C.", (2 + 1 / 3,), "cup"),
        ("0.5 tsp vanilla", (0.5,), "tsp"),
        ("1-2 Tbsp honey", (1.0, 2.0), "tbsp"),
        ("1 to 2 tablespoons honey", (1.0, 2.0), "tbsp"),
        ("250g flour", (250.0,), "g"),
        ("2 fl. oz. milk", (2.0,), "fl oz"),
        ("1 (15 oz) can tomatoes", (1.0,), "can"),
        ("1 heaping T. sugar", (1.0,), "tbsp"),
        ("1 TB. honey", (1.0,), "tbsp"),  # the pipeline's shorthand table: "TB." isn't terabytes
        ("1 pkg. yeast", (1.0,), "pkg"),
        # the pipeline's other abbreviations (`shorthand.ABBREVIATIONS`), scored like the units it writes for them
        ("1 doz. eggs", (1.0,), "dozen"),
        ("1 dozen eggs", (1.0,), "dozen"),
        ("1 env. Dream Whip", (1.0,), "envelope"),
        ("2 sq. chocolate", (2.0,), "square"),
        ("2 squares chocolate", (2.0,), "square"),
        ("1 banana", (1.0,), None),
        ("2 eggs", (2.0,), None),
        ("1 tomato", (1.0,), None),
        ("Cinnamon to taste", None, None),
        ("Tbsp sugar", None, None),  # a unit is only read after a quantity
        ("[blank] C. sugar", None, None),
    ],
)
def test_parse_amount(line: str, quantity: tuple[float, ...] | None, unit: str | None):
    amount = ev.parse_amount(line)

    assert amount.unit == unit
    assert amount.quantity == (pytest.approx(quantity) if quantity else None)


def test_card_shorthand_comes_from_the_pipelines_table():
    from mealie.services.ai.ingest.shorthand import UNITS

    assert set(ev.CASE_SENSITIVE_UNITS) == set(UNITS)
    assert ev.CASE_SENSITIVE_UNITS["T"] == "tbsp"
    assert ev.CASE_SENSITIVE_UNITS["t"] == "tsp"


def test_the_pipelines_abbreviations_are_units_the_eval_knows():
    from mealie.services.ai.ingest.shorthand import ABBREVIATIONS

    for token, unit in ABBREVIATIONS.items():
        assert ev.canonical_unit(token) == ev.canonical_unit(unit) is not None
    assert [ev.canonical_unit(unit) for unit in ("dozen", "envelope", "square")] == ["dozen", "envelope", "square"]


def test_parse_amount_writes_amounts_the_same_way():
    assert ev.parse_amount("1/4 t. salt").text == ev.parse_amount("¼ teaspoon salt").text == "0.25 tsp salt"
    assert ev.parse_amount("1 T. coconut oil (melted)").text == "1 tbsp coconut oil melted"
    assert ev.parse_amount("1 T. oil").text != ev.parse_amount("1 t. oil").text


def test_quantities_agree():
    assert ev.quantities_agree((1 / 3,), (0.33,))
    assert ev.quantities_agree(None, None)
    assert not ev.quantities_agree((0.25,), (0.5,))
    assert not ev.quantities_agree((1.0,), None)
    assert not ev.quantities_agree((1.0, 2.0), (1.0,))


def test_match_lines_forgives_how_an_amount_is_written():
    result = ev.match_lines(
        ["1/4 t. salt", "1 T. coconut oil (melted)", "1/3 C. almond flour", "1/2 t vanilla"],
        [["¼ teaspoon salt"], ["1 tablespoon coconut oil, melted"], ["0.33 cup almond flour"], ["½ tsp. vanilla"]],
    )

    assert result.recall == 1.0
    assert result.precision == 1.0
    assert result.misread == []


@pytest.mark.parametrize(
    "expected, actual, quantity_ok, unit_ok",
    [
        ("1/4 t. salt", "1/2 t. salt", False, True),
        ("1/4 t. salt", "1/4 T. salt", True, False),
        ("1 T. coconut oil (melted)", "1 t. coconut oil (melted)", True, False),
        ("1 T. coconut oil (melted)", "1 tsp coconut oil (melted)", True, False),
        ("1/3 C. almond flour", "1/3 tsp almond flour", True, False),
        ("1 banana", "2 bananas", False, True),
        ("Cinnamon to taste", "1 tsp cinnamon, to taste", False, False),  # an invented amount
        ("[blank] C. sugar", "1 C. sugar", False, False),  # a blank filled in
    ],
)
def test_match_lines_wrong_quantity_or_unit_does_not_match(
    expected: str, actual: str, quantity_ok: bool, unit_ok: bool
):
    result = ev.match_lines([expected], [[actual]])

    assert result.matches == []
    assert result.recall == 0.0
    assert result.precision == 0.0
    # reported as the same line misread, rather than one missing and one extra
    assert result.missing == []
    assert result.extra == []
    [misread] = result.misread
    assert (misread.expected, misread.actual) == (expected, actual)
    assert (misread.quantity_ok, misread.unit_ok) == (quantity_ok, unit_ok)


def test_match_lines_forgives_a_dropped_note_but_not_a_different_ingredient():
    assert ev.match_lines(["1 T. coconut oil (melted)"], [["1 Tbsp coconut oil"]]).recall == 1.0
    assert ev.match_lines(["1 egg"], [["1 egg yolk"]]).recall == 0.0


def test_match_lines_pairs_lines_by_amount():
    # the same ingredient twice, in different amounts, extracted in the other order
    result = ev.match_lines(["1 T. sugar", "1 t. sugar"], [["1 tsp sugar"], ["1 tbsp sugar"]])

    assert result.recall == 1.0
    assert [(m.expected, m.actual) for m in result.matches] == [
        ("1 T. sugar", "1 tbsp sugar"),
        ("1 t. sugar", "1 tsp sugar"),
    ]


def test_score_name():
    assert ev.score_name("Banana Mug Cake", "banana mug cake") == 1.0
    assert ev.score_name("Banana Mug Cake", "Mug Cake, Banana") == 1.0
    assert ev.score_name("Banana Mug Cake", "Chocolate Chip Cookies") < 0.5
    assert ev.score_name("Banana Mug Cake", None) == 0.0


def test_score_instructions():
    expected = EXPECTED.instructions

    assert ev.score_instructions(expected, expected) == 1.0
    # steps merged into one still cover the card
    assert ev.score_instructions(expected, [" ".join(expected)]) == 1.0
    # a missing step loses its share, weighted by length
    one_step = ev.score_instructions(expected, [expected[1]])
    assert one_step is not None and 0.5 < one_step < 1.0
    assert ev.score_instructions(expected, ["Preheat the oven to 350 and grease a 9x13 pan."]) == 0.0
    assert ev.score_instructions(expected, []) == 0.0
    assert ev.score_instructions([], ["Mix."]) is None
    # a kept blank neither helps nor hurts coverage: it's scored on its own
    assert ev.score_instructions([BANANA_STEP], [expected[1]]) == 1.0


def test_score_instructions_short_extraction_is_not_full_coverage():
    # partial_ratio alone would find "mash" inside the expected step and call it 100%
    assert ev.score_instructions(["Mash banana and mix ingredients thoroughly."], ["Mash."]) == 0.0


def test_score_description():
    assert ev.score_description(["sugar", "gluten"], "Sugar-free and gluten free!") == (1.0, ["sugar", "gluten"])
    assert ev.score_description(["sugar", "gluten"], "A quick cake") == (0.0, [])
    assert ev.score_description(["sugar"], "sugary") == (0.0, [])  # whole words only
    assert ev.score_description([], "anything") == (None, [])


def test_find_inventions_flags_invented_cook_time():
    assert ev.find_inventions(["cook time"], make_recipe()) == {}

    invented = ev.find_inventions(["cook time"], make_recipe(perform_time="2 minutes", total_time="5 minutes"))
    assert invented == {"cook time": {"total_time": "5 minutes", "perform_time": "2 minutes"}}


def test_find_inventions_other_checks():
    assert ev.find_inventions(["yield", "nutrition"], make_recipe()) == {}
    assert ev.find_inventions(["yield"], make_recipe(recipe_yield="1 mug")) == {"yield": {"recipe_yield": "1 mug"}}
    # a card draft, and the attribution check
    assert ev.find_inventions(["attribution"], make_draft(attribution="From Jo")) == {
        "attribution": {"attribution": "From Jo"}
    }


def test_unknown_must_not_invent_check_is_rejected():
    with pytest.raises(ValueError, match="must_not_invent"):
        ev.ExpectedRecipe(name="x", ingredients=["y"], must_not_invent=["bake temperature"])

    assert ev.ExpectedRecipe(name="x", ingredients=["y"], must_not_invent=["Cook_Time"]).must_not_invent == [
        "cook time"
    ]


def test_overall_score_skips_unscored_components():
    weights = {"a": 0.75, "b": 0.25, "c": 1.0}

    assert ev.overall_score({"a": 1.0, "b": 0.0}, weights) == 0.75
    assert ev.overall_score({"a": 1.0, "b": 0.0, "c": None}, weights) == 0.75
    assert ev.overall_score({"a": None}, weights) == 0.0


def test_good_extraction_scores_high():
    scores = ev.score_recipe(EXPECTED, make_recipe())

    assert scores.ingredient_recall == 1.0
    assert scores.ingredient_precision == 1.0
    assert scores.instruction_coverage == 1.0
    assert scores.description == 1.0
    assert scores.no_invention == 1.0
    assert scores.inventions == {}
    assert scores.overall > 0.95
    # the import pipeline has no flags to calibrate or link
    assert scores.calibration is None
    assert scores.linking is None


def test_missing_ingredients_lower_recall():
    good = ev.score_recipe(EXPECTED, make_recipe())
    partial = ev.score_recipe(EXPECTED, make_recipe(ingredients=["1 banana", "1 egg", "1/3 cup almond flour"]))

    assert partial.ingredient_recall == pytest.approx(3 / 7)
    assert partial.ingredient_precision == 1.0
    assert partial.overall < good.overall


def test_wrong_amounts_score_lower_than_exact():
    exact = ev.score_recipe(EXPECTED, make_recipe())
    # the right ingredients, one with a teaspoon for the card's tablespoon
    one_wrong = ev.score_recipe(
        EXPECTED,
        make_recipe(
            ingredients=[
                "1 banana",
                "1 tsp coconut oil, melted",
                "¼ tsp salt",
                "½ t vanilla",
                "1/3 cup almond flour",
                "1 egg",
                "cinnamon, to taste",
            ]
        ),
    )

    assert one_wrong.ingredient_recall == pytest.approx(6 / 7)
    assert one_wrong.ingredient_precision == pytest.approx(6 / 7)
    assert [m.expected for m in one_wrong.ingredients.misread] == ["1 T. coconut oil (melted)"]
    assert one_wrong.overall < exact.overall


def test_mostly_wrong_amounts_score_low():
    # every ingredient found, but six of seven with the wrong quantity or unit
    misread = ev.score_recipe(
        EXPECTED,
        make_recipe(
            ingredients=[
                "2 bananas",
                "1 t. coconut oil (melted)",
                "1/2 t. salt",
                "1/2 T vanilla",
                "1/2 C. almond flour",
                "2 eggs",
                "Cinnamon to taste",
            ]
        ),
    )

    assert misread.ingredient_recall == pytest.approx(1 / 7)
    assert len(misread.ingredients.misread) + len(misread.ingredients.missing) == 6
    assert misread.overall < 0.7


def test_invented_ingredients_lower_precision():
    padded = ev.score_recipe(
        EXPECTED, make_recipe(ingredients=[*EXPECTED.ingredient_lines, "2 tbsp sugar", "1 tsp baking powder"])
    )

    assert padded.ingredient_recall == 1.0
    assert padded.ingredient_precision == pytest.approx(7 / 9)
    assert padded.ingredients.extra == ["2 tbsp sugar", "1 tsp baking powder"]


def test_invented_cook_time_is_flagged():
    good = ev.score_recipe(EXPECTED, make_recipe())
    invented = ev.score_recipe(EXPECTED, make_recipe(perform_time="2 minutes"))

    assert invented.inventions == {"cook time": {"perform_time": "2 minutes"}}
    assert invented.no_invention == 0.0
    assert invented.overall < good.overall


# ----------------------------------------------------------------
# Phase 2 columns (card pipeline)


def test_the_v2_fixture_scores_like_v1():
    """Structured lines and the kept blank don't change the overall score: v1 and v2 numbers compare"""
    recipe = make_recipe(instructions=["Mash banana and mix ingredients thoroughly.", BANANA_STEP])

    assert ev.score_recipe(banana_expected(), recipe).overall == pytest.approx(
        ev.score_recipe(EXPECTED, recipe).overall
    )


def test_an_invented_number_is_a_step_invention():
    invented = "Microwave in bowl or large mug for 2 minutes or until firm in center."
    scores = ev.score_card(banana_expected(), make_extraction(make_draft(steps=[EXPECTED.instructions[0], invented])))

    # coverage alone barely notices
    assert scores.instruction_coverage is not None and scores.instruction_coverage > 0.9
    assert [(i.field, i.value) for i in scores.step_inventions] == [("steps", 2.0)]
    # and nothing flagged it, so it's a silent error
    assert scores.calibration is not None
    assert scores.calibration.silent_errors == 1
    assert scores.blanks_kept == 0.0
    assert scores.blanks_safe == 0.0


def test_numbers_on_the_card_are_not_inventions():
    draft = make_draft(
        steps=["Mash 1 banana and mix ingredients thoroughly.", BANANA_STEP],
        total_time="5 minutes",
        recipe_yield="1 mug",
    )

    scores = ev.score_card(banana_expected(), make_extraction(draft))

    # "1" is on the card (1 banana); "5" isn't
    assert [(i.field, i.value) for i in scores.step_inventions] == [("total_time", 5.0)]
    assert scores.inventions == {"cook time": {"total_time": "5 minutes"}}


def test_blanks_kept_and_blanks_safe():
    expected = banana_expected()
    filled = "Microwave in bowl or large mug for 2 minutes or until firm in center."

    # kept as [blank], with the marker's own error flag
    draft = make_draft()
    marker = make_flag(CardFlagKind.blank, "steps", draft.steps[1].id)
    kept = ev.score_card(expected, make_extraction(draft, [marker]))
    assert (kept.blanks_kept, kept.blanks_safe) == (1.0, 1.0)
    assert kept.blanks[0].found == BANANA_STEP

    # filled with a number, but the cross-read flagged it with an error: safe, not kept
    draft = make_draft(steps=[EXPECTED.instructions[0], filled])
    cross = make_flag(CardFlagKind.blank, "steps", draft.steps[1].id, source=CardFlagSource.cross_read)
    flagged = ev.score_card(expected, make_extraction(draft, [cross]))
    assert (flagged.blanks_kept, flagged.blanks_safe) == (0.0, 1.0)

    # a warning isn't enough: the reviewer can commit past it
    warning = make_flag(CardFlagKind.not_on_card, "steps", draft.steps[1].id, severity=CardFlagSeverity.warning)
    warned = ev.score_card(expected, make_extraction(draft, [warning]))
    assert (warned.blanks_kept, warned.blanks_safe) == (0.0, 0.0)

    # a resolved flag no longer counts
    resolved = cross.model_copy(update={"resolution": "kept"})
    assert ev.score_card(expected, make_extraction(draft, [resolved])).blanks_safe == 0.0

    # the step left out entirely
    missing = ev.score_card(expected, make_extraction(make_draft(steps=[EXPECTED.instructions[0]])))
    assert (missing.blanks_kept, missing.blanks_safe) == (0.0, 0.0)

    # the import pipeline has no flags, so safety isn't known
    assert ev.score_recipe(expected, make_recipe(instructions=[filled])).blanks_safe is None


def test_a_blank_in_a_note_is_safe_when_its_note_is_flagged():
    """A note's flags are keyed to its id, as the review page and the flags key them"""
    expected = ev.ExpectedRecipe(
        name="Pie", ingredients=[], blanks=[ev.ExpectedBlank(field="notes", text="Freezes for [blank] months")]
    )
    filled = CardDraftNote(text="Freezes for 3 months")
    draft = make_draft(ingredients=[], notes=[CardDraftNote(text="Serve warm"), filled])

    flag = make_flag(CardFlagKind.blank, "notes", filled.id, source=CardFlagSource.cross_read)
    scores = ev.score_card(expected, make_extraction(draft, [flag]))
    assert (scores.blanks_kept, scores.blanks_safe) == (0.0, 1.0)
    assert scores.blanks[0].ref == str(filled.id)

    # another note's flag doesn't make it safe
    other = make_flag(CardFlagKind.blank, "notes", draft.notes[0].id, source=CardFlagSource.cross_read)
    assert ev.score_card(expected, make_extraction(draft, [other])).blanks_safe == 0.0


def test_a_blank_time_is_kept_when_left_empty():
    expected = ev.ExpectedRecipe(name="Pie", ingredients=[], blanks=[ev.ExpectedBlank(field="perform_time")])

    assert ev.score_card(expected, make_extraction(make_draft(ingredients=[]))).blanks_kept == 1.0
    assert (
        ev.score_card(expected, make_extraction(make_draft(ingredients=[], perform_time="[blank]"))).blanks_kept == 1.0
    )
    invented = ev.score_card(expected, make_extraction(make_draft(ingredients=[], perform_time="10 minutes")))
    assert (invented.blanks_kept, invented.blanks_safe) == (0.0, 0.0)
    flag = make_flag(CardFlagKind.blank, "performTime", source=CardFlagSource.cross_read)
    safe = ev.score_card(expected, make_extraction(make_draft(ingredients=[], perform_time="10 minutes"), [flag]))
    assert safe.blanks_safe == 1.0


def test_flag_calibration_math():
    expected = banana_expected()
    lines = [*expected.ingredient_lines[:2], "1/2 t. salt", *expected.ingredient_lines[3:6], "2 T. sugar"]
    draft = make_draft(ingredients=lines)  # "Cinnamon to taste" left out
    misread, invented = draft.ingredients[2], draft.ingredients[6]
    flags = [
        make_flag(CardFlagKind.unsure, "ingredients", misread.reference_id, severity=CardFlagSeverity.warning),
        make_flag(CardFlagKind.blank, "steps", draft.steps[1].id),
        # infos aren't highlighted
        make_flag(CardFlagKind.shorthand_read, "ingredients", invented.reference_id, severity=CardFlagSeverity.info),
    ]

    calibration = ev.score_card(expected, make_extraction(draft, flags)).calibration

    assert calibration is not None
    # name, 7 lines, 1 missing line, 2 steps
    assert len(calibration.items) == 11
    assert [item.kind for item in calibration.items if not item.correct] == [
        "ingredient",
        "ingredient",
        "missing_ingredient",
    ]
    assert (calibration.wrong, calibration.flagged, calibration.wrong_flagged, calibration.silent_errors) == (
        3,
        2,
        1,
        2,
    )
    assert not calibration.clean
    assert not calibration.fully_correct

    summary = ev.summarize_runs("x", "m", [run_with(ev.score_card(expected, make_extraction(draft, flags)))])
    assert summary.flag_recall == pytest.approx(1 / 3)
    assert summary.flag_precision == pytest.approx(1 / 2)
    assert summary.flag_rate == pytest.approx(2 / 11)
    assert summary.silent_errors == 2


def run_with(
    scores: ev.CardScores | None, *, card: str = "card", label: str = "x", attempt: int = 1, **kwargs
) -> ev.RunResult:
    kwargs.setdefault("latency_s", 1.0)
    return ev.RunResult(card=card, label=label, attempt=attempt, verified_by_owner=True, scores=scores, **kwargs)


def test_clean_card_precision():
    expected = banana_expected()
    correct = make_draft()
    wrong = make_draft(ingredients=[*expected.ingredient_lines[:6], "2 t. cinnamon"])
    flagged = make_draft(ingredients=[*expected.ingredient_lines[:6], "2 t. cinnamon"])
    flag = make_flag(CardFlagKind.not_on_card, "ingredients", flagged.ingredients[6].reference_id)

    runs = [
        run_with(ev.score_card(expected, make_extraction(correct)), card="a"),  # clean and right
        run_with(ev.score_card(expected, make_extraction(wrong)), card="b"),  # clean but wrong
        run_with(ev.score_card(expected, make_extraction(flagged, [flag])), card="c"),  # not clean
    ]

    summary = ev.summarize_runs("x", "m", runs)
    assert summary.clean_precision == 0.5
    # b: the invented line and the missing one; c: the missing one (its invented line is flagged)
    assert summary.silent_errors == pytest.approx((0 + 2 + 1) / 3)


class Catalog:
    """The group's foods and units by name, as `IngestMatcher.exact_food` and `exact_unit` answer"""

    def __init__(self, foods: dict[str, UUID], units: dict[str, UUID]) -> None:
        self.foods, self.units = foods, units

    def exact_food(self, name: str | None) -> Any:
        food_id = self.foods.get((name or "").lower())
        return SimpleNamespace(id=food_id) if food_id else None

    def exact_unit(self, name: str | None) -> Any:
        unit_id = self.units.get((name or "").lower())
        return SimpleNamespace(id=unit_id) if unit_id else None


def test_linking_relative_to_the_groups_foods():
    banana, coconut, salt, tbsp, tsp = (uuid4() for _ in range(5))
    catalog = Catalog({"banana": banana, "coconut oil": coconut, "salt": salt}, {"tbsp": tbsp, "tsp": tsp})
    expected = banana_expected()

    def line(text: str, food: CardDraftRef | None = None, unit: CardDraftRef | None = None) -> CardDraftIngredient:
        return CardDraftIngredient(original_text=text, display=text, food=food, unit=unit)

    draft = make_draft(
        ingredients=[
            line("1 banana", CardDraftRef(id=banana, name="banana")),
            # linked to an existing food, but the wrong one
            line("1 T. coconut oil (melted)", CardDraftRef(id=salt, name="salt"), CardDraftRef(id=tbsp, name="tbsp")),
            # the group has salt, but the line wasn't linked to it: commit would duplicate it
            line("1/4 t. salt", CardDraftRef(name="salt"), CardDraftRef(id=tsp, name="teaspoon")),
            # new foods the group doesn't have yet, named right
            line("1/2 t vanilla", CardDraftRef(name="vanilla"), CardDraftRef(id=tsp, name="teaspoon")),
            line("1/3 C. almond flour", CardDraftRef(name="almond flour"), CardDraftRef(name="cup")),
            line("1 egg", CardDraftRef(name="eggs")),
            line("Cinnamon to taste", CardDraftRef(name="cinnamon")),
        ]
    )

    linking = ev.score_card(expected, make_extraction(draft), catalog=catalog).linking

    assert linking is not None
    assert (linking.food_checked, linking.food_correct, linking.food_wrong, linking.food_unlinked) == (7, 5, 1, 5)
    assert (linking.unit_checked, linking.unit_correct, linking.unit_wrong) == (4, 4, 0)
    assert linking.food_link_acc == pytest.approx(5 / 7)
    assert linking.unit_link_acc == 1.0
    assert linking.wrong_link_rate == pytest.approx(1 / 11)
    assert linking.new_food_rate == pytest.approx(5 / 7)

    # without the group's catalog, links are judged by name
    by_name = ev.score_card(expected, make_extraction(draft)).linking
    assert by_name is not None
    assert by_name.food_correct == 6  # salt, unlinked but named right, now counts


def test_attribution_yield_and_times():
    expected = ev.ExpectedRecipe(
        name="Pancakes",
        ingredients=["2 C. flour"],
        attribution="From Grandma Jo",
        recipe_yield="12 pancakes",
        times=ev.ExpectedTimes(prep_time="10 min", perform_time="20 minutes"),
    )
    draft = make_draft(
        name="Pancakes",
        ingredients=["2 C. flour"],
        steps=["Mix."],
        attribution="From Grandma Jo",
        recipe_yield="12 pancakes",
        prep_time="10 minutes",
        total_time="20 min",  # the card's cook time, in another field
    )

    scores = ev.score_card(expected, make_extraction(draft))

    assert scores.attribution == 1.0
    # the draft keeps the attribution without its "From" now, as the review page labels it
    stripped = make_extraction(draft.model_copy(update={"attribution": "Grandma Jo"}))
    assert ev.score_card(expected, stripped).attribution == 1.0
    assert scores.recipe_yield == 1.0
    assert scores.times == 1.0
    assert ev.score_card(expected, make_extraction(make_draft(name="Pancakes", recipe_yield="6"))).recipe_yield == 0.0
    no_attribution = ev.ExpectedRecipe(name="x", ingredients=[])
    assert ev.score_card(no_attribution, make_extraction(draft)).attribution is None


# ================================================================
# Fixtures, CLI and reporting


def test_every_committed_fixture_validates():
    cards = ev.load_cards(CARDS_DIR)

    ev.check_cards(cards)
    assert "banana-mug-cake" in [card.id for card in cards]


def test_the_banana_fixture_is_v2():
    [card] = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    fixture = card.fixture

    assert card.images == [CARDS_DIR / "banana-mug-cake.jpg"]
    assert fixture.schema_version == 2
    assert set(fixture.tags) == {"handwritten", "sideways", "blank"}
    assert fixture.expected.name == "Banana Mug Cake"
    assert fixture.expected.must_not_invent == ["cook time"]
    assert fixture.expected.ingredient_lines == EXPECTED.ingredient_lines
    assert all(line is not None and line.food for line in fixture.expected.structured_ingredients)
    assert [(blank.field, blank.text) for blank in fixture.expected.blanks] == [("steps", BANANA_STEP)]


def test_v1_fixtures_still_load(tmp_path: Path):
    fixture = {"source": "card.jpg", "verified_by_owner": True, "expected": {"name": "x", "ingredients": ["1 egg"]}}
    (tmp_path / "card.json").write_text(json.dumps(fixture))
    (tmp_path / "card.jpg").write_bytes((CARDS_DIR / "banana-mug-cake.jpg").read_bytes())

    [card] = ev.load_cards(tmp_path)

    assert card.fixture.schema_version == 1
    assert card.fixture.tags == []
    assert card.fixture.local_only is False


def test_load_cards_rejects_bad_input(tmp_path: Path):
    with pytest.raises(ev.EvalSetupError, match="doesn't exist"):
        ev.load_cards(tmp_path / "missing")

    with pytest.raises(ev.EvalSetupError, match="No cards"):
        ev.load_cards(tmp_path)

    with pytest.raises(ev.EvalSetupError, match="no-such-card"):
        ev.load_cards(CARDS_DIR, ["no-such-card"])

    fixture = {"source": ["front.jpg", "back.jpg"], "expected": {"name": "x", "ingredients": ["y"]}}
    (tmp_path / "two-sided.json").write_text(json.dumps(fixture))
    (tmp_path / "front.jpg").write_bytes(b"")
    with pytest.raises(ev.EvalSetupError, match="back.jpg"):
        ev.load_cards(tmp_path)

    (tmp_path / "back.jpg").write_bytes(b"")
    assert ev.load_cards(tmp_path)[0].images == [tmp_path / "front.jpg", tmp_path / "back.jpg"]

    # but they aren't images, which --check finds
    with pytest.raises(ev.EvalSetupError, match="front.jpg"):
        ev.check_cards(ev.load_cards(tmp_path))

    # a typo is an error, not a silently dropped key
    (tmp_path / "two-sided.json").write_text(json.dumps({**fixture, "verifed_by_owner": True}))
    with pytest.raises(ev.EvalSetupError, match="verifed_by_owner"):
        ev.load_cards(tmp_path)


def test_check_needs_no_group(capsys: pytest.CaptureFixture[str], tmp_path: Path):
    ev.main(["--check", "--cards", str(CARDS_DIR)])

    out = capsys.readouterr().out
    assert "banana-mug-cake: v2, 1 image(s), unverified [handwritten, sideways, blank]" in out
    assert "are valid" in out

    with pytest.raises(SystemExit) as exit_info:
        ev.main(["--check", "--cards", str(tmp_path)])
    assert exit_info.value.code == 2


def test_parser_accepts_documented_flags():
    args = ev.parse_args(
        [
            *("--group", "home", "--household", "family"),
            *("--provider", "Gemini", "--provider", "qwen3-vl:Claude Sonnet", "--ocr"),
            *("--ocr-provider", "qwen3-vl", "--ocr-provider", "Claude Sonnet"),
            *("--cards", "/app/data/cards", "--card", "banana-mug-cake", "--out", "results.json", "--repeat", "3"),
            *("--price", "Gemini=0.30,2.50", "--price", "Ollama=0,0"),
            *("--cross-read", "--no-intake-ocr", "--local-only", "--baseline", "Gemini"),
            *("--chain", "qwen3-vl>OCR+qwen3-vl", "--chain", "A > B > C", "--reference", "before.json"),
        ]
    )

    assert args.group == "home"
    assert args.household == "family"
    assert args.providers == ["Gemini", "qwen3-vl:Claude Sonnet"]
    assert args.ocr is True
    assert args.ocr_providers == ["qwen3-vl", "Claude Sonnet"]
    assert args.cards == Path("/app/data/cards")
    assert args.only_cards == ["banana-mug-cake"]
    assert args.out == Path("results.json")
    assert args.repeat == 3
    assert dict(args.prices) == {"Gemini": (0.30, 2.50), "Ollama": (0.0, 0.0)}
    assert (args.cross_read, args.intake_ocr, args.local_only) == (True, False, True)
    assert args.baseline == "Gemini"
    assert args.chains == [["qwen3-vl", "OCR+qwen3-vl"], ["A", "B", "C"]]
    assert args.reference == Path("before.json")
    assert args.pipeline == "card"


def test_parser_defaults():
    args = ev.parse_args(["--group", "home"])

    assert args.providers == []
    assert args.ocr is False
    assert args.ocr_providers == []
    assert args.cards == ev.DEFAULT_CARDS_DIR
    assert args.out == ev.DEFAULT_OUT
    assert args.repeat == 1
    assert args.pipeline == "card"
    assert (args.cross_read, args.intake_ocr, args.local_only, args.check) == (False, True, False, False)
    assert args.chains == []


@pytest.mark.parametrize(
    "argv",
    [
        [],  # --group is required to run
        ["--group", "home", "--repeat", "0"],
        ["--group", "home", "--price", "Gemini"],
        ["--group", "home", "--price", "Gemini=1"],
        ["--group", "home", "--price", "Gemini=-1,2"],
        ["--group", "home", "--chain", "Gemini"],
        ["--group", "home", "--chain", "Gemini>Gemini"],
        ["--group", "home", "--chain", "Gemini>"],
        ["--group", "home", "--pipeline", "import", "--cross-read"],
        ["--group", "home", "--pipeline", "import", "--no-intake-ocr"],
        ["--group", "home", "--pipeline", "v3"],
    ],
)
def test_parser_rejects_bad_flags(argv: list[str]):
    with pytest.raises(SystemExit):
        ev.parse_args(argv)


def test_run_cost():
    usage = {"Gemini": ev.TokenUsage(requests=2, prompt_tokens=1_000_000, completion_tokens=500_000)}

    assert ev.run_cost(usage, {"Gemini": (0.30, 2.50)}) == pytest.approx(0.30 + 1.25)
    assert ev.run_cost(usage, {}) is None
    # a run that reported no usage, say because it failed, has no known cost
    assert ev.run_cost({}, {}) is None
    assert ev.run_cost({}, {"Gemini": (0.30, 2.50)}) is None


def make_provider(name: str = "Gemini", model: str = "gemini-flash", **kwargs) -> AIProviderOut:
    return AIProviderOut(id=uuid4(), name=name, model=model, api_key="secret-key", **kwargs)


def test_report():
    card = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])[0]
    provider = make_provider()
    configs = [ev.EvalConfig(label="Gemini", image_provider=provider, text_provider=provider)]
    results = [
        ev.RunResult(
            card=card.id,
            label="Gemini",
            attempt=1,
            verified_by_owner=False,
            latency_s=2.0,
            scores=ev.score_card(card.fixture.expected, make_extraction(make_draft())),
            usage={"Gemini": ev.TokenUsage(requests=2, prompt_tokens=1000, completion_tokens=200)},
            usage_by_slot={"Gemini [image]": ev.TokenUsage(requests=1, prompt_tokens=900, completion_tokens=100)},
            cost_usd=0.0008,
            read_path="image",
            prompts={"recipes.card-compile-rules": "ab" * 32},
            tags=list(card.fixture.tags),
        ),
        # a run that failed before the provider answered reported no usage, so has no cost
        ev.RunResult(card=card.id, label="Gemini", attempt=2, verified_by_owner=False, latency_s=30.0, error="Boom"),
    ]

    table = ev.format_report(results, configs, [card])
    assert "Gemini" in table
    assert "Misread" in table
    assert "Silent/card" in table
    assert "banana-mug-cake (unverified)" in table
    assert "Mean score per tag" in table and "sideways" in table

    [summary] = ev.summarize(results, configs)
    assert summary.runs == 2
    assert summary.errors == 1
    assert summary.score == pytest.approx(results[0].score / 2)  # failed runs count as 0
    assert summary.latency_s == 2.0  # failed runs don't count towards latency
    assert summary.misread == 0
    assert summary.cost_usd == pytest.approx(0.0008)  # the mean of the runs with a cost
    assert summary.blanks_kept == 1.0
    assert summary.tokens_by_slot == {"Gemini [image]": 1000}
    assert summary.card_std == pytest.approx(results[0].score / 2**0.5)

    report = ev.build_report(results, configs, [card], group="home", cards_dir=CARDS_DIR, repeat=2)
    dumped = json.dumps(report, default=str)
    assert "secret-key" not in dumped
    loaded = json.loads(dumped)
    assert loaded["runs"][1]["error"] == "Boom"
    assert loaded["runs"][0]["read_path"] == "image"
    assert loaded["prompts"] == {"recipes.card-compile-rules": ["ab" * 32]}
    assert loaded["pipeline"] == "card"
    assert "mealie_commit" in loaded


def test_per_tag_rows():
    cards = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    results = [run_with(ev.score_recipe(EXPECTED, make_recipe()), card="banana-mug-cake", label="A")]

    rows = ev.per_tag_rows(results, ["A"], cards)

    assert [(tag, label, count) for tag, label, count, _ in rows] == [
        ("handwritten", "A", 1),
        ("sideways", "A", 1),
        ("blank", "A", 1),
    ]
    assert rows[0][3] == pytest.approx(results[0].score)


# ================================================================
# Chains and baselines


def scored(overall: float) -> ev.CardScores:
    return dataclasses.replace(ev.score_recipe(EXPECTED, make_recipe()), overall=overall)


def vision_and_ocr() -> list[ev.EvalConfig]:
    vision, text = make_provider("V"), make_provider("T")
    return [
        ev.EvalConfig(label="V", image_provider=vision, text_provider=vision),
        ev.EvalConfig(label="OCR+T", image_provider=None, text_provider=text),
    ]


def chain_inputs() -> list[ev.RunResult]:
    def run(card: str, label: str, score: float | None, read_path: str | None, latency: float) -> ev.RunResult:
        return run_with(
            scored(score) if score is not None else None,
            card=card,
            label=label,
            read_path=read_path,
            latency_s=latency,
            error=None if score is not None else "failed",
            orient_s=0.5,
            usage={label: ev.TokenUsage(requests=1, prompt_tokens=10, completion_tokens=5)},
            cost_usd=0.01,
        )

    return [
        run("c1", "V", 0.9, "image", 2.5),
        run("c1", "OCR+T", 0.5, "ocr", 4.5),
        run("c2", "V", None, None, 1.5),
        run("c2", "OCR+T", 0.6, "ocr", 4.5),
        # read, but not by its own reader: doesn't count as reading the card itself
        run("c3", "V", 0.8, "ocr", 2.5),
        run("c3", "OCR+T", 0.4, "ocr", 4.5),
        run("c4", "V", None, None, 1.5),
        run("c4", "OCR+T", None, None, 4.5),
    ]


def test_chain_rows_come_from_existing_results():
    configs = vision_and_ocr()

    rows = ev.chain_results(chain_inputs(), ["V", "OCR+T"], configs)

    assert [(r.card, r.label, r.chain_source, r.fell_back) for r in rows] == [
        ("c1", "V>OCR+T", "V", False),
        ("c2", "V>OCR+T", "OCR+T", True),
        ("c3", "V>OCR+T", "OCR+T", True),
        ("c4", "V>OCR+T", "OCR+T", True),
    ]
    assert [r.score for r in rows] == pytest.approx([0.9, 0.6, 0.4, 0.0])
    # latencies of the configs tried, the orientation probe counted once
    assert [r.latency_s for r in rows] == pytest.approx([2.5, 0.5 + 1.0 + 4.0, 0.5 + 2.0 + 4.0, 0.5 + 1.0 + 4.0])
    assert rows[1].usage == {
        "V": ev.TokenUsage(requests=1, prompt_tokens=10, completion_tokens=5),
        "OCR+T": ev.TokenUsage(requests=1, prompt_tokens=10, completion_tokens=5),
    }
    assert rows[1].cost_usd == pytest.approx(0.02)
    assert ev.rescued_cards(rows) == ["c2", "c3"]


def test_compare_with_a_baseline():
    results = chain_inputs()
    results += ev.chain_results(results, ["V", "OCR+T"], vision_and_ocr())

    comparison = ev.compare(results, "V>OCR+T", "V")

    assert comparison.deltas == pytest.approx({"c1": 0.0, "c2": 0.6, "c3": -0.4, "c4": 0.0})
    assert (comparison.cards, comparison.wins, comparison.ties, comparison.losses) == (4, 1, 2, 1)
    assert comparison.mean_delta == pytest.approx(0.05)
    assert comparison.interval is not None
    low, high = comparison.interval
    assert -0.4 <= low <= 0.05 <= high <= 0.6
    # seeded: the same interval every time
    assert ev.compare(results, "V>OCR+T", "V").interval == comparison.interval
    assert not comparison.excludes_zero


def test_bootstrap_interval():
    assert ev.bootstrap_interval([]) is None
    assert ev.bootstrap_interval([0.2, 0.2, 0.2]) == pytest.approx((0.2, 0.2))
    interval = ev.bootstrap_interval([0.1, 0.2, 0.3, 0.4] * 5)
    assert interval is not None
    low, high = interval
    assert 0.1 < low < 0.25 < high < 0.4
    assert ev.bootstrap_interval([0.1, 0.3], seed=1) == ev.bootstrap_interval([0.1, 0.3], seed=1)


def test_unknown_chain_labels_fail_before_any_call():
    configs = vision_and_ocr()

    with pytest.raises(ev.EvalSetupError, match=r"'Nope'.*available: V, OCR\+T"):
        ev.check_labels([["V", "Nope"]], None, configs)
    with pytest.raises(ev.EvalSetupError, match="--baseline 'Gemini'"):
        ev.check_labels([], "Gemini", configs)
    ev.check_labels([["V", "OCR+T"]], "V>OCR+T", configs)


def test_main_fails_fast_on_an_unknown_chain(
    unique_user: TestUser, providers: dict[str, AIProviderOut], monkeypatch: pytest.MonkeyPatch
):
    async def run_eval(*args, **kwargs):
        raise AssertionError("no provider may be called")

    monkeypatch.setattr(ev, "run_eval", run_eval)
    group = unique_user.repos.groups.get_one(unique_user.group_id)
    assert group
    vision = providers["vision"].name

    with pytest.raises(SystemExit) as exit_info:
        ev.main(["--group", group.slug, "--provider", vision, "--chain", f"{vision}>OCR+Nope"])
    assert exit_info.value.code == 2


def test_decisions():
    configs = vision_and_ocr()
    results = chain_inputs()
    expected = banana_expected()
    for attempt in (1, 2, 3):
        draft = make_draft()
        flag = make_flag(CardFlagKind.blank, "steps", draft.steps[1].id)
        results.append(
            run_with(
                ev.score_card(expected, make_extraction(draft, [flag])),
                card="banana-mug-cake",
                label="V",
                attempt=attempt,
                read_path="image",
            )
        )
    results += ev.chain_results(results, ["V", "OCR+T"], configs)
    summaries = ev.summarize(results, configs, [["V", "OCR+T"]])
    cards = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    settings = ev.EvalSettings()

    decisions = ev.decide(results, summaries, configs, [["V", "OCR+T"]], cards, {}, settings)

    [vision_target] = [t for t in decisions.targets if t["label"] == "V"]
    assert vision_target["banana_blank_safe"] is True
    assert vision_target["silent_errors_ok"] is True
    [rule] = decisions.last_resort
    assert rule["rescued_cards"] == ["c2", "c3"]
    assert rule["keep_ocr_fallback"] is True
    text = ev.format_decisions(decisions, settings)
    assert "keep the OCR fallback" in text
    assert "Cross-read default: n/a" in text


def test_the_cross_read_rule_against_a_reference_run(tmp_path: Path):
    configs = vision_and_ocr()
    expected = banana_expected()
    filled = "Microwave in bowl or large mug for 2 minutes or until firm in center."
    card = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])

    def runs(draft_steps: list[str], flagged: bool) -> list[ev.RunResult]:
        out = []
        for attempt in (1, 2, 3):
            draft = make_draft(steps=draft_steps)
            flags = [make_flag(CardFlagKind.blank, "steps", draft.steps[1].id, source=CardFlagSource.cross_read)]
            out.append(
                run_with(
                    ev.score_card(expected, make_extraction(draft, flags if flagged else [])),
                    card="banana-mug-cake",
                    label="V",
                    attempt=attempt,
                    read_path="image",
                )
            )
        return out

    without = runs([EXPECTED.instructions[0], filled], flagged=False)
    reference_file = tmp_path / "without.json"
    reference_file.write_text(
        json.dumps(ev.build_report(without, configs, card, group="g", cards_dir=CARDS_DIR, repeat=3), default=str)
    )
    reference = ev.load_reference(reference_file)
    assert [run.silent_errors for run in reference] == [1, 1, 1]
    assert [run.blanks_safe for run in reference] == [0.0, 0.0, 0.0]

    with_ = runs([EXPECTED.instructions[0], filled], flagged=True)
    settings = ev.EvalSettings(cross_read=True)
    summaries = ev.summarize(with_, configs)
    decisions = ev.decide(with_, summaries, configs, [], card, {}, settings, reference)

    [rule] = decisions.cross_read
    assert rule["drop"] == 1.0
    assert rule["banana_safe_only_with"] is True
    assert rule["turn_on"] is True
    assert "turn cross-read on by default" in ev.format_decisions(decisions, settings)


# ================================================================
# Preparing cards: intake normalization and orientation


def fake_orient(turn: int):
    def orient_page(page: ev.CardPage) -> PageMeta:
        return page.meta.model_copy(update={"rotation": turn, "oriented": True})

    return orient_page


def test_prepare_card_normalizes_and_orients(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(ocr, "binary_available", lambda: True)  # orientation needs only Tesseract installed
    monkeypatch.setattr(ev, "orient_page", fake_orient(90))
    [card] = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])

    prepared = ev.prepare_card(card, tmp_path)

    assert prepared.error is None
    [page] = prepared.pages
    assert (page.dir / "page.jpg").is_file() and (page.dir / "view.jpg").is_file()
    assert page.meta.raw_sha256 and page.meta.format == "jpeg"
    assert prepared.rotations == prepared.probed_rotations == [90]

    copies = prepared.copy_to(tmp_path / "run")
    assert copies[0].dir != page.dir
    assert (copies[0].dir / "view.jpg").read_bytes() == (page.dir / "view.jpg").read_bytes()


def test_no_intake_ocr_still_reports_wrong_turns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(ocr, "binary_available", lambda: True)
    monkeypatch.setattr(ev, "orient_page", fake_orient(180))
    [banana] = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    upright = ev.Card(id="upright", images=banana.images, fixture=banana.fixture.model_copy(update={"tags": []}))

    prepared = {card.id: ev.prepare_card(card, tmp_path / card.id, intake_ocr=False) for card in (banana, upright)}

    # the pages are read as intake left them, and the probe's turns are reported
    assert prepared["upright"].rotations == [0]
    assert prepared["upright"].orient_s == 0
    assert prepared["upright"].probed_rotations == [180]
    turns = ev.wrong_turns([banana, upright], prepared)
    assert turns is not None
    assert (turns.upright, turns.turned, turns.passed) == (1, ["upright"], True)

    two = ev.Card(id="upright-2", images=banana.images, fixture=upright.fixture)
    prepared["upright-2"] = prepared["upright"]
    turns = ev.wrong_turns([banana, upright, two], prepared)
    assert turns is not None and not turns.passed

    # without Tesseract there's nothing to report
    monkeypatch.setattr(ocr, "binary_available", lambda: False)
    plain = ev.prepare_card(upright, tmp_path / "plain")
    assert plain.probed_rotations is None
    assert ev.wrong_turns([upright], {"upright": plain}) is None


def test_a_card_that_cant_be_read_is_an_error_for_every_run(tmp_path: Path):
    (tmp_path / "card.pdf").write_bytes(b"%PDF-1.7 not an image")
    card = ev.Card(
        id="pdf", images=[tmp_path / "card.pdf"], fixture=ev.CardFixture(source="card.pdf", expected=EXPECTED)
    )

    prepared = ev.prepare_card(card, tmp_path / "work")

    assert prepared.error == "PageRejected: pdf_not_supported"


# ================================================================
# Running against the database, with the AI provider stubbed out


@pytest.fixture()
def providers(unique_user: TestUser) -> Generator[dict[str, AIProviderOut]]:
    """A vision provider configured as the image provider, and a text provider as the default."""

    repos = unique_user.repos
    vision = repos.group_ai_providers.create(AIProviderCreate(name=f"Vision {random_string()}", model="v", api_key="k"))
    text = repos.group_ai_providers.create(AIProviderCreate(name=f"Text {random_string()}", model="t", api_key="k"))
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=text.id, audio_provider_id=None, image_provider_id=vision.id),
    )

    yield {"vision": vision, "text": text}

    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=None, audio_provider_id=None, image_provider_id=None),
    )
    repos.group_ai_providers.delete(vision.id)
    repos.group_ai_providers.delete(text.id)


@pytest.fixture()
def local_provider(unique_user: TestUser) -> Generator[AIProviderOut]:
    """A provider marked as running locally, at a loopback address"""
    clear_address_cache()
    provider = unique_user.repos.group_ai_providers.create(
        AIProviderCreate(
            name=f"Local {random_string()}",
            model="qwen3-vl",
            api_key="k",
            base_url="http://127.0.0.1:11434/v1",
            runs_locally=True,
        )
    )
    yield provider
    unique_user.repos.group_ai_providers.delete(provider.id)


def test_build_configs(unique_user: TestUser, providers: dict[str, AIProviderOut], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    repos = unique_user.repos
    vision, text = providers["vision"], providers["text"]

    [config] = ev.build_configs(repos, [], ocr=False)
    assert (config.label, config.image_provider, config.text_provider) == (vision.name, vision, vision)
    assert config.read_path == "image"
    assert config.local is False

    configs = ev.build_configs(repos, [text.name.upper(), vision.name], ocr=True)
    assert [(c.image_provider, c.text_provider) for c in configs] == [(text, text), (vision, vision), (None, text)]
    assert configs[-1].is_ocr
    assert configs[-1].read_path == "ocr"

    # --ocr-provider may repeat: one OCR config each
    ocr_configs = ev.build_configs(repos, [str(text.id)], ocr=True, ocr_provider_names=[vision.name, text.name])[1:]
    assert [(c.label, c.text_provider) for c in ocr_configs] == [
        (f"OCR+{vision.name}", vision),
        (f"OCR+{text.name}", text),
    ]

    # a provider named twice is evaluated once
    [config] = ev.build_configs(repos, [vision.name, vision.name.upper(), str(vision.id)], ocr=False)
    assert config.image_provider == vision

    # VISION:TEXT for mixed setups
    [mixed] = ev.build_configs(repos, [f"{vision.name}:{text.name}"], ocr=False)
    assert (mixed.label, mixed.image_provider, mixed.text_provider) == (f"{vision.name}:{text.name}", vision, text)

    with pytest.raises(ev.EvalSetupError, match="No AI provider named"):
        ev.build_configs(repos, ["no such provider"], ocr=False)
    with pytest.raises(ev.EvalSetupError, match="VISION:TEXT"):
        ev.build_configs(repos, [f"{vision.name}:no such provider"], ocr=False)


def test_build_configs_local_only(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    local_provider: AIProviderOut,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    repos = unique_user.repos

    configs = ev.build_configs(
        repos, [local_provider.name, providers["vision"].name], ocr=True, ocr_provider_names=[local_provider.name]
    )
    assert [(c.label, c.local) for c in configs] == [
        (local_provider.name, True),
        (providers["vision"].name, False),
        (f"OCR+{local_provider.name}", True),
    ]
    assert ev.EvalConfig.describe(configs[0])["local"] is True

    with pytest.raises(ev.EvalSetupError, match=f"--local-only: {providers['vision'].name}"):
        ev.build_configs(repos, [local_provider.name, providers["vision"].name], ocr=False, local_only=True)
    # a cloud text provider behind a local vision one isn't local either
    with pytest.raises(ev.EvalSetupError, match="--local-only"):
        ev.build_configs(repos, [f"{local_provider.name}:{providers['text'].name}"], ocr=False, local_only=True)
    assert len(ev.build_configs(repos, [local_provider.name], ocr=False, local_only=True)) == 1


def test_build_configs_needs_ocr_for_ocr(
    unique_user: TestUser, providers: dict[str, AIProviderOut], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ocr, "is_available", lambda: False)

    with pytest.raises(ev.EvalSetupError, match="OCR isn't available"):
        ev.build_configs(unique_user.repos, [], ocr=True)

    assert len(ev.build_configs(unique_user.repos, [], ocr=False)) == 1


def test_eval_runtime_applies_the_call_policy(
    unique_user: TestUser, providers: dict[str, AIProviderOut], local_provider: AIProviderOut
):
    cloud = ev.EvalOpenAIService(unique_user.repos, image_provider=providers["vision"], text_provider=providers["text"])
    local = ev.EvalOpenAIService(unique_user.repos, image_provider=local_provider, text_provider=local_provider)

    assert cloud.runtime.candidates(AIProviderSlot.image) == [providers["vision"]]
    with ai_call_policy(local_only=True):
        for slot in (AIProviderSlot.image, AIProviderSlot.default, AIProviderSlot.fast):
            with pytest.raises(AIProviderLocalOnlyError):
                cloud.runtime.candidates(slot)
            assert local.runtime.candidates(slot) == [local_provider]


def test_workflow_steps_read_the_card_one_way_only():
    provider = make_provider()
    provider_steps = ev.workflow_steps(
        ev.EvalConfig(label="Gemini", image_provider=provider, text_provider=provider), []
    )
    ocr_steps = ev.workflow_steps(ev.EvalConfig(label="OCR+Gemini", image_provider=None, text_provider=provider), [])

    def compilers(steps: list) -> list:
        [step] = [step for step in steps if isinstance(step, CompileSourceStep)]
        return [compiler.wrapped for compiler in step.compilers]

    # a provider that fails to read the card doesn't fall back to OCR
    assert compilers(provider_steps) == [ImageCompiler]
    assert compilers(ocr_steps) == [OCRImageCompiler]
    # every other step is upstream's
    others = [step for step in DEFAULT_WORKFLOW_STEPS if not isinstance(step, CompileSourceStep)]
    assert [step for step in provider_steps if not isinstance(step, CompileSourceStep)] == others
    assert [step for step in ocr_steps if not isinstance(step, CompileSourceStep)] == others


IMPORT = ev.EvalSettings(pipeline="import")


class StubAI:
    """Stands in for the AI provider, recording which providers each request would have used."""

    def __init__(self, recipe: OpenAIRecipe) -> None:
        self.recipe = recipe
        self.calls: list[dict] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> StubAI:
        stub = self

        async def get_response(self: OpenAIService, prompt, message, *, response_schema, attachments=None, **_):
            stub.calls.append(
                {
                    "schema": response_schema.__name__,
                    "image_provider": self.image_provider,
                    "default_provider": self.default_provider,
                    "has_images": any(isinstance(a, OpenAIImageBase) for a in attachments or []),
                    "message": message,
                }
            )
            if response_schema is OpenAICompiledSource:
                return OpenAICompiledSource(contains_recipe=True, content="Banana Mug Cake ...")
            if response_schema is OpenAIRecipe:
                return stub.recipe
            return None

        monkeypatch.setattr(OpenAIService, "get_response", get_response)
        return self


@pytest.fixture()
def openai_recipe() -> OpenAIRecipe:
    return OpenAIRecipe(
        name="Banana Mug Cake",
        description="Sugar free, gluten free",
        ingredients=[OpenAIRecipeIngredient(text=line) for line in EXPECTED.ingredient_lines],
        instructions=[OpenAIRecipeInstruction(text=text) for text in EXPECTED.instructions],
    )


def test_import_pipeline_with_provider(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    openai_recipe: OpenAIRecipe,
):
    ai = StubAI(openai_recipe).install(monkeypatch)
    card = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])[0]
    text = providers["text"]
    config = ev.EvalConfig(label=text.name, image_provider=text, text_provider=text)
    fixture_files = sorted(CARDS_DIR.iterdir())

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config, settings=IMPORT))

    assert result.error is None
    assert result.pipeline == "import"
    assert result.scores is not None and result.scores.overall > 0.95
    assert result.recipe is not None and result.recipe["name"] == "Banana Mug Cake"
    assert result.latency_s >= 0
    # every request ran on the provider under test, not the group's configured ones
    assert [call["schema"] for call in ai.calls] == ["OpenAICompiledSource", "OpenAIRecipe"]
    assert all(call["image_provider"] == text and call["default_provider"] == text for call in ai.calls)
    assert ai.calls[0]["has_images"]
    # nothing is written next to the fixtures
    assert sorted(CARDS_DIR.iterdir()) == fixture_files


def test_import_pipeline_records_errors(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    openai_recipe: OpenAIRecipe,
):
    # OCR would work, so a fallback to it would succeed
    ocr_reads: list[Path] = []

    def extract_text(path: Path, **_) -> ocr.OCRResult:
        ocr_reads.append(path)
        return ocr.OCRResult(text="Banana Mug Cake\n1 banana")

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", extract_text)

    # the provider fails to read images, but would answer anything else
    calls: list[bool] = []

    async def get_response(self: OpenAIService, prompt, message, *, response_schema, attachments=None, **_):
        has_images = any(isinstance(a, OpenAIImageBase) for a in attachments or [])
        calls.append(has_images)
        if has_images:
            raise RuntimeError("provider unreachable 401")
        if response_schema is OpenAICompiledSource:
            return OpenAICompiledSource(contains_recipe=True, content="Banana Mug Cake ...")
        return openai_recipe

    monkeypatch.setattr(OpenAIService, "get_response", get_response)
    card = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])[0]
    vision = providers["vision"]
    config = ev.EvalConfig(label=vision.name, image_provider=vision, text_provider=vision)

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config, settings=IMPORT))

    # the provider's own error is the result, not the workflow's "couldn't read anything"
    assert result.error == "ImageCompiler: RuntimeError: provider unreachable 401"
    assert result.scores is None
    assert result.score == 0.0
    # and it didn't fall back to OCR
    assert calls == [True]
    assert ocr_reads == []


def test_import_pipeline_records_ocr_errors(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    openai_recipe: OpenAIRecipe,
):
    def extract_text(_: Path, **__) -> ocr.OCRResult:
        raise OSError("tesseract crashed")

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", extract_text)
    ai = StubAI(openai_recipe).install(monkeypatch)
    card = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])[0]
    text = providers["text"]
    config = ev.EvalConfig(label=f"OCR+{text.name}", image_provider=None, text_provider=text)

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config, settings=IMPORT))

    assert result.error == "OCRImageCompiler: OSError: tesseract crashed"
    assert result.scores is None
    assert ai.calls == []


def test_import_pipeline_with_ocr(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    openai_recipe: OpenAIRecipe,
):
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", lambda *_, **__: ocr.OCRResult(text="Banana Mug Cake\n1 banana"))
    ai = StubAI(openai_recipe).install(monkeypatch)
    card = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])[0]
    text = providers["text"]
    config = ev.EvalConfig(label=f"OCR+{text.name}", image_provider=None, text_provider=text)

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config, settings=IMPORT))

    assert result.error is None
    assert ai.calls, "the OCR path should still ask the text provider to make sense of the text"
    assert all(call["image_provider"] is None and call["default_provider"] == text for call in ai.calls)
    assert not any(call["has_images"] for call in ai.calls)
    assert "1 banana" in ai.calls[0]["message"]


def mock_openai_api(monkeypatch: pytest.MonkeyPatch, answer: dict | None = None) -> list[str]:
    """
    Stands in for the OpenAI-compatible providers' API behind the real client, answering `answer` (or failing
    with a 500 when it's None) and reporting 120 prompt and 30 completion tokens. Returns the providers asked.
    """
    calls: list[str] = []
    completion = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "t",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(answer)}}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }
    get_client = OpenAIService.get_client

    def mocked_client(self: OpenAIService, provider: AIProviderOut) -> openai.AsyncOpenAI:
        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(provider.name)
            return httpx2.Response(200, json=completion) if answer else httpx2.Response(500, json={})

        transport = httpx2.MockTransport(handler)
        http_client = openai.DefaultAsyncHttpxClient(transport=transport)
        return get_client(self, provider).with_options(http_client=http_client, max_retries=0)

    monkeypatch.setattr(OpenAIService, "get_client", mocked_client)
    return calls


def usage_log(user: TestUser, *providers: AIProviderOut) -> list:
    ids = {provider.id for provider in providers}
    return [row for row in user.repos.group_ai_usage.get_all() if row.provider_id in ids]


def test_eval_service_tallies_token_usage(
    unique_user: TestUser, providers: dict[str, AIProviderOut], monkeypatch: pytest.MonkeyPatch
):
    mock_openai_api(monkeypatch, {"text": "hello"})
    text = providers["text"]
    ai = ev.EvalOpenAIService(unique_user.repos, image_provider=None, text_provider=text)

    async def ask_twice() -> list[OpenAIText | None]:
        return [await ai.get_response("prompt", "message", response_schema=OpenAIText) for _ in range(2)]

    responses = asyncio.run(ask_twice())

    assert [response.text if response else None for response in responses] == ["hello", "hello"]
    assert ai.usage == {text.name: ev.TokenUsage(requests=2, prompt_tokens=240, completion_tokens=60)}
    assert ai.usage_by_slot == {
        f"{text.name} [default]": ev.TokenUsage(requests=2, prompt_tokens=240, completion_tokens=60)
    }
    assert ai.models == [text.model]
    # An eval run isn't the group's real usage
    assert usage_log(unique_user, text) == []


def test_eval_service_records_prompt_hashes(unique_user: TestUser, providers: dict[str, AIProviderOut]):
    import hashlib

    ai = ev.EvalOpenAIService(unique_user.repos, image_provider=None, text_provider=providers["text"])

    prompt = ai.get_prompt("recipes.parse-recipe-ingredients")

    assert ai.prompt_hashes == {"recipes.parse-recipe-ingredients": hashlib.sha256(prompt.encode()).hexdigest()}


def test_eval_service_tallies_claude_tokens(unique_user: TestUser, monkeypatch: pytest.MonkeyPatch):
    async def get_response(prompt, message, *, response_schema, provider, attachments=None, usage=None):
        usage.prompt_tokens, usage.completion_tokens = 300, 70
        usage.model = "claude-fallback"
        return response_schema(text="hello"), usage

    monkeypatch.setattr(anthropic_adapter, "get_response", get_response)
    claude = AIProviderOut(id=uuid4(), name="Claude", model="c", api_key="k", protocol=AIProviderProtocol.anthropic)
    ai = ev.EvalOpenAIService(unique_user.repos, image_provider=None, text_provider=claude)

    asyncio.run(ai.get_response("prompt", "message", response_schema=OpenAIText))

    assert ai.usage == {"Claude": ev.TokenUsage(requests=1, prompt_tokens=300, completion_tokens=70)}
    # the model that answered, not the configured one
    assert ai.models == ["claude-fallback"]


@pytest.fixture()
def default_route(unique_user: TestUser, providers: dict[str, AIProviderOut]) -> Generator[AIProviderOut]:
    """A provider the group falls back to when its default provider fails"""
    repos = unique_user.repos
    backup = repos.group_ai_providers.create(AIProviderCreate(name=f"Backup {random_string()}", model="b", api_key="k"))
    repos.group_ai_provider_routes.replace_routes({AIProviderSlot.default: [backup.id]})

    yield backup

    repos.group_ai_providers.delete(backup.id)


@pytest.mark.parametrize("slot", [None, AIProviderSlot.fast])
def test_eval_service_never_falls_back_to_the_groups_providers(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    default_route: AIProviderOut,
    monkeypatch: pytest.MonkeyPatch,
    slot: AIProviderSlot | None,
):
    """A run scores the provider under test, even when it fails"""
    calls = mock_openai_api(monkeypatch, answer=None)
    text = providers["text"]
    ai = ev.EvalOpenAIService(unique_user.repos, image_provider=None, text_provider=text)

    with pytest.raises(Exception, match="OpenAI Request Failed"):
        asyncio.run(ai.get_response("prompt", "message", response_schema=OpenAIText, slot=slot))

    assert calls == [text.name]
    assert usage_log(unique_user, text, default_route) == []
    assert ai.usage[text.name].failures == 1


def test_eval_service_without_a_provider_for_the_slot(unique_user: TestUser, providers: dict[str, AIProviderOut]):
    ai = ev.EvalOpenAIService(unique_user.repos, image_provider=None, text_provider=providers["text"])

    with pytest.raises(OpenAINotEnabledException, match="No image provider set"):
        ai.runtime.candidates(AIProviderSlot.image)


# ----------------------------------------------------------------
# The card pipeline, with `extract_card` stood in for (the real banana replay is
# tests/unit_tests/services_tests/ai/ingest/test_banana_replay.py)


class FakePipeline:
    """
    Stands in for `pipeline.extract_card`, reading the card as production does for `options.read_path`: the image
    slot, then (only for `image_then_ocr`) the OCR fallback; a cross-read on the same `ai` when asked. Records the
    options and pages it was given.
    """

    def __init__(self, draft: CardDraft | None = None) -> None:
        self.draft = draft or make_draft()
        self.options: list[ev.CardPipelineOptions] = []
        self.pages: list[list[ev.CardPage]] = []
        self.ocr_reads = 0

    async def __call__(self, pages, *, ai, repos, translator, options, on_progress=None) -> ev.CardExtraction:
        self.options.append(options)
        self.pages.append(pages)
        assert all(page.view_path.is_file() for page in pages)
        if on_progress:
            await on_progress("recipe-ingest.progress.reading-card")

        read_path = None
        error: Exception | None = None
        if options.read_path in ("image", "image_then_ocr"):
            try:
                await ai.get_response("prompt", "read the card", response_schema=OpenAIText, slot=AIProviderSlot.image)
                read_path = IngestReadPath.image
            except Exception as e:
                error = e
        if read_path is None and options.read_path in ("ocr", "image_then_ocr"):
            self.ocr_reads += 1
            ocr.extract_text(pages[0].page_path)
            await ai.get_response("prompt", "structure the OCR text", response_schema=OpenAIText)
            read_path = IngestReadPath.ocr
        if read_path is None:
            assert error is not None
            raise error
        if options.cross_read and ai.image_provider is not None:
            await ai.get_response("prompt", "transcribe", response_schema=OpenAIText, slot=AIProviderSlot.image)

        flag = make_flag(CardFlagKind.blank, "steps", self.draft.steps[1].id)
        return ev.CardExtraction(
            draft=self.draft,
            flags=[flag],
            transcription="Banana Mug Cake",
            extraction=ExtractionMeta(read_path=read_path, cross_read_lines=[] if options.cross_read else None),
        )


@pytest.fixture()
def card_pipeline(monkeypatch: pytest.MonkeyPatch) -> FakePipeline:
    fake = FakePipeline()
    monkeypatch.setattr(ev, "extract_card", fake)
    monkeypatch.setattr(ocr, "is_available", lambda: False)  # no OCR fallback
    monkeypatch.setattr(ocr, "binary_available", lambda: False)  # and no orientation probe
    return fake


def row_counts(user: TestUser) -> tuple[int, ...]:
    repos = user.repos
    return (
        len(repos.ingredient_foods.get_all()),
        len(repos.ingredient_units.get_all()),
        len(repos.recipes.get_all()),
        len(repos.group_ai_usage.get_all()),
    )


def test_card_pipeline_scores_the_production_pipeline(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    card_pipeline: FakePipeline,
):
    calls = mock_openai_api(monkeypatch, {"text": "ok"})
    [card] = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    vision = providers["vision"]
    config = ev.EvalConfig(label=vision.name, image_provider=vision, text_provider=vision)
    fixture_files = sorted(CARDS_DIR.iterdir())

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config))

    assert result.error is None
    assert result.pipeline == "card"
    assert result.read_path == "image"
    assert [options.read_path for options in card_pipeline.options] == ["image"]
    assert result.scores is not None and result.scores.blanks_safe == 1.0
    assert result.scores.calibration is not None
    assert result.flags == [f"blank:steps:{card_pipeline.draft.steps[1].id} (error)"]
    assert result.progress[0][1] == "recipe-ingest.progress.reading-card"
    assert result.usage_by_slot == {
        f"{vision.name} [image]": ev.TokenUsage(requests=1, prompt_tokens=120, completion_tokens=30)
    }
    assert calls == [vision.name]
    # the pages it read were normalized copies, never the fixtures
    [pages] = card_pipeline.pages
    assert pages[0].meta.raw_sha256 and CARDS_DIR not in pages[0].dir.parents
    assert sorted(CARDS_DIR.iterdir()) == fixture_files


def test_a_failed_vision_read_is_an_error_never_an_ocr_result(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    card_pipeline: FakePipeline,
):
    calls = mock_openai_api(monkeypatch, answer=None)
    ocr_reads: list[Path] = []
    monkeypatch.setattr(ocr, "extract_text", lambda path, **_: ocr_reads.append(path) or ocr.OCRResult(text="x"))
    [card] = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    vision = providers["vision"]
    config = ev.EvalConfig(label=vision.name, image_provider=vision, text_provider=vision)

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config))

    assert result.error is not None and "OpenAI Request Failed" in result.error
    assert result.scores is None and result.read_path is None
    assert not result.refused
    assert card_pipeline.options[0].read_path == "image"
    assert card_pipeline.ocr_reads == 0 and ocr_reads == []
    assert calls == [vision.name]


def test_an_eval_run_with_cross_read_writes_nothing_and_calls_only_the_pinned_provider(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    default_route: AIProviderOut,
    monkeypatch: pytest.MonkeyPatch,
    card_pipeline: FakePipeline,
):
    calls = mock_openai_api(monkeypatch, {"text": "ok"})
    monkeypatch.setattr(ocr, "extract_text", lambda *_, **__: ocr.OCRResult(text="Banana Mug Cake"))
    cards = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    vision, text = providers["vision"], providers["text"]
    configs = [
        ev.EvalConfig(label=vision.name, image_provider=vision, text_provider=vision),
        ev.EvalConfig(label=f"OCR+{text.name}", image_provider=None, text_provider=text),
    ]
    before = row_counts(unique_user)
    settings = ev.EvalSettings(cross_read=True, group_options=ev.CardPipelineOptions(suggest_organizers=False))

    results, prepared = asyncio.run(ev.run_eval(unique_user.repos, cards, configs, repeat=2, settings=settings))

    assert [(r.label, r.attempt, r.error) for r in results] == [
        (vision.name, 1, None),
        (vision.name, 2, None),
        (f"OCR+{text.name}", 1, None),
        (f"OCR+{text.name}", 2, None),
    ]
    # cross-read for the vision config only (an OCR config has no image provider), and the group's other options kept
    assert [(o.read_path, o.cross_read, o.suggest_organizers) for o in card_pipeline.options] == [
        ("image", True, False),
        ("image", True, False),
        ("ocr", False, False),
        ("ocr", False, False),
    ]
    # the card read and cross-read on the vision provider, the OCR text on the text provider; never the fallback route
    assert calls == [vision.name] * 4 + [text.name] * 2
    assert default_route.name not in calls
    assert row_counts(unique_user) == before
    assert usage_log(unique_user, vision, text, default_route) == []
    assert list(prepared) == ["banana-mug-cake"]


def test_a_local_only_card_is_refused_by_the_policy(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    card_pipeline: FakePipeline,
):
    calls = mock_openai_api(monkeypatch, {"text": "ok"})
    [banana] = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    card = ev.Card(id=banana.id, images=banana.images, fixture=banana.fixture.model_copy(update={"local_only": True}))
    vision = providers["vision"]
    config = ev.EvalConfig(label=vision.name, image_provider=vision, text_provider=vision)

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config))

    assert result.refused
    assert result.error is not None and result.error.startswith("AIProviderLocalOnlyError")
    assert calls == []
    # refused runs are reported apart, not scored as failures
    summary = ev.summarize_runs(vision.name, "v", [result])
    assert (summary.runs, summary.errors, summary.refused) == (0, 0, 1)

    # --local-only does the same for every card
    result = asyncio.run(
        ev.run_card(
            unique_user.repos, get_locale_provider("en-US"), banana, config, settings=ev.EvalSettings(local_only=True)
        )
    )
    assert result.refused and calls == []


def test_a_local_only_card_reaches_a_local_provider(
    unique_user: TestUser, local_provider: AIProviderOut, monkeypatch: pytest.MonkeyPatch, card_pipeline: FakePipeline
):
    calls = mock_openai_api(monkeypatch, {"text": "ok"})
    [banana] = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    card = ev.Card(id=banana.id, images=banana.images, fixture=banana.fixture.model_copy(update={"local_only": True}))
    config = ev.EvalConfig(label="local", image_provider=local_provider, text_provider=local_provider, local=True)

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config))

    assert result.error is None and not result.refused
    assert calls == [local_provider.name]


def test_self_drafted_cards_are_marked():
    [banana] = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])
    origin = ev.FixtureOrigin(drafted_by=ev.FixtureDraftedBy(provider="Gemini", model="gemini-flash"))
    card = ev.Card(id="x", images=banana.images, fixture=banana.fixture.model_copy(update={"origin": origin}))
    gemini, other = make_provider("Gemini"), make_provider("Claude", model="claude")

    assert ev.is_self_drafted(card, ev.EvalConfig(label="Gemini", image_provider=gemini, text_provider=gemini))
    assert not ev.is_self_drafted(card, ev.EvalConfig(label="Claude", image_provider=other, text_provider=other))
    assert not ev.is_self_drafted(banana, ev.EvalConfig(label="Gemini", image_provider=gemini, text_provider=gemini))


def test_main_runs_the_card_pipeline_and_writes_the_report(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    card_pipeline: FakePipeline,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    calls = mock_openai_api(monkeypatch, {"text": "ok"})
    group = unique_user.repos.groups.get_one(unique_user.group_id)
    assert group
    vision, text = providers["vision"].name, providers["text"].name
    mixed = f"{vision}:{text}"
    out = tmp_path / "report.json"

    ev.main(
        [
            *("--group", group.slug, "--card", "banana-mug-cake", "--repeat", "2", "--out", str(out)),
            *("--provider", vision, "--provider", mixed, "--chain", f"{vision}>{mixed}", "--baseline", vision),
            *("--price", f"{vision}=1,2", "--price", f"{text}=1,2"),
        ]
    )

    printed = capsys.readouterr().out
    assert f"{vision}>{mixed}" in printed
    assert "Paired by card" in printed
    assert "Decisions" in printed and "D3 orientation" in printed
    report = json.loads(out.read_text())
    assert report["pipeline"] == "card"
    assert report["chains"] == [f"{vision}>{mixed}"]
    assert [config["label"] for config in report["configs"]] == [vision, mixed]
    assert [summary["label"] for summary in report["summary"]] == [vision, mixed, f"{vision}>{mixed}"]
    assert [c["label"] for c in report["comparisons"]] == [mixed, f"{vision}>{mixed}"]
    assert report["decisions"]["targets"][0]["banana_blank_safe"] is None  # needs 3 repeats
    assert len(report["runs"]) == 2 + 2 + 2
    assert all(run["cost_usd"] is not None for run in report["runs"])
    # the mixed config read the card with the vision provider (no cross-read: the group's setting is off)
    assert calls == [vision] * 4
    assert [options.read_path for options in card_pipeline.options] == ["image"] * 4


# ================================================================
# Before spending: --check offline, secrets from files, --dry-run, and the ranking and cost columns


def test_check_needs_no_production_setting(tmp_path: Path):
    """`--check` imports no settings or database, so it runs from a checkout with nothing set"""
    env = {key: value for key, value in os.environ.items() if key not in ("PRODUCTION", "TESTING", "DATA_DIR")}
    run = subprocess.run(
        [sys.executable, "-m", "mealie.scripts.eval_recipe_cards", "--check", "--cards", str(CARDS_DIR)],
        cwd=Path(__file__).parents[2],
        env={**env, "DATA_DIR": str(tmp_path / "data")},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert run.returncode == 0, run.stderr
    assert "1 card(s)" in run.stdout and "are valid" in run.stdout
    assert "PRODUCTION" not in run.stderr


def test_secrets_are_read_from_their_files_before_the_settings(tmp_path: Path):
    """
    `docker exec` skips entry.sh: the eval reads `NAME_FILE` itself, and as entry.sh does, the secret wins over a
    `NAME` also set (a stale one would send the eval to another database than the server's); an empty `NAME_FILE`
    leaves `NAME` as it is, the way to override a secret
    """
    password = tmp_path / "postgres_password"
    password.write_text("s3cret\n")
    user = tmp_path / "postgres_user"
    user.write_text("mealie")
    env = {
        "POSTGRES_PASSWORD_FILE": str(password),
        "POSTGRES_USER_FILE": str(user),
        "POSTGRES_USER": "stale",
        "POSTGRES_DB_FILE": "",
        "POSTGRES_DB": "given",
        "UNRELATED_FILE": str(user),
    }

    assert ev.load_secret_files(env) == ["POSTGRES_USER", "POSTGRES_PASSWORD"]
    assert (env["POSTGRES_PASSWORD"], env["POSTGRES_USER"], env["POSTGRES_DB"]) == ("s3cret", "mealie", "given")
    assert "UNRELATED" not in env

    with pytest.raises(ev.EvalSetupError, match="POSTGRES_DB_FILE"):
        ev.load_secret_files({"POSTGRES_DB_FILE": str(tmp_path / "missing")})

    # run as a script, before anything else: a secret that can't be read stops it at once
    env = {key: value for key, value in os.environ.items() if key not in ("PRODUCTION", "POSTGRES_DB")}
    run = subprocess.run(
        [sys.executable, "-m", "mealie.scripts.eval_recipe_cards", "--check", "--cards", str(CARDS_DIR)],
        cwd=Path(__file__).parents[2],
        env={**env, "POSTGRES_DB_FILE": str(tmp_path / "missing")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 2 and "POSTGRES_DB_FILE" in run.stderr + run.stdout


def _no_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_response(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a dry run calls no provider")

    async def run_eval(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a dry run runs nothing")

    monkeypatch.setattr(OpenAIService, "get_response", get_response)
    monkeypatch.setattr(ev, "run_eval", run_eval)


def test_a_dry_run_checks_everything_and_calls_no_provider(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _no_provider_calls(monkeypatch)
    group = unique_user.repos.groups.get_one(unique_user.group_id)
    assert group
    vision, text = providers["vision"].name, providers["text"].name

    ev.main(
        [
            *("--group", group.slug, "--provider", vision, "--cards", str(CARDS_DIR), "--dry-run"),
            *("--price", f"{vision}=1,2", "--price", f"{text}=0.5,1"),
        ]
    )

    out = capsys.readouterr().out
    assert f"Dry run: 1 card(s), 1 config(s) ({vision})" in out
    assert "No problems found" in out


def test_a_dry_run_lists_every_problem_and_exits_1(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _no_provider_calls(monkeypatch)
    group = unique_user.repos.groups.get_one(unique_user.group_id)
    assert group
    vision = providers["vision"].name

    with pytest.raises(SystemExit) as exit_info:
        ev.main(
            [
                *("--group", group.slug, "--cards", str(CARDS_DIR), "--dry-run"),
                *("--provider", "No Such Vision", "--provider", vision, "--price", "Elsewhere=1,1"),
            ]
        )

    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "No AI provider named 'No Such Vision'" in out
    assert f"No --price for {vision}" in out
    assert "--price for Elsewhere, which no config uses" in out


def test_the_dry_run_checks_each_slot_under_the_local_only_policy(
    unique_user: TestUser, providers: dict[str, AIProviderOut], monkeypatch: pytest.MonkeyPatch
):
    """A config whose provider the run's policy refuses is reported before anything is spent"""
    _no_provider_calls(monkeypatch)
    group = unique_user.repos.groups.get_one(unique_user.group_id)
    assert group
    config = ev.EvalConfig(label="V", image_provider=providers["vision"], text_provider=providers["text"])
    monkeypatch.setattr(ev, "build_configs", lambda *args, **kwargs: [config])
    args = ev.parse_args(["--group", group.slug, "--cards", str(CARDS_DIR), "--dry-run", "--local-only"])

    problems, _ = ev.dry_run(args)

    assert [problem.split(":")[0] for problem in problems] == ["V", "V", "V"]
    assert all("can't be asked" in problem for problem in problems)


def test_flag_auroc_ranks_wrong_items_by_their_highest_flag():
    def item(correct: bool, severity: int) -> ev.CalibrationItem:
        return ev.CalibrationItem(kind="ingredient", correct=correct, flagged=severity >= 2, severity=severity)

    # every wrong item flagged above every right one
    assert ev.flag_auroc([item(False, 3), item(False, 2), item(True, 0), item(True, 1)]) == 1.0
    # no better than chance: the same scores
    assert ev.flag_auroc([item(False, 2), item(True, 2)]) == 0.5
    # one of two wrong items silent among right ones: half the pairs above, the rest tied
    assert ev.flag_auroc([item(False, 3), item(False, 0), item(True, 0), item(True, 0)]) == 0.75
    # both classes are needed
    assert ev.flag_auroc([item(True, 0)]) is None and ev.flag_auroc([item(False, 3)]) is None


def test_the_summary_has_the_auroc_and_the_cost_per_caught_error():
    expected = banana_expected()
    wrong = make_draft(ingredients=[*expected.ingredient_lines[:6], "2 t. cinnamon"])
    flag = make_flag(CardFlagKind.not_on_card, "ingredients", wrong.ingredients[6].reference_id)
    runs = [
        run_with(ev.score_card(expected, make_extraction(wrong, [flag])), card="a", label="V", cost_usd=0.02),
        run_with(ev.score_card(expected, make_extraction(wrong)), card="b", label="V", cost_usd=0.04),
    ]

    summary = ev.summarize_runs("V", "v", runs)

    # each run has a wrong line and a missing one; only the first run's wrong line is flagged (a warning)
    assert (summary.cards, summary.auroc_items, summary.auroc_wrong, summary.caught) == (2, 22, 4, 1)
    assert summary.auroc == pytest.approx((18 + 0.5 * 3 * 18) / (4 * 18))
    assert summary.cost_per_caught == pytest.approx(0.06)
    # a run without a price: no cost per caught error
    runs[1].cost_usd = None
    assert ev.summarize_runs("V", "v", runs).cost_per_caught is None
