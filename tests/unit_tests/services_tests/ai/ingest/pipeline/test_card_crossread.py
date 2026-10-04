"""The second reading's alignment and comparison (docs/ai/PHASE2.md §4.5), and the card text it reads"""

from fractions import Fraction
from pathlib import Path

import pytest

from mealie.services.ai.ingest.pipeline.cardtext import (
    canonical_markers,
    card_numbers,
    find_numbers,
    find_temperatures,
    format_number,
    markers_in,
    number_set,
    salient_tokens,
)
from mealie.services.ai.ingest.pipeline.crossread import (
    CrossReadFailed,
    align_ingredient,
    align_step,
    compare,
    read_transcript,
    transcript_lines,
)
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


def test_markers_and_temperatures():
    assert canonical_markers("a [ Illegible ] b [BLANK]") == "a [illegible] b [blank]"
    assert markers_in("for [blank] min, [illegible]") == ["blank", "illegible"]
    found = [(t.value, t.unit) for t in find_temperatures("350°, 180 °C, 400 degrees F, 425F, 12 C. flour")]
    assert found == [(350, None), (180, "C"), (400, "F"), (425, "F")]


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
