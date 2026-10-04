"""Turning card pages upright with a margin (docs/ai/PHASE2.md §4.1 step 0, §4.4)"""

import io
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

import mealie.services.ocr.tesseract as tesseract_module
from mealie.core.config import get_app_settings
from mealie.schema.recipe_ingest import OCRLine, PageMeta, PageOCR, PageRotationSource
from mealie.services import ocr
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.pipeline import (
    CardPage,
    OrientDecision,
    compilers,
    decide_orientation,
    orient_page,
    orientation_available,
    oriented_meta,
)
from mealie.services.ai.ingest.pipeline import orient as orient_module
from mealie.services.ai.ingest.settings import get_ingest_settings
from tests import data as test_data
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import card_image, make_pages

requires_tesseract = pytest.mark.skipif(not ocr.binary_available(), reason="tesseract is not installed")

BANANA = test_data.CWD / "cards" / "banana-mug-cake.jpg"

# measured on the banana card (F7)
SIDEWAYS = {0: 307.0, 90: 3533.0, 180: 310.0, 270: 8234.0}
UPRIGHT = {0: 6776.0, 90: 400.0, 180: 3594.0, 270: 380.0}
UPSIDE_DOWN = {0: 3209.0, 90: 400.0, 180: 6719.0, 270: 380.0}
FAINT = {0: 100.0, 90: 140.0, 180: 90.0, 270: 60.0}


@pytest.mark.parametrize(
    ("scores", "min_ratio", "expected"),
    [
        (SIDEWAYS, limits.ORIENT_MIN_RATIO, 270),
        (UPRIGHT, limits.ORIENT_MIN_RATIO, 0),
        (UPSIDE_DOWN, limits.ORIENT_MIN_RATIO, 180),
        (FAINT, limits.ORIENT_MIN_RATIO, 0),  # handwriting that reads badly every way: leave it be
        (FAINT, 1.0, 90),  # without a margin the best probe wins, as before
        ({0: 0.0, 90: 0.0, 180: 0.0, 270: 0.0}, 1.0, 0),
        # a blank back page: a few specks read sideways, nothing upright; any ratio of 0 would turn it
        ({0: 0.0, 90: 12.0, 180: 0.0, 270: 0.0}, limits.ORIENT_MIN_RATIO, 0),
        ({0: 0.0, 90: 12.0, 180: 0.0, 270: 0.0}, 1.0, 90),
        ({0: 0.0, 90: tesseract_module.MIN_TURN_SCORE, 180: 0.0, 270: 0.0}, limits.ORIENT_MIN_RATIO, 90),
    ],
)
def test_a_turn_needs_a_margin(scores: dict[int, float], min_ratio: float, expected: int):
    assert tesseract_module._choose_rotation(scores, min_ratio) == expected


@pytest.fixture()
def fake_tesseract(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OCR_ENABLED", "true")
    get_app_settings.cache_clear()
    monkeypatch.setattr(tesseract_module, "_tesseract_path", lambda: "/usr/bin/tesseract")
    yield
    get_app_settings.cache_clear()


@pytest.mark.parametrize(("min_ratio", "rotation"), [(1.0, 90), (limits.ORIENT_MIN_RATIO, 0)])
def test_extract_text_reports_its_scores_and_reads_at_the_rotation_chosen(
    fake_tesseract: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, min_ratio: float, rotation: int
):
    read_sizes: list[tuple[int, int]] = []

    def read_words(image: Image.Image, work_dir: Path, deadline: float) -> list:
        read_sizes.append(image.size)
        return [tesseract_module._Word(1, 1, 1, 100, 30, 80.0, "Banana")]

    monkeypatch.setattr(tesseract_module, "_probe_rotations", lambda image, read: FAINT)
    monkeypatch.setattr(tesseract_module, "_read_words", read_words)
    path = tmp_path / "card.png"
    Image.new("RGB", (400, 1600), "white").save(path)

    result = ocr.extract_text(path, min_ratio=min_ratio)

    assert (result.rotation, result.rotation_scores, result.text) == (rotation, FAINT, "Banana")
    # the text is read at the rotation chosen: a quarter turn swaps the sides
    width, height = read_sizes[-1]
    assert (width > height) is (rotation == 90)


@pytest.fixture()
def fake_tesseract_ocr_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tesseract installed, `OCR_ENABLED=false`"""
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_app_settings.cache_clear()
    monkeypatch.setattr(tesseract_module, "_tesseract_path", lambda: "/usr/bin/tesseract")
    yield
    get_app_settings.cache_clear()


def test_tesseract_can_read_for_orientation_with_ocr_off(
    fake_tesseract_ocr_off: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`OCR_ENABLED` is about reading recipes with OCR; Tesseract being installed is `binary_available`"""
    monkeypatch.setattr(tesseract_module, "_probe_rotations", lambda image, read: SIDEWAYS)
    monkeypatch.setattr(
        tesseract_module, "_read_words", lambda *_, **__: [tesseract_module._Word(1, 1, 1, 100, 30, 80.0, "Soup")]
    )
    path = tmp_path / "card.png"
    Image.new("RGB", (400, 600), "white").save(path)

    assert (ocr.is_available(), ocr.binary_available()) == (False, True)
    assert ocr.extract_text(path) == ocr.OCRResult()  # the OCR reader is off
    result = ocr.extract_text(path, min_ratio=limits.ORIENT_MIN_RATIO, require_enabled=False)
    assert (result.rotation, result.text) == (270, "Soup")


@pytest.mark.parametrize(
    ("ocr_enabled", "orient", "installed", "available"),
    [
        ("true", None, True, True),
        ("false", None, True, True),  # OCR off: orientation still on
        ("false", "true", True, True),
        ("true", "false", True, False),  # its own switch
        ("false", "false", True, False),
        ("true", None, False, False),  # Tesseract not installed
    ],
)
def test_orientation_has_its_own_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ocr_enabled: str,
    orient: str | None,
    installed: bool,
    available: bool,
):
    monkeypatch.setenv("OCR_ENABLED", ocr_enabled)
    if orient is None:
        monkeypatch.delenv("AI_INGEST_ORIENT", raising=False)
    else:
        monkeypatch.setenv("AI_INGEST_ORIENT", orient)
    get_app_settings.cache_clear()
    get_ingest_settings.cache_clear()
    monkeypatch.setattr(tesseract_module, "_tesseract_path", lambda: "/usr/bin/tesseract" if installed else None)
    probed: list[bool] = []

    def extract_text(path: Path, *, min_ratio: float = 1.0, require_enabled: bool = True) -> ocr.OCRResult:
        probed.append(require_enabled)
        return ocr.OCRResult(text="Soup", confidence=90.0, rotation=90)

    monkeypatch.setattr(ocr, "extract_text", extract_text)
    (page,) = make_pages(tmp_path)
    try:
        assert orientation_available() is available
        decision = decide_orientation(page)
    finally:
        get_app_settings.cache_clear()
        get_ingest_settings.cache_clear()

    if available:
        assert decision == OrientDecision(rotation=90, ocr=PageOCR(text="Soup", confidence=90.0), settled=True)
        assert probed == [False]  # whatever OCR_ENABLED says
    else:
        assert decision == OrientDecision(rotation=0, ocr=None, settled=False)
        assert probed == []


# Tesseract's TSV for an 800x600 image: two lines in one paragraph, then a line of a second block. Rows of levels 1-4
# (page, block, paragraph, line) and empty or unreadable (-1) words aren't words.
TSV = "\n".join(
    "\t".join(str(field) for field in row)
    for row in [
        (
            "level",
            "page_num",
            "block_num",
            "par_num",
            "line_num",
            "word_num",
            "left",
            "top",
            "width",
            "height",
            "conf",
            "text",
        ),
        (1, 1, 0, 0, 0, 0, 0, 0, 800, 600, -1, ""),
        (4, 1, 1, 1, 1, 0, 40, 30, 400, 40, -1, ""),
        (5, 1, 1, 1, 1, 1, 40, 30, 160, 40, 91.5, "Banana"),
        (5, 1, 1, 1, 1, 2, 220, 34, 80, 36, 88.0, "Mug"),
        (5, 1, 1, 1, 1, 3, 320, 30, 120, 40, 90.0, "Cake"),
        (5, 1, 1, 1, 2, 1, 40, 100, 20, 30, 70.0, "1"),
        (5, 1, 1, 1, 2, 2, 70, 104, 120, 30, 75.0, "banana"),
        (5, 1, 1, 1, 2, 3, 200, 104, 40, 30, -1.0, ""),
        (5, 1, 2, 1, 1, 1, 400, 500, 100, 40, 85.0, "Bake"),
        (5, 1, 2, 1, 1, 2, 520, 500, 40, 40, 80.0, "at"),
        (5, 1, 2, 1, 1, 3, 580, 500, 80, 44, 82.0, "350"),
    ]
)


def test_tesseract_words_are_grouped_into_line_boxes():
    words = tesseract_module._parse_tsv(TSV)
    assert [(word.text, word.left, word.top) for word in words][:2] == [("Banana", 40, 30), ("Mug", 220, 34)]

    lines = tesseract_module._to_lines(words, (800, 600))

    # fractions of the image as it was read: left and top of the first word, right and bottom of the furthest
    assert lines == (
        ocr.OCRLine(text="Banana Mug Cake", x=0.05, y=0.05, width=0.5, height=0.0667),
        ocr.OCRLine(text="1 banana", x=0.05, y=0.1667, width=0.1875, height=0.0566),
        ocr.OCRLine(text="Bake at 350", x=0.5, y=0.8333, width=0.325, height=0.0734),
    )
    assert tesseract_module._to_text(words) == "Banana Mug Cake\n1 banana\n\nBake at 350"
    assert tesseract_module._to_lines([], (800, 600)) == ()


@pytest.mark.parametrize("rotation", [0, 90])
def test_extract_text_returns_the_line_boxes_of_the_image_as_read(
    fake_tesseract: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rotation: int
):
    """The boxes are fractions of the image turned by the rotation chosen: the page as it is once turned upright"""
    sizes: list[tuple[int, int]] = []

    def read_words(image: Image.Image, work_dir: Path, deadline: float) -> list:
        sizes.append(image.size)
        width, height = image.size
        # one word in the top left quarter of whatever image is read
        return [tesseract_module._Word(1, 1, 1, width // 4, height // 10, 90.0, "Soup", left=0, top=0)]

    monkeypatch.setattr(tesseract_module, "_probe_rotations", lambda image, read: SIDEWAYS if rotation else UPRIGHT)
    monkeypatch.setattr(tesseract_module, "_read_words", read_words)
    path = tmp_path / "card.png"
    Image.new("RGB", (1600, 2400), "white").save(path)

    result = ocr.extract_text(path, min_ratio=1.0)

    assert result.rotation == (270 if rotation else 0)
    assert (sizes[-1][0] > sizes[-1][1]) is bool(rotation)  # read turned
    assert result.lines == (ocr.OCRLine(text="Soup", x=0.0, y=0.0, width=0.25, height=0.1),)


def test_orientation_keeps_the_line_boxes_with_the_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    lines = tuple(ocr.OCRLine(text=f"line {i}", x=0.1, y=i / 400, width=0.5, height=0.002) for i in range(350))
    monkeypatch.setattr(ocr, "binary_available", lambda: True)
    monkeypatch.setattr(
        ocr, "extract_text", lambda path, **_: ocr.OCRResult(text="Soup", confidence=90.0, rotation=90, lines=lines)
    )
    (page,) = make_pages(tmp_path)

    decision = decide_orientation(page)

    assert decision.ocr is not None
    assert len(decision.ocr.lines) == orient_module.MAX_OCR_LINES
    assert decision.ocr.lines[1] == OCRLine(text="line 1", x=0.1, y=0.0025, width=0.5, height=0.002)
    # stored with the page's metadata, and read back
    meta = oriented_meta(page.meta, decision)
    assert PageMeta.model_validate(meta.model_dump()).ocr == decision.ocr


def _files(page: CardPage) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(page.dir.iterdir())}


@pytest.mark.parametrize(
    ("result", "decision"),
    [
        (
            ocr.OCRResult(text="Banana Mug Cake", confidence=61.0, rotation=90),
            OrientDecision(rotation=90, ocr=PageOCR(text="Banana Mug Cake", confidence=61.0), settled=True),
        ),
        (
            ocr.OCRResult(text="Banana Mug Cake", confidence=70.0, rotation=0),
            OrientDecision(rotation=0, ocr=PageOCR(text="Banana Mug Cake", confidence=70.0), settled=True),
        ),
        (ocr.OCRResult(text="", confidence=0.0, rotation=0, failed=True), OrientDecision(0, None, settled=False)),
    ],
)
def test_the_orientation_is_decided_without_writing_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: ocr.OCRResult, decision: OrientDecision
):
    """The runner stages the turned files and stores their metadata before it swaps them in (no crash window)"""
    (page,) = make_pages(tmp_path)
    before = _files(page)
    monkeypatch.setattr(ocr, "binary_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", lambda path, **_: result)

    assert decide_orientation(page) == decision
    assert _files(page) == before

    # a page left as it is is settled with its text; a turn's metadata comes from the turned files
    if decision.settled and not decision.rotation:
        assert oriented_meta(page.meta, decision) == page.meta.model_copy(
            update={"oriented": True, "ocr": decision.ocr}
        )
    if not decision.settled:
        assert oriented_meta(page.meta, decision) == page.meta


@pytest.mark.parametrize(
    ("scores", "sure"),
    [
        (UPRIGHT, True),
        ({0: 900.0, 90: 600.0, 180: 80.0, 270: 60.0}, True),  # 1.5 times the next way up: as a turn must
        ({0: 300.0, 90: 100.0, 180: 80.0, 270: 60.0}, False),  # best upright, but too little to tell
        # the sideways banana card blurred: upright wins on noise (it needs 270 or 90), so the reader's turn applies
        ({0: 116.0, 90: 68.0, 180: 0.0, 270: 112.0}, False),
        ({0: 115.0, 90: 0.0, 180: 0.0, 270: 70.0}, False),
        ({0: 222.0, 270: 158.0}, False),
        ({0: 600.0, 90: 0.0, 180: 0.0, 270: 800.0}, False),  # enough upright, but another way up as good
        ({0: 800.0, 90: 600.0, 180: 0.0, 270: 0.0}, False),  # under 1.5 times the next way up
        ({0: 0.0, 90: 135.0, 180: 83.0, 270: 0.0}, False),  # the banana card blurred: too little read any way up
        ({0: 0.0, 90: 0.0, 180: 0.0, 270: 0.0}, False),  # no words at all
        (FAINT, False),  # handwriting that reads badly every way, upright no better than the rest
        ({}, True),  # nothing probed: taken at its word
    ],
)
def test_a_reading_too_poor_to_tell_leaves_the_orientation_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scores: dict[int, float], sure: bool
):
    """
    Tesseract that read too little to tell which way up the page is fell back to upright: its text is kept, but the
    page isn't marked oriented, so a turn the image reader reports still applies (`compilers._rotations`, the runner's
    `_turn_as_read`), where it used to be dropped for good
    """
    (page,) = make_pages(tmp_path)
    result = ocr.OCRResult(text="Banana", confidence=20.0, rotation=0, rotation_scores=scores)
    monkeypatch.setattr(ocr, "binary_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", lambda path, **_: result)

    decision = decide_orientation(page)

    assert (decision.rotation, decision.settled, decision.sure) == (0, True, sure)
    meta = oriented_meta(page.meta, decision)
    assert (meta.oriented, meta.ocr) == (sure, PageOCR(text="Banana", confidence=20.0))
    assert compilers._rotations([CardPage(dir=page.dir, meta=meta)], [90]) == ({} if sure else {0: 90})
    assert orient_page(page) == meta  # the eval's copies alike


def test_a_page_already_oriented_or_without_tesseract_is_decided_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (page,) = make_pages(tmp_path)

    def extract_text(path: Path, **_) -> ocr.OCRResult:
        raise AssertionError("not probed")

    monkeypatch.setattr(ocr, "extract_text", extract_text)
    monkeypatch.setattr(ocr, "binary_available", lambda: False)
    assert decide_orientation(page) == OrientDecision(rotation=0, ocr=None, settled=False)

    monkeypatch.setattr(ocr, "binary_available", lambda: True)
    oriented = CardPage(dir=page.dir, meta=page.meta.model_copy(update={"oriented": True}))
    assert decide_orientation(oriented) == OrientDecision(rotation=0, ocr=None, settled=True)

    (page.dir / "page.jpg").unlink()
    with pytest.raises(FileNotFoundError):
        decide_orientation(page)


def test_without_tesseract_nothing_is_settled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(ocr, "binary_available", lambda: False)
    (page,) = make_pages(tmp_path)

    assert orient_page(page) == page.meta
    assert page.meta.oriented is False


def test_a_turn_rewrites_the_page_inside_the_write_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (page,) = make_pages(tmp_path, data=card_image(size=(600, 800)))
    calls: list[tuple[Path, float]] = []

    def extract_text(path: Path, *, min_ratio: float = 1.0, require_enabled: bool = True) -> ocr.OCRResult:
        assert require_enabled is False  # orientation has its own switch
        calls.append((path, min_ratio))
        return ocr.OCRResult(text="Banana Mug Cake", confidence=61.0, rotation=90)

    locked: list[str] = []

    @contextmanager
    def ingest_write() -> Iterator[None]:
        locked.append("enter")
        yield
        locked.append("exit")

    monkeypatch.setattr(ocr, "binary_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", extract_text)
    monkeypatch.setattr(orient_module.storage, "ingest_write", ingest_write)

    meta = orient_page(page)

    assert calls == [(page.page_path, limits.ORIENT_MIN_RATIO)]
    assert locked == ["enter", "exit"]
    assert (meta.width, meta.height, meta.rotation, meta.rotation_source, meta.oriented) == (
        800,
        600,
        90,
        PageRotationSource.ocr,
        True,
    )
    assert meta.page_sha256 != page.meta.page_sha256
    assert meta.ocr == PageOCR(text="Banana Mug Cake", confidence=61.0)
    with Image.open(page.page_path) as image:
        assert image.size == (800, 600)

    # an oriented page isn't probed again
    assert orient_page(CardPage(dir=page.dir, meta=meta)) == meta
    assert len(calls) == 1


def test_an_upright_page_is_settled_without_a_rewrite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (page,) = make_pages(tmp_path)
    before = page.page_path.read_bytes()
    monkeypatch.setattr(ocr, "binary_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", lambda path, **_: ocr.OCRResult(text="Soup", confidence=90.0))

    meta = orient_page(page)

    assert (meta.rotation, meta.rotation_source, meta.oriented, meta.ocr) == (
        0,
        PageRotationSource.none,
        True,
        PageOCR(text="Soup", confidence=90.0),
    )
    assert page.page_path.read_bytes() == before


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired("tesseract", 60), subprocess.CalledProcessError(1, "tesseract"), OSError("crashed")],
)
def test_a_failed_reading_settles_nothing(
    fake_tesseract: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
):
    """
    Tesseract timing out under load isn't an upright page with no text: the page stays unsettled, so the next
    extraction probes it again and the OCR fallback reads it itself, rather than finding no text for good
    """
    (page,) = make_pages(tmp_path, data=card_image(size=(600, 800)))
    before = page.page_path.read_bytes()

    def fail(*_, **__):
        raise error

    monkeypatch.setattr(tesseract_module.subprocess, "run", fail)

    meta = orient_page(page)

    assert meta == page.meta
    assert (meta.oriented, meta.ocr) == (False, None)
    assert page.page_path.read_bytes() == before

    # once Tesseract answers, the page is settled
    monkeypatch.setattr(tesseract_module, "_probe_rotations", lambda image, read: UPRIGHT)
    monkeypatch.setattr(
        tesseract_module, "_read_words", lambda *_, **__: [tesseract_module._Word(1, 1, 1, 100, 30, 80.0, "Soup")]
    )
    settled = orient_page(page)

    assert (settled.oriented, settled.rotation) == (True, 0)
    assert settled.ocr is not None and (settled.ocr.text, settled.ocr.confidence) == ("Soup", 80.0)
    assert [line.text for line in settled.ocr.lines] == ["Soup"]


@pytest.fixture()
def default_ocr_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in {"OCR_ENABLED": "true", "OCR_LANGUAGES": "eng", "OCR_TIMEOUT": "60"}.items():
        monkeypatch.setenv(key, value)
    get_app_settings.cache_clear()
    yield
    get_app_settings.cache_clear()


@requires_tesseract
def test_the_sideways_card_turns_and_an_upright_copy_stays_put(default_ocr_settings: None, tmp_path: Path):
    (sideways,) = make_pages(tmp_path / "sideways", data=BANANA.read_bytes())
    assert sideways.meta.width < sideways.meta.height  # the card lies on its side in the photo (F7)

    turned = orient_page(sideways)

    assert turned.rotation in (90, 270)
    assert (turned.rotation_source, turned.oriented) == (PageRotationSource.ocr, True)
    assert turned.width > turned.height
    assert turned.ocr is not None and turned.ocr.text

    # the turned page, intake'd again, is upright: the probe leaves it alone
    (upright,) = make_pages(tmp_path / "upright", data=sideways.page_path.read_bytes())
    before = upright.page_path.read_bytes()
    settled = orient_page(upright)

    assert (settled.rotation, settled.rotation_source, settled.oriented) == (0, PageRotationSource.none, True)
    assert upright.page_path.read_bytes() == before
    assert settled.ocr is not None and settled.ocr.text


@requires_tesseract
def test_the_upside_down_card_turns_round(default_ocr_settings: None, tmp_path: Path):
    with Image.open(BANANA) as image:
        upright = image.transpose(Image.Transpose.ROTATE_90)  # the raw pixels, as the phone saw them
        buffer = io.BytesIO()
        upright.transpose(Image.Transpose.ROTATE_180).save(buffer, format="JPEG", quality=90)
    (page,) = make_pages(tmp_path, data=buffer.getvalue())

    meta = orient_page(page)

    assert (meta.rotation, meta.rotation_source) == (180, PageRotationSource.ocr)


@requires_tesseract
@pytest.mark.parametrize(("orient", "turned"), [("true", True), ("false", False)])
def test_ocr_off_still_turns_the_sideways_card_unless_orientation_is_off(
    default_ocr_settings: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, orient: str, turned: bool
):
    """`OCR_ENABLED=false` turns off the OCR fallback reader only; `AI_INGEST_ORIENT=false` turns off orientation"""
    monkeypatch.setenv("OCR_ENABLED", "false")
    monkeypatch.setenv("AI_INGEST_ORIENT", orient)
    get_app_settings.cache_clear()
    get_ingest_settings.cache_clear()
    (sideways,) = make_pages(tmp_path, data=BANANA.read_bytes())

    try:
        meta = orient_page(sideways)
    finally:
        get_ingest_settings.cache_clear()

    if turned:
        assert meta.rotation in (90, 270)
        assert (meta.rotation_source, meta.oriented) == (PageRotationSource.ocr, True)
        assert meta.ocr is not None and "banana" in meta.ocr.text.lower()  # the text read is still stored
    else:
        assert meta == sideways.meta
        assert (meta.rotation, meta.oriented, meta.ocr) == (0, False, None)
