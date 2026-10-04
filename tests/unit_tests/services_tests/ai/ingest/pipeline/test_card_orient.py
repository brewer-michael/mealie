"""Turning card pages upright with a margin (docs/ai/PHASE2.md §4.1 step 0, §4.4)"""

import io
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

import mealie.services.ocr.tesseract as tesseract_module
from mealie.core.config import get_app_settings
from mealie.schema.recipe_ingest import PageOCR, PageRotationSource
from mealie.services import ocr
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.pipeline import CardPage, OrientDecision, decide_orientation, orient_page, oriented_meta
from mealie.services.ai.ingest.pipeline import orient as orient_module
from tests import data as test_data
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import card_image, make_pages

requires_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")

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
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", lambda path, *, min_ratio=1.0: result)

    assert decide_orientation(page) == decision
    assert _files(page) == before

    # a page left as it is is settled with its text; a turn's metadata comes from the turned files
    if decision.settled and not decision.rotation:
        assert oriented_meta(page.meta, decision) == page.meta.model_copy(
            update={"oriented": True, "ocr": decision.ocr}
        )
    if not decision.settled:
        assert oriented_meta(page.meta, decision) == page.meta


def test_a_page_already_oriented_or_without_tesseract_is_decided_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (page,) = make_pages(tmp_path)

    def extract_text(path: Path, *, min_ratio: float = 1.0) -> ocr.OCRResult:
        raise AssertionError("not probed")

    monkeypatch.setattr(ocr, "extract_text", extract_text)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    assert decide_orientation(page) == OrientDecision(rotation=0, ocr=None, settled=False)

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    oriented = CardPage(dir=page.dir, meta=page.meta.model_copy(update={"oriented": True}))
    assert decide_orientation(oriented) == OrientDecision(rotation=0, ocr=None, settled=True)

    (page.dir / "page.jpg").unlink()
    with pytest.raises(FileNotFoundError):
        decide_orientation(page)


def test_without_tesseract_nothing_is_settled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    (page,) = make_pages(tmp_path)

    assert orient_page(page) == page.meta
    assert page.meta.oriented is False


def test_a_turn_rewrites_the_page_inside_the_write_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (page,) = make_pages(tmp_path, data=card_image(size=(600, 800)))
    calls: list[tuple[Path, float]] = []

    def extract_text(path: Path, *, min_ratio: float = 1.0) -> ocr.OCRResult:
        calls.append((path, min_ratio))
        return ocr.OCRResult(text="Banana Mug Cake", confidence=61.0, rotation=90)

    locked: list[str] = []

    @contextmanager
    def ingest_write() -> Iterator[None]:
        locked.append("enter")
        yield
        locked.append("exit")

    monkeypatch.setattr(ocr, "is_available", lambda: True)
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
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", lambda path, *, min_ratio=1.0: ocr.OCRResult(text="Soup", confidence=90.0))

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

    assert (settled.oriented, settled.rotation, settled.ocr) == (True, 0, PageOCR(text="Soup", confidence=80.0))


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
