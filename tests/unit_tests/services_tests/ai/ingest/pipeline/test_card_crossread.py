"""The second reading's alignment and comparison (docs/ai/PHASE2.md §4.5), and the card text it reads"""

from fractions import Fraction
from pathlib import Path

import pytest

from mealie.services.ai.ingest.pipeline.cardtext import (
    canonical_markers,
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


def test_salient_tokens_keep_case_sensitive_units_after_a_number():
    assert salient_tokens("1 T. coconut oil") == [("number", "1"), ("unit", "T")]
    assert salient_tokens("1/4 t. salt, don't stir") == [("number", "1/4"), ("unit", "t")]
    assert salient_tokens("Bake 20-25 min at 350°") == [("range", "20", "25"), ("number", "350")]
    assert salient_tokens("Microwave for [blank] minutes") == [("marker", "blank")]


def test_an_ingredient_aligns_with_its_own_line():
    assert LINES[align_ingredient("1 banana", LINES)] == "1 banana"  # type: ignore[index]
    assert LINES[align_ingredient("1 tbsp coconut oil melted", LINES)] == "1 T. coconut oil (melted)"  # type: ignore[index]
    assert align_ingredient("2 cups chopped walnuts", LINES) is None
    assert align_ingredient("2", LINES) is None


def test_a_wrapped_step_aligns_with_its_whole_window():
    step = "Microwave in bowl or large mug for 2 minutes or until firm in center."
    start, end = align_step(step, LINES)  # type: ignore[misc]

    assert LINES[start:end] == [
        "thoroughly. Microwave in bowl or large",
        "mug for [blank] minutes or until firm",
        "in center.",
    ]
    assert align_step("Whisk the cream until stiff peaks form.", LINES) is None


def test_compare_lists_what_the_second_reading_lacks():
    assert compare("1 T. coconut oil (melted)", "1 T. coconut oil (melted)") is None
    assert compare("1 tablespoon coconut oil", "1 T. coconut oil") is None  # agreement isn't required both ways

    case = compare("1 T. sugar", "1 t. sugar")
    assert case is not None and case.missing == [("unit", "T")] and not case.window_blank

    blank = compare("for 2 minutes", "mug for [blank] minutes")
    assert blank is not None and blank.missing == [("number", "2")] and blank.window_blank

    assert compare("Bake 20-25 minutes", "Bake 20 minutes").missing == [("range", "20", "25")]  # type: ignore[union-attr]


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
