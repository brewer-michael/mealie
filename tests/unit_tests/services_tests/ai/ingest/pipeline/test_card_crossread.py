"""The second reading's alignment and comparison (docs/ai/PHASE2.md §4.5), and the card text it reads"""

import textwrap
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from rapidfuzz import fuzz

from mealie.schema.recipe_ingest import CardDraft, CardDraftIngredient, CardDraftStep, CardFlagSource, ExtractionMeta
from mealie.services.ai.ingest.pipeline import crossread
from mealie.services.ai.ingest.pipeline.cardtext import (
    canonical_markers,
    card_numbers,
    find_numbers,
    find_temperatures,
    format_number,
    letters_only,
    markers_in,
    number_set,
    salient_tokens,
)
from mealie.services.ai.ingest.pipeline.crossread import (
    STEP_MAX_LINES,
    CrossReadFailed,
    align_ingredient,
    align_step,
    compare,
    read_transcript,
    transcript_lines,
)
from mealie.services.ai.ingest.pipeline.flags import compute_flags
from mealie.services.ai.ingest.pipeline.service import JobOpenAIService
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    BANANA_TRANSCRIPT,
    FakeCardAI,
    banana_answers,
    configure,
    create_provider,
    job_session,
    make_pages,
)
from tests.utils.fixture_schemas import TestUser

LINES = transcript_lines(BANANA_TRANSCRIPT["text"])


def test_numbers_read_the_same_however_they_are_written():
    assert number_set("1 1/2 cups") == number_set("1½ c") == {Fraction(3, 2), Fraction(1), Fraction(1, 2)}
    assert number_set("0.5 tsp") == number_set("½ tsp") == number_set("1/2 tsp")
    assert [number.text for number in find_numbers("Bake 20-25 minutes, or 2 to 3 more")] == ["20-25", "2 to 3"]
    assert find_numbers("2 tomatoes")[0].end is None
    assert format_number(find_numbers("11/2")[0].value) == "5 1/2"


def test_a_mixed_number_written_with_a_dash_is_one_number():
    """Printed recipes write 2 1/4 cups as "2-1/4 c.": a range never goes down to a proper fraction"""
    (mixed,) = find_numbers("2-1/4 c. flour")
    assert (mixed.value, mixed.end, mixed.text, mixed.span) == (Fraction(9, 4), None, "2-1/4", (0, 5))
    assert [(number.value, number.text) for number in find_numbers("Bake 1 - 1/2 hrs, or 1–1/2 to 2")] == [
        (Fraction(3, 2), "1 - 1/2"),
        (Fraction(3, 2), "1–1/2 to 2"),
    ]
    assert find_numbers("1/2-3/4 c. sugar")[0].end == Fraction(3, 4)  # ranges go up
    assert find_numbers("2-3 lb. roast")[0].end == 3
    # the two readings agree however the mixed number is written
    assert salient_tokens("2-1/4 c. flour") == salient_tokens("2 1/4 c. flour") == [("number", "9/4"), ("unit", "cup")]
    assert compare("2-1/4 c. flour", "2 1/4 c. flour") is None


def test_markers_and_temperatures():
    assert canonical_markers("a [ Illegible ] b [BLANK]") == "a [illegible] b [blank]"
    assert markers_in("for [blank] min, [illegible]") == ["blank", "illegible"]
    found = [(t.value, t.unit) for t in find_temperatures("350°, 180 °C, 400 degrees F, 425F, 12 C. flour")]
    assert found == [(350, None), (180, "C"), (400, "F"), (425, "F")]
    # a digit too many is still a temperature, and flagged as one
    assert [(t.value, t.unit, t.text) for t in find_temperatures("Bake at 3500°F")] == [(3500, "F", "3500°F")]


def test_list_numbers_are_not_numbers_on_the_card():
    """A transcription in markdown may number the steps; "2." isn't an amount, so an invented 2 isn't on the card"""
    transcription = "## Directions\n1. Mash banana and mix.\n2. Microwave for [blank] minutes.\n10) Serve."
    assert card_numbers(transcription) == set()
    assert number_set(transcription) == {1, 2, 10}

    card = "- 1 banana\n- 1/4 t. salt\n1.5 c. milk\n2 eggs\nBake 1 hr. 2. Cool"
    assert card_numbers(card) == {Fraction(1), Fraction(1, 4), Fraction(3, 2), Fraction(2)}


def test_salient_tokens_keep_case_sensitive_units_after_a_number():
    assert salient_tokens("1 T. coconut oil") == [("number", "1"), ("unit", "tbsp")]
    assert salient_tokens("1/4 t. salt, don't stir") == [("number", "1/4"), ("unit", "tsp")]
    assert salient_tokens("Bake 20-25 min at 350°") == [("range", "20", "25"), ("number", "350")]
    assert salient_tokens("Microwave for [blank] minutes") == [("marker", "blank")]


def test_an_ingredient_aligns_with_its_own_line():
    assert LINES[align_ingredient("1 banana", LINES)] == "1 banana"  # type: ignore[index]
    assert LINES[align_ingredient("1 tbsp coconut oil melted", LINES)] == "1 T. coconut oil (melted)"  # type: ignore[index]
    assert align_ingredient("2 cups chopped walnuts", LINES) is None
    assert align_ingredient("2", LINES) is None

    # every line of the banana card, though "banana" is also in the step "Mash banana and mix ingredients"
    for line in ["1 banana", "1 T. coconut oil (melted)", "1/4 t. salt", "1/2 t vanilla", "1/3 C. almond flour"]:
        assert LINES[align_ingredient(line, LINES)] == line  # type: ignore[index]
    assert LINES[align_ingredient("1 egg", LINES)] == "1 egg"  # type: ignore[index]
    assert LINES[align_ingredient("Cinnamon to taste", LINES)] == "Cinnamon to taste"  # type: ignore[index]


@pytest.mark.parametrize(
    ("line", "transcript"),
    [
        ("2 eggs", ["Sugar Cookies", "1 c. shortening", "2 egg", "1 c. sugar", "Cream shortening and sugar. Add eggs"]),
        ("3 bananas", ["3 banannas", "Mash bananas"]),
        ("1 onion", ["1 onoin", "Saute onion in butter"]),
        ("2 egg whites", ["2 eggwhites", "Fold in egg whites"]),
        ("1 banana", ["1 bananna", "Mash banana and mix ingredients"]),
        ("2 eggs", ["2 eggs, beaten", "Add eggs"]),  # a line that says more still aligns
        ("2 eggs", ["2 egg", "2. Add eggs and beat well."]),  # a numbered step's "2." isn't an amount
    ],
)
def test_an_ingredient_read_differently_aligns_with_its_line_not_a_step_that_names_it(line: str, transcript: list[str]):
    """Every word of a short ingredient is in a step line too; the ingredient's own line, one letter off, still wins"""
    index = align_ingredient(line, transcript)
    assert index is not None and transcript[index].split()[0][0].isdigit()
    assert compare(line, transcript[index]) is None


@pytest.mark.parametrize(
    ("line", "transcript", "expected"),
    [
        # the second reading lost the amount: the ingredient's own line, not another that holds all its words
        ("1 c. sugar", ["c. sugar", "1 c. brown sugar"], "c. sugar"),
        ("1 t. salt", ["salt", "1 t. garlic salt"], "salt"),
        ("1 c. milk", ["c. milk", "1 c. buttermilk"], "c. milk"),
        ("1 c. sugar", ["c. sugar", "2 c. brown sugar"], "c. sugar"),
        # a line that says more is still the ingredient's, and a step that names it still isn't
        ("2 eggs", ["2 eggs, beaten", "Add eggs"], "2 eggs, beaten"),
        ("1 c. butter", ["c. butter", "1 c. flour"], "c. butter"),
        ("2 eggs", ["Eggs", "2 eggs"], "2 eggs"),  # a line that reads the same with the amount wins
    ],
)
def test_an_ingredient_read_without_its_amount_aligns_with_its_own_line(
    line: str, transcript: list[str], expected: str
):
    index = align_ingredient(line, transcript)
    assert index is not None and transcript[index] == expected


CAKE_AND_FROSTING = ["Cake", "1 c. sugar", "2 eggs", "Frosting", "1/2 c. sugar", "2 T. butter"]


def test_lines_that_read_alike_align_in_order():
    """By letters, "1 c. sugar" and "1/2 c. sugar" read the same: the first one after the line before wins"""
    assert align_ingredient("1 c. sugar", CAKE_AND_FROSTING) == 1
    assert align_ingredient("1/2 c. sugar", CAKE_AND_FROSTING) == 1  # alone, the first
    assert align_ingredient("1/2 c. sugar", CAKE_AND_FROSTING, after=2) == 4
    assert align_ingredient("1 c. sugar", CAKE_AND_FROSTING, after=4) == 1  # none after: the first again
    assert align_ingredient("2 T. butter", CAKE_AND_FROSTING, after=4) == 5


def _ingredient(text: str) -> CardDraftIngredient:
    return CardDraftIngredient(original_text=text, note=text)


def _cross_read(ingredients: list[str], transcript: list[str]) -> list[tuple[int, str, dict, list[str]]]:
    """The cross-read's flags on a draft of these ingredient lines: (the line's position, kind, params, alternatives)"""
    draft = CardDraft(
        name="Cake", ingredients=[_ingredient(text) for text in ingredients], steps=[CardDraftStep(text="Mix.")]
    )
    positions = {str(line.reference_id): position for position, line in enumerate(draft.ingredients)}
    flags = compute_flags(draft, ExtractionMeta(language="English", cross_read_lines=transcript), {})
    return [
        (positions[flag.ref or ""], flag.kind.value, flag.params, flag.alternatives)
        for flag in flags
        if flag.source == CardFlagSource.cross_read
    ]


def test_the_same_food_in_two_sections_is_compared_with_its_own_line():
    # both readings agree: the frosting's "1/2 c. sugar" isn't held against the cake's "1 c. sugar"
    assert _cross_read(["1 c. sugar", "2 eggs", "1/2 c. sugar", "2 T. butter"], CAKE_AND_FROSTING) == []
    assert _cross_read(["1/2 c. sugar", "1 c. sugar"], ["Frosting", "1/2 c. sugar", "Cake", "1 c. sugar"]) == []
    assert (
        _cross_read(["1/2 tsp. salt", "1 c. flour", "1/4 tsp. salt"], ["1/2 tsp. salt", "1 c. flour", "1/4 tsp. salt"])
        == []
    )

    # the main reading copied the cake's amount into the frosting: the second reading's frosting line says otherwise
    assert _cross_read(["1 c. sugar", "2 eggs", "1 c. sugar", "2 T. butter"], CAKE_AND_FROSTING) == [
        (2, "read_disagreement", {"text": "1/2 c. sugar", "value": "1"}, ["1/2 c. sugar"])
    ]


def test_an_amount_the_second_reading_lacks_is_flagged_on_the_ingredients_own_line():
    assert _cross_read(["1 c. sugar", "1 c. brown sugar"], ["c. sugar", "1 c. brown sugar"]) == [
        (0, "read_disagreement", {"text": "c. sugar", "value": "1"}, ["c. sugar"])
    ]


def test_a_wrapped_step_aligns_with_its_whole_window():
    step = "Microwave in bowl or large mug for 2 minutes or until firm in center."
    start, end = align_step(step, LINES)  # type: ignore[misc]

    assert LINES[start:end] == [
        "thoroughly. Microwave in bowl or large",
        "mug for [blank] minutes or until firm",
        "in center.",
    ]
    assert align_step("Whisk the cream until stiff peaks form.", LINES) is None
    assert LINES[slice(*align_step("Mash banana and mix ingredients thoroughly.", LINES))] == [  # type: ignore[misc]
        "Mash banana and mix ingredients"
    ]


MUG = "Microwave in bowl or large mug for 2 minutes or until firm in center."


def test_a_step_aligns_with_its_whole_window_when_one_word_reads_differently():
    """
    The second read has a gap where the step has a number, and one word on that line differs ("min." for
    "minutes"): the whole window still aligns, so the gap is seen, not just "in center." (which reads word for word)
    """
    lines = [
        "Mash banana and mix ingredients",
        "thoroughly. Microwave in bowl or lg",
        "mug for [blank] min. or until firm",
    ]
    lines.append("in center.")

    window = align_step(MUG, lines)

    assert window == (1, 4)
    disagreement = compare(MUG, " ".join(lines[1:4]))
    assert disagreement is not None and disagreement.window_blank and disagreement.missing == [("number", "2")]


def test_a_correct_step_read_with_other_abbreviations_agrees():
    lines = ["Cream butter and sugar.", "Bake at 350 for 30 min.,", "until golden brown on top."]
    step = "Bake at 350 for 30 minutes, until golden brown on top."

    window = align_step(step, lines)

    assert window == (1, 3)
    assert compare(step, " ".join(lines[1:3])) is None


def test_a_step_longer_than_four_lines_aligns_whole():
    lines = [
        "1 c. sugar",
        "Preheat oven to 350.",
        "Cream the butter and",
        "sugar, add the eggs one",
        "at a time and beat well,",
        "then fold in the flour",
        "and bake 30 minutes.",
        "Cool on a rack.",
    ]
    step = (
        "Preheat oven to 350. Cream the butter and sugar, add the eggs one at a time and beat well, then fold in the "
        "flour and bake 30 minutes."
    )

    assert align_step(step, lines) == (1, 7)
    assert compare(step, " ".join(lines[1:7])) is None


# A printed page: long steps, each wrapped over many lines of the second reading
PAGE_STEPS = [
    "Preheat the oven to 350 degrees and grease a 9 by 5 inch loaf pan. In a large bowl, cream 1/2 cup of softened "
    "butter with 3/4 cup of brown sugar for about 3 minutes, until light and fluffy. Beat in 2 eggs one at a time, "
    "scraping down the bowl after each, then stir in 1 teaspoon of vanilla and 3 ripe mashed bananas until just "
    "blended; a few small lumps of banana are fine.",
    "In a separate bowl, whisk together 2 cups of flour, 1 teaspoon of baking soda, 1/2 teaspoon of salt and 1/2 "
    "teaspoon of cinnamon. Add the dry ingredients to the banana mixture in three parts, alternating with 1/3 cup of "
    "buttermilk, and mix on low speed only until no streaks of flour remain. Fold in 3/4 cup of chopped walnuts.",
    "Scrape the batter into the pan and smooth the top. Bake on the middle rack for 55 to 65 minutes, until a skewer "
    "inserted into the center comes out clean. If the top browns too quickly, tent it loosely with foil for the last "
    "15 minutes. Cool in the pan for 10 minutes, then turn out onto a rack and cool completely, at least 1 hour.",
]
PAGE_INGREDIENTS = ["1/2 c. butter", "3/4 c. brown sugar", "2 eggs", "1 tsp. vanilla", "3 bananas", "2 c. flour"]
PAGE = ["Banana Bread", *PAGE_INGREDIENTS, *(line for step in PAGE_STEPS for line in textwrap.wrap(step, 36))]
TYPED_STEP = (
    "Meanwhile toast the pecans in a dry skillet over medium heat, stirring often, for 5 to 7 minutes until fragrant "
    "and a shade darker; let them cool and chop them coarsely before folding them in at the very end."
)


class _CountingFuzz:
    """rapidfuzz's scorers, keeping the windows `partial_ratio` is given"""

    def __init__(self) -> None:
        self.partial_windows: list[str] = []

    def partial_ratio(self, target: str, window: str, **kwargs: Any) -> float:
        self.partial_windows.append(window)
        return fuzz.partial_ratio(target, window, **kwargs)

    def ratio(self, *args: Any, **kwargs: Any) -> float:
        return fuzz.ratio(*args, **kwargs)

    def token_set_ratio(self, *args: Any, **kwargs: Any) -> float:
        return fuzz.token_set_ratio(*args, **kwargs)


def test_long_steps_align_whole_and_partial_ratio_only_sees_short_windows(monkeypatch: pytest.MonkeyPatch):
    """
    The alignment runs on every save. `partial_ratio` takes far more than linear time on long strings, so it only
    ever compares windows of up to `STEP_MAX_LINES` lines, a bounded number of them; a longer window (a long step's
    whole text) is compared by `ratio`. Comparing every longer window by `partial_ratio` made one save of this page
    take seconds.
    """
    counting = _CountingFuzz()
    monkeypatch.setattr(crossread, "fuzz", counting)
    words = [letters_only(line) for line in PAGE]
    short_windows = {
        " ".join(word for word in words[start:end] if word)
        for start in range(len(PAGE))
        for end in range(start + 1, min(start + STEP_MAX_LINES, len(PAGE)) + 1)
    }

    windows = [align_step(step, PAGE) for step in PAGE_STEPS]

    first = len(PAGE_INGREDIENTS) + 1
    assert windows == [(first, first + 11), (first + 11, first + 21), (first + 21, len(PAGE))]
    for step, window in zip(PAGE_STEPS, windows, strict=True):
        assert window is not None and compare(step, " ".join(PAGE[window[0] : window[1]])) is None
    assert set(counting.partial_windows) <= short_windows
    assert len(counting.partial_windows) <= len(PAGE_STEPS) * STEP_MAX_LINES * len(PAGE)

    # the page's extraction, then a save once the reviewer typed a step the transcript lacks: the same way
    counting.partial_windows.clear()
    draft = CardDraft(
        name="Banana Bread",
        ingredients=[_ingredient(text) for text in PAGE_INGREDIENTS],
        steps=[CardDraftStep(text=text) for text in PAGE_STEPS],
    )
    extraction = ExtractionMeta(language="English", cross_read_lines=PAGE)
    extracted = compute_flags(draft, extraction, {})
    assert [flag for flag in extracted if flag.source == CardFlagSource.cross_read] == []
    draft.steps.append(CardDraftStep(text=TYPED_STEP))
    compute_flags(draft, extraction, {}, previous=extracted)
    assert set(counting.partial_windows) <= short_windows
    assert len(counting.partial_windows) <= (2 * len(PAGE_STEPS) + 1) * STEP_MAX_LINES * len(PAGE)


def test_compare_lists_what_the_second_reading_lacks():
    assert compare("1 T. coconut oil (melted)", "1 T. coconut oil (melted)") is None
    assert compare("1 tablespoon coconut oil", "1 T. coconut oil") is None  # agreement isn't required both ways

    case = compare("1 T. sugar", "1 t. sugar")
    assert case is not None and case.missing == [("unit", "tbsp")] and not case.window_blank

    blank = compare("for 2 minutes", "mug for [blank] minutes")
    assert blank is not None and blank.missing == [("number", "2")] and blank.window_blank

    assert compare("Bake 20-25 minutes", "Bake 20 minutes").missing == [("range", "20", "25")]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("line", "window"),
    [
        ("1/3 C. almond flour", "1/3 c. almond flour"),  # a capital C is cups too
        ("1/2 t vanilla", "1/2 tsp. vanilla"),
        ("1 Tbs. oil", "1 Tbsp. oil"),
        ("1 pkg. yeast", "1 Pkg yeast"),
    ],
)
def test_units_are_compared_by_what_they_mean(line: str, window: str):
    assert compare(line, window) is None


def test_tablespoons_and_teaspoons_still_disagree():
    assert compare("1 T. oil", "1 t. oil").missing == [("unit", "tbsp")]  # type: ignore[union-attr]
    assert compare("1/2 tsp vanilla", "1/2 T vanilla").missing == [("unit", "tsp")]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_the_transcript_is_read_on_the_image_slot(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))
    answers = banana_answers()
    fake = FakeCardAI(answers).install(monkeypatch)
    pages = make_pages(tmp_path, 2)

    with job_session(user) as (_, repos):
        ai = JobOpenAIService(repos)
        assert await read_transcript(pages, ai=ai) == LINES

        call = fake.calls[-1]
        assert (call.provider, call.schema, call.images) == ("Vision", "OpenAIRecipeCardTranscript", 2)
        assert "Image 1 (front), Image 2 (back)" in call.message

        for nothing in ({"contains_recipe": False, "text": ""}, {"contains_recipe": True, "text": " \n "}, None):
            answers["OpenAIRecipeCardTranscript"] = nothing
            with pytest.raises(CrossReadFailed):
                await read_transcript(pages, ai=ai)
