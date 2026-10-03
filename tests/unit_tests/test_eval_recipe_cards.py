import asyncio
import json
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

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
from mealie.scripts import eval_recipe_cards as ev
from mealie.services.ai import anthropic_adapter
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


def test_match_lines_missing_and_extra_lines():
    result = ev.match_lines(["1 egg", "1 banana", "1/4 t. salt", "1/2 t vanilla"], [["1 egg"], ["1 banana"], ["sugar"]])

    assert result.recall == 0.5
    assert result.precision == pytest.approx(2 / 3)
    assert result.missing == ["1/4 t. salt", "1/2 t vanilla"]
    assert result.extra == ["sugar"]


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
        ("1 banana", (1.0,), None),
        ("2 eggs", (2.0,), None),
        ("1 tomato", (1.0,), None),
        ("Cinnamon to taste", None, None),
        ("Tbsp sugar", None, None),  # a unit is only read after a quantity
    ],
)
def test_parse_amount(line: str, quantity: tuple[float, ...] | None, unit: str | None):
    amount = ev.parse_amount(line)

    assert amount.unit == unit
    assert amount.quantity == (pytest.approx(quantity) if quantity else None)


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
        EXPECTED, make_recipe(ingredients=[*EXPECTED.ingredients, "2 tbsp sugar", "1 tsp baking powder"])
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


# ================================================================
# Fixtures, CLI and reporting


def test_load_cards_reads_the_fixture_format():
    cards = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])

    assert [card.id for card in cards] == ["banana-mug-cake"]
    assert cards[0].images == [CARDS_DIR / "banana-mug-cake.jpg"]
    assert cards[0].fixture.expected.name == "Banana Mug Cake"
    assert cards[0].fixture.expected.must_not_invent == ["cook time"]


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


def test_parser_accepts_documented_flags():
    args = ev.build_parser().parse_args(
        [
            *("--group", "home", "--household", "family"),
            *("--provider", "Gemini", "--provider", "Ollama", "--ocr", "--ocr-provider", "Fast"),
            *("--cards", "/app/data/cards", "--card", "banana-mug-cake", "--out", "results.json", "--repeat", "3"),
            *("--price", "Gemini=0.30,2.50", "--price", "Ollama=0,0"),
        ]
    )

    assert args.group == "home"
    assert args.household == "family"
    assert args.providers == ["Gemini", "Ollama"]
    assert args.ocr is True
    assert args.ocr_provider == "Fast"
    assert args.cards == Path("/app/data/cards")
    assert args.only_cards == ["banana-mug-cake"]
    assert args.out == Path("results.json")
    assert args.repeat == 3
    assert dict(args.prices) == {"Gemini": (0.30, 2.50), "Ollama": (0.0, 0.0)}


def test_parser_defaults():
    args = ev.build_parser().parse_args(["--group", "home"])

    assert args.providers == []
    assert args.ocr is False
    assert args.cards == ev.DEFAULT_CARDS_DIR
    assert args.out == ev.DEFAULT_OUT
    assert args.repeat == 1


@pytest.mark.parametrize(
    "argv",
    [
        [],  # --group is required
        ["--group", "home", "--repeat", "0"],
        ["--group", "home", "--price", "Gemini"],
        ["--group", "home", "--price", "Gemini=1"],
        ["--group", "home", "--price", "Gemini=-1,2"],
    ],
)
def test_parser_rejects_bad_flags(argv: list[str]):
    with pytest.raises(SystemExit):
        ev.build_parser().parse_args(argv)


def test_run_cost():
    usage = {"Gemini": ev.TokenUsage(requests=2, prompt_tokens=1_000_000, completion_tokens=500_000)}

    assert ev.run_cost(usage, {"Gemini": (0.30, 2.50)}) == pytest.approx(0.30 + 1.25)
    assert ev.run_cost(usage, {}) is None
    # a run that reported no usage, say because it failed, has no known cost
    assert ev.run_cost({}, {}) is None
    assert ev.run_cost({}, {"Gemini": (0.30, 2.50)}) is None


def make_provider(name: str = "Gemini", model: str = "gemini-flash") -> AIProviderOut:
    return AIProviderOut(id=uuid4(), name=name, model=model, api_key="secret-key")


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
            scores=ev.score_recipe(card.fixture.expected, make_recipe()),
            usage={"Gemini": ev.TokenUsage(requests=2, prompt_tokens=1000, completion_tokens=200)},
            cost_usd=0.0008,
        ),
        # a run that failed before the provider answered reported no usage, so has no cost
        ev.RunResult(card=card.id, label="Gemini", attempt=2, verified_by_owner=False, latency_s=30.0, error="Boom"),
    ]

    table = ev.format_report(results, configs, [card])
    assert "Gemini" in table
    assert "Misread" in table
    assert "banana-mug-cake (unverified)" in table

    [summary] = ev.summarize(results, configs)
    assert summary.runs == 2
    assert summary.errors == 1
    assert summary.score == pytest.approx(results[0].score / 2)  # failed runs count as 0
    assert summary.latency_s == 2.0  # failed runs don't count towards latency
    assert summary.misread == 0
    assert summary.cost_usd == pytest.approx(0.0008)  # the mean of the runs with a cost

    report = ev.build_report(results, configs, [card], group="home", cards_dir=CARDS_DIR, repeat=2)
    dumped = json.dumps(report, default=str)
    assert "secret-key" not in dumped
    assert json.loads(dumped)["runs"][1]["error"] == "Boom"


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


def test_build_configs(unique_user: TestUser, providers: dict[str, AIProviderOut], monkeypatch: pytest.MonkeyPatch):
    from mealie.services import ocr

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    repos = unique_user.repos
    vision, text = providers["vision"], providers["text"]

    [config] = ev.build_configs(repos, [], ocr=False)
    assert (config.label, config.image_provider, config.text_provider) == (vision.name, vision, vision)

    configs = ev.build_configs(repos, [text.name.upper(), vision.name], ocr=True)
    assert [(c.image_provider, c.text_provider) for c in configs] == [(text, text), (vision, vision), (None, text)]
    assert configs[-1].is_ocr

    [ocr_config] = ev.build_configs(repos, [str(text.id)], ocr=True, ocr_provider_name=vision.name)[1:]
    assert ocr_config.text_provider == vision

    # a provider named twice is evaluated once
    [config] = ev.build_configs(repos, [vision.name, vision.name.upper(), str(vision.id)], ocr=False)
    assert config.image_provider == vision

    with pytest.raises(ev.EvalSetupError, match="No AI provider named"):
        ev.build_configs(repos, ["no such provider"], ocr=False)


def test_build_configs_needs_ocr_for_ocr(
    unique_user: TestUser, providers: dict[str, AIProviderOut], monkeypatch: pytest.MonkeyPatch
):
    from mealie.services import ocr

    monkeypatch.setattr(ocr, "is_available", lambda: False)

    with pytest.raises(ev.EvalSetupError, match="OCR isn't available"):
        ev.build_configs(unique_user.repos, [], ocr=True)

    assert len(ev.build_configs(unique_user.repos, [], ocr=False)) == 1


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
        ingredients=[OpenAIRecipeIngredient(text=line) for line in EXPECTED.ingredients],
        instructions=[OpenAIRecipeInstruction(text=text) for text in EXPECTED.instructions],
    )


def test_run_card_with_provider(
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

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config))

    assert result.error is None
    assert result.scores is not None and result.scores.overall > 0.95
    assert result.recipe is not None and result.recipe["name"] == "Banana Mug Cake"
    assert result.latency_s >= 0
    # every request ran on the provider under test, not the group's configured ones
    assert [call["schema"] for call in ai.calls] == ["OpenAICompiledSource", "OpenAIRecipe"]
    assert all(call["image_provider"] == text and call["default_provider"] == text for call in ai.calls)
    assert ai.calls[0]["has_images"]
    # nothing is written next to the fixtures
    assert sorted(CARDS_DIR.iterdir()) == fixture_files


def test_run_card_records_errors(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    openai_recipe: OpenAIRecipe,
):
    from mealie.services import ocr

    # OCR would work, so a fallback to it would succeed
    ocr_reads: list[Path] = []

    def extract_text(path: Path) -> ocr.OCRResult:
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

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config))

    # the provider's own error is the result, not the workflow's "couldn't read anything"
    assert result.error == "ImageCompiler: RuntimeError: provider unreachable 401"
    assert result.scores is None
    assert result.score == 0.0
    # and it didn't fall back to OCR
    assert calls == [True]
    assert ocr_reads == []


def test_run_card_records_ocr_errors(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    openai_recipe: OpenAIRecipe,
):
    from mealie.services import ocr

    def extract_text(_: Path) -> ocr.OCRResult:
        raise OSError("tesseract crashed")

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", extract_text)
    ai = StubAI(openai_recipe).install(monkeypatch)
    card = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])[0]
    text = providers["text"]
    config = ev.EvalConfig(label=f"OCR+{text.name}", image_provider=None, text_provider=text)

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config))

    assert result.error == "OCRImageCompiler: OSError: tesseract crashed"
    assert result.scores is None
    assert ai.calls == []


def test_run_card_with_ocr(
    unique_user: TestUser,
    providers: dict[str, AIProviderOut],
    monkeypatch: pytest.MonkeyPatch,
    openai_recipe: OpenAIRecipe,
):
    from mealie.services import ocr

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", lambda _: ocr.OCRResult(text="Banana Mug Cake\n1 banana"))
    ai = StubAI(openai_recipe).install(monkeypatch)
    card = ev.load_cards(CARDS_DIR, ["banana-mug-cake"])[0]
    text = providers["text"]
    config = ev.EvalConfig(label=f"OCR+{text.name}", image_provider=None, text_provider=text)

    result = asyncio.run(ev.run_card(unique_user.repos, get_locale_provider("en-US"), card, config))

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
    # An eval run isn't the group's real usage
    assert usage_log(unique_user, text) == []


def test_eval_service_tallies_claude_tokens(unique_user: TestUser, monkeypatch: pytest.MonkeyPatch):
    async def get_response(prompt, message, *, response_schema, provider, attachments=None, usage=None):
        usage.prompt_tokens, usage.completion_tokens = 300, 70
        return response_schema(text="hello"), usage

    monkeypatch.setattr(anthropic_adapter, "get_response", get_response)
    claude = AIProviderOut(id=uuid4(), name="Claude", model="c", api_key="k", protocol=AIProviderProtocol.anthropic)
    ai = ev.EvalOpenAIService(unique_user.repos, image_provider=None, text_provider=claude)

    asyncio.run(ai.get_response("prompt", "message", response_schema=OpenAIText))

    assert ai.usage == {"Claude": ev.TokenUsage(requests=1, prompt_tokens=300, completion_tokens=70)}


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


def test_eval_service_without_a_provider_for_the_slot(unique_user: TestUser, providers: dict[str, AIProviderOut]):
    ai = ev.EvalOpenAIService(unique_user.repos, image_provider=None, text_provider=providers["text"])

    with pytest.raises(OpenAINotEnabledException, match="No image provider set"):
        ai.runtime.candidates(AIProviderSlot.image)
