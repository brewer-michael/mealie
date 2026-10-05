"""Where on a card a field's text probably is, so a re-read's selection starts there (`pipeline.region_hint`)"""

from pathlib import Path

import pytest

from mealie.core.config import get_app_settings
from mealie.schema.recipe_ingest import OCRLine, PageMeta, PageOCR, RegionHintSource
from mealie.services import ocr
from mealie.services.ai.ingest.pipeline import RegionHint, orient_page, region_hint
from mealie.services.ai.ingest.pipeline import regions as regions_module
from tests import data as test_data
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import make_pages

BANANA = test_data.CWD / "cards" / "banana-mug-cake.jpg"


def page(index: int = 0, lines: list[OCRLine] | None = None) -> PageMeta:
    return PageMeta(
        index=index,
        width=1600,
        height=1200,
        view_width=1600,
        view_height=1200,
        raw_sha256="raw",
        page_sha256="page",
        format="jpeg",
        raw_bytes=1,
        oriented=lines is not None,
        ocr=PageOCR(text="\n".join(line.text for line in lines), confidence=80.0, lines=lines) if lines else None,
    )


def line(text: str, y: float, height: float = 0.05, x: float = 0.1, width: float = 0.6) -> OCRLine:
    return OCRLine(text=text, x=x, y=y, width=width, height=height)


CARD_LINES = [
    line("Mystery Bread", 0.05, 0.08),
    line("2 cups flour", 0.2),
    line("1 tsp salt", 0.26),
    line("Mix well and knead for ten", 0.5),
    line("minutes, then let it rise", 0.56),
    line("Bake at 350 for 30 min.", 0.8),
]


def test_a_line_tesseract_found_gives_a_band_around_it():
    hint = region_hint([page(lines=CARD_LINES)], None, "Bake at 350° for 30 minutes.")

    # across the card, half a line above and below the line (0.8 to 0.85)
    assert hint == RegionHint(page=0, x=0.05, y=0.775, width=0.9, height=0.1, source=RegionHintSource.ocr)


def test_a_text_written_over_several_lines_takes_them_all():
    step = "Mix well and knead for ten minutes, then let it rise until doubled."
    hint = region_hint([page(lines=CARD_LINES)], None, step)

    assert hint is not None and hint.source == RegionHintSource.ocr
    # both lines of the step (0.5 to 0.61), half a line more each way
    assert (hint.y, hint.height) == (0.475, 0.16)


@pytest.mark.parametrize(
    ("text", "y"),
    [
        ("1 tsp. salt", 0.235),  # half a line above the line at 0.26
        ("2 c. flour", 0.175),
        ("Mystery Bread", 0.01),  # a taller line: half of it above
        ("[illegible] Bread", 0.01),  # markers aren't compared
    ],
)
def test_the_most_alike_line_wins(text: str, y: float):
    hint = region_hint([page(lines=CARD_LINES)], None, text)
    assert hint is not None and hint.source == RegionHintSource.ocr
    assert hint.y == pytest.approx(y, abs=1e-4)


def test_the_page_it_was_found_on():
    back = page(1, lines=[line("Frosting: 1 c. powdered sugar", 0.3)])
    hint = region_hint([page(lines=CARD_LINES), back], None, "1 c. powdered sugar")
    assert hint is not None and (hint.page, hint.source) == (1, RegionHintSource.ocr)


def test_a_band_stays_on_the_page():
    lines = [line("Serves 4", 0.97, 0.03), line("Grandma's Pie", 0.0, 0.04)]
    bottom = region_hint([page(lines=lines)], None, "Serves 4")
    top = region_hint([page(lines=lines)], None, "Grandma's Pie")
    assert bottom is not None and top is not None
    assert bottom.y + bottom.height == pytest.approx(1.0) and bottom.height == pytest.approx(0.06)
    assert top.y == 0.0 and top.height == pytest.approx(0.08)


TRANSCRIPTION = """# Mystery Bread

2 cups flour
1 tsp salt

Mix well and knead.
Bake at 350 for 30 min."""


def test_without_tesseract_s_lines_the_place_in_the_transcription_gives_a_band():
    hint = region_hint([page()], TRANSCRIPTION, "Bake at 350° for 30 minutes.")

    # the last of 5 lines: centred at 0.9, 12% high, kept on the page
    assert hint == RegionHint(page=0, x=0.05, y=0.84, width=0.9, height=0.12, source=RegionHintSource.position)

    first = region_hint([page()], TRANSCRIPTION, "Mystery Bread")
    assert first is not None and (first.y, first.height) == (0.04, 0.12)


def test_a_line_tesseract_misread_falls_back_to_the_transcription():
    lines = [line("Mvstcry Brcad", 0.05), line("Bkae a1 35O", 0.8)]
    hint = region_hint([page(lines=lines)], TRANSCRIPTION, "1 tsp salt")
    assert hint is not None and hint.source == RegionHintSource.position


@pytest.mark.parametrize(
    "transcription",
    [
        "Front:\nMystery Bread\n2 cups flour\n\nBack:\nMix well.\nBake at 350 for 30 min.",
        "**Front**\nMystery Bread\n2 cups flour\n\n**Back:**\nMix well.\nBake at 350 for 30 min.",
        "Mystery Bread\n2 cups flour\n\n## Image 2 (back)\nMix well.\nBake at 350 for 30 min.",
        "Front:\nMystery Bread\n2 cups flour\n\nNext page:\nMix well.\nBake at 350 for 30 min.",
        # no headers: the two pages share the lines in order
        "Mystery Bread\n2 cups flour\nMix well.\nBake at 350 for 30 min.",
    ],
)
def test_the_page_whose_part_of_the_transcription_holds_the_text(transcription: str):
    hint = region_hint([page(0), page(1)], transcription, "Bake at 350 for 30 minutes")

    # the second of two lines on the back
    assert hint == RegionHint(page=1, x=0.05, y=0.69, width=0.9, height=0.12, source=RegionHintSource.position)
    front = region_hint([page(0), page(1)], transcription, "2 c. flour")
    assert front is not None and front.page == 0


def test_a_part_for_a_page_the_card_lacks_is_its_last_page():
    hint = region_hint([page(0)], "Front:\nSoup\n\nBack:\nServe hot with bread", "Serve hot with bread")
    assert hint is not None and hint.page == 0


@pytest.mark.parametrize(
    ("pages", "transcription", "text"),
    [
        ([page(lines=CARD_LINES)], TRANSCRIPTION, "Garnish with parsley"),  # not on the card: typed by the reviewer
        ([page()], None, "Bake at 350"),
        ([page()], "", "Bake at 350"),
        ([page(lines=CARD_LINES)], TRANSCRIPTION, "  "),
        ([], TRANSCRIPTION, "Bake at 350"),
    ],
)
def test_no_hint_when_the_text_isn_t_found(pages: list[PageMeta], transcription: str | None, text: str):
    assert region_hint(pages, transcription, text) is None


def test_a_short_line_isn_t_found_inside_a_long_text():
    """ "350" or "Salt" alone is in almost any step: a line is compared with a longer text only from 8 characters"""
    lines = [line("Salt", 0.1), line("350", 0.2)]
    assert region_hint([page(lines=lines)], None, "Bake at 350 for 30 minutes, then salt") is None
    assert regions_module.MIN_MATCH_CHARACTERS == 8


PECAN_PIE = """# Pecan Pie

## Ingredients
- 3 eggs
- 1 C. sugar
- 1 C. dark corn syrup
- 2 T. butter
- 1 t. vanilla
- 1 C. [illegible]
- 1 pie crust

## Directions
Beat eggs, sugar, syrup, butter and vanilla."""

PECAN_PIE_LINES = [
    line("Pecan Pie", 0.066, 0.0493),
    line("_- 1 C. sugar. |", 0.189, 0.0343),
    line("-1.C. dark cornsyrup", 0.223, 0.062),
    line("- 2 T. butter", 0.2787, 0.0243),
    line("- 7 t. vanilla", 0.3227, 0.025),
    line("1G eae", 0.3663, 0.0254),  # Tesseract's reading of the scribble
    line("- 1 pie crust", 0.411, 0.031),
]
"""A live run's printed Pecan Pie card as Tesseract read it: the line with the scribble is "1G eae" """


def test_a_marker_line_is_found_between_the_lines_around_it():
    """
    "1 C. [illegible]" is "1 c." once its marker is out, which every "1 C. ..." line matches: the re-read started on
    "1 C. sugar" (and a model then proposed "1 C. sugar" for it). It's the line between "1 t. vanilla" and "1 pie
    crust", whatever Tesseract made of it.
    """
    hint = region_hint([page(lines=PECAN_PIE_LINES)], PECAN_PIE, "1 C. [illegible]")

    assert hint is not None and (hint.page, hint.source) == (0, RegionHintSource.ocr)
    assert (hint.y, hint.height) == pytest.approx((0.3536, 0.0508), abs=1e-4)  # around "1G eae" only

    # Tesseract read nothing there: the gap between them
    skipped = [line for line in PECAN_PIE_LINES if line.text != "1G eae"]
    gap = region_hint([page(lines=skipped)], PECAN_PIE, "1 C. [illegible]")
    assert gap is not None and gap.source == RegionHintSource.ocr
    assert 0.3227 < gap.y + gap.height / 2 < 0.411  # centred between "1 t. vanilla" and "1 pie crust"


def test_a_short_line_is_found_by_the_lines_around_it():
    """ "1 onion" is in a step too ("Brown the beef with the onion"): its own line is between its neighbours"""
    transcription = "# Chili\n- 1 lb. ground beef\n- 1 onion\n- 1 T. chili powder\nBrown the beef with the onion."
    lines = [
        line("Brown the beef with the onion.", 0.05),
        line("- 1 lb. ground beef", 0.4),
        line("- 7_onion", 0.46),
        line("- 1 T. chill powder", 0.52),
    ]
    hint = region_hint([page(lines=lines)], transcription, "1 onion")
    assert hint is not None and hint.y == pytest.approx(0.435, abs=1e-4)


def test_a_marker_line_without_its_neighbours_is_placed_by_its_own_line():
    """No Tesseract line reads like the lines around it: the transcription's line saying exactly that places it"""
    lines = [line("_- 1 C. sugar. |", 0.189, 0.0343), line("-1.C. dark cornsyrup", 0.223, 0.062)]
    hint = region_hint([page(lines=lines)], PECAN_PIE, "1 C. [illegible]")
    assert hint is not None and hint.source == RegionHintSource.position
    # the 8th of 11 lines (headings too), not the first "1 C." line
    assert hint.y == pytest.approx((7 + 0.5) / 11 - 0.06, abs=1e-4)


PECAN_PIE_BACK = [
    line("Directions", 0.065, 0.04),
    line("1. Beat eggs, sugar, syrup, butter and — |", 0.123, 0.04),
    line("vanilla.", 0.173, 0.03),
]
"""The same card's back: "3 eggs" is in its first step"""


def test_a_short_line_whose_neighbour_tesseract_didnt_read_is_beside_the_other():
    """
    Tesseract read neither "3 eggs" nor the "Ingredients" heading above it: the re-read started on the back's "1. Beat
    eggs, sugar, ..." (`partial_ratio` finds "3 eggs" in it). It's just above "1 C. sugar", the line after it.
    """
    pages = [page(lines=PECAN_PIE_LINES), page(1, lines=PECAN_PIE_BACK)]

    hint = region_hint(pages, PECAN_PIE, "3 eggs")

    assert hint is not None and (hint.page, hint.source) == (0, RegionHintSource.ocr)
    assert 0.066 < hint.y + hint.height / 2 < 0.189  # below the title, just above "1 C. sugar"
    # a line it read itself is found as a whole, on its own page
    read = [*PECAN_PIE_LINES[:1], line("- 3 eggs", 0.15, 0.03), *PECAN_PIE_LINES[1:]]
    hint = region_hint([page(lines=read), page(1, lines=PECAN_PIE_BACK)], PECAN_PIE, "3 eggs")
    assert hint is not None and (hint.page, hint.y) == (0, pytest.approx(0.135, abs=1e-4))


def test_a_neighbour_is_matched_as_a_whole():
    """
    "1 cup buttermilk" is inside "Buttermilk Pancakes": when Tesseract read neither it nor "2 eggs", the title isn't
    taken for the line before "2 eggs" (the hint was a band just below it); its line in the transcription places it
    """
    transcription = "# Buttermilk Pancakes\n- 1 c. flour\n- 1 cup buttermilk\n- 2 eggs"
    lines = [line("Buttermilk Pancakes", 0.05, 0.06), line("- 1 c. flour", 0.2, 0.04)]

    hint = region_hint([page(lines=lines)], transcription, "2 eggs")

    assert hint is not None and hint.source == RegionHintSource.position
    assert hint.y == pytest.approx(3.5 / 4 - 0.06, abs=1e-4)


CAKE = """# Grandma's Cake
- 2 cups flour
- 1 cup sugar
- 1 egg
- 1 cup milk
## Frosting
- 2 cups powdered sugar
- 1 egg
- 1 tsp vanilla"""

CAKE_LINES = [
    line("Grandma's Cake", 0.05, 0.04),
    line("2 cups flour", 0.12, 0.04),
    line("1 cup sugar", 0.17, 0.04),
    line("1 egg", 0.22, 0.04),
    line("1 cup milk", 0.27, 0.04),
    line("Frosting", 0.40, 0.04),
    line("2 cups powdered sugar", 0.47, 0.04),
    line("1 egg", 0.52, 0.04),
    line("1 tsp vanilla", 0.57, 0.04),
]


@pytest.mark.parametrize(
    ("lines", "transcription", "y"),
    [
        (CAKE_LINES, CAKE, (0.2, 0.5)),  # between its neighbours
        ([line for line in CAKE_LINES if "sugar" not in line.text], CAKE, (0.2, 0.5)),  # as a whole line
        ([line for line in CAKE_LINES if line.text != "1 egg"], CAKE, (0.19, 0.49)),  # in the gap there
        ([], CAKE, (0.3289, 0.7733)),  # by its line in the transcription
    ],
)
def test_a_short_line_the_card_says_twice_is_found_by_which_it_is(
    lines: list[OCRLine], transcription: str, y: tuple[float, float]
):
    """The frosting's "1 egg" got the cake's place: the second line saying it is the second"""
    pages = [page(lines=lines or None)]
    hints = [region_hint(pages, transcription, "1 egg", occurrence) for occurrence in (0, 1)]
    assert [hint.y if hint else None for hint in hints] == [pytest.approx(value, abs=1e-4) for value in y]
    assert region_hint(pages, transcription, "1 egg", 5) == hints[1]  # past the last: the last


@pytest.fixture()
def tesseract_on(monkeypatch: pytest.MonkeyPatch):
    for key, value in {"OCR_ENABLED": "true", "OCR_LANGUAGES": "eng", "OCR_TIMEOUT": "60"}.items():
        monkeypatch.setenv(key, value)
    get_app_settings.cache_clear()
    yield
    get_app_settings.cache_clear()


@pytest.mark.skipif(not ocr.binary_available(), reason="tesseract is not installed")
def test_the_banana_card_s_lines_point_at_its_writing(tesseract_on: None, tmp_path: Path):
    """Orientation stores Tesseract's line boxes of the upright page; a handwritten line is found among them"""
    (sideways,) = make_pages(tmp_path, data=BANANA.read_bytes())
    meta = orient_page(sideways)
    assert meta.ocr is not None and meta.ocr.lines
    assert all(0 <= line.x <= 1 and 0 <= line.y <= 1 and line.x + line.width <= 1.0001 for line in meta.ocr.lines)

    hint = region_hint([meta], None, "Cinnamon to taste")

    assert hint is not None and hint.source == RegionHintSource.ocr
    # the ingredient column's last line, low on the card
    assert 0.45 < hint.y < 0.75
