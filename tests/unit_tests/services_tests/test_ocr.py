import shutil
import subprocess
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError

import mealie.services.ocr.tesseract as tesseract_module
from mealie.core.config import get_app_settings
from mealie.services import ocr
from tests import data as test_data

requires_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")

TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def tsv_row(block: int, paragraph: int, line: int, text: str, conf: float = 90, width: int = 100, height: int = 30):
    return f"5\t1\t{block}\t{paragraph}\t{line}\t1\t0\t0\t{width}\t{height}\t{conf}\t{text}"


@pytest.fixture()
def app_env(monkeypatch: pytest.MonkeyPatch) -> Generator[Callable[..., None]]:
    """Sets environment variables and reloads the app settings, restoring them afterwards."""

    def apply(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_app_settings.cache_clear()

    yield apply
    get_app_settings.cache_clear()


@pytest.fixture()
def default_ocr_settings(app_env: Callable[..., None]) -> None:
    """Reads with the default OCR settings, whatever the environment or `.env` sets."""

    app_env(OCR_ENABLED="true", OCR_LANGUAGES="eng", OCR_TIMEOUT="60")


@pytest.fixture()
def fake_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Makes OCR look available whether or not Tesseract is installed."""

    monkeypatch.setattr(tesseract_module, "_tesseract_path", lambda: "/usr/bin/tesseract")


@pytest.mark.parametrize("languages", ["eng", "eng+deu", "chi_sim+eng", "script/Latin", " eng "])
def test_ocr_languages_accepts_language_codes(app_env: Callable[..., None], languages: str):
    app_env(OCR_LANGUAGES=languages)
    assert get_app_settings().OCR_LANGUAGES == languages.strip()


@pytest.mark.parametrize(
    "languages",
    ["", "eng deu", "eng+", "+eng", "-l", "--psm 0", "eng;rm", "../eng", "/eng", "eng+deu --oem 0"],
)
def test_ocr_languages_rejects_anything_else(app_env: Callable[..., None], languages: str):
    app_env(OCR_LANGUAGES=languages)
    with pytest.raises(ValidationError):
        get_app_settings()


@pytest.mark.parametrize("timeout", ["0", "-5"])
def test_ocr_timeout_must_be_positive(app_env: Callable[..., None], timeout: str):
    app_env(OCR_TIMEOUT=timeout)
    with pytest.raises(ValidationError):
        get_app_settings()


def test_ocr_can_be_disabled(app_env: Callable[..., None], fake_tesseract: None, monkeypatch: pytest.MonkeyPatch):
    def fail_if_run(*_, **__):
        raise AssertionError("tesseract should not run while OCR is disabled")

    monkeypatch.setattr(tesseract_module.subprocess, "run", fail_if_run)

    app_env(OCR_ENABLED="false")
    assert not ocr.is_available()
    assert ocr.extract_text(Path("recipe.jpg")) == ocr.OCRResult()

    app_env(OCR_ENABLED="true")
    assert ocr.is_available()


def test_ocr_is_unavailable_without_tesseract(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tesseract_module, "_tesseract_path", lambda: None)

    assert not ocr.is_available()
    assert ocr.extract_text(Path("recipe.jpg")) == ocr.OCRResult()


def test_an_unreadable_image_gives_an_empty_result(fake_tesseract: None, tmp_path: Path):
    not_an_image = tmp_path / "recipe.jpg"
    not_an_image.write_text("not an image")

    assert ocr.extract_text(not_an_image) == ocr.OCRResult(failed=True)


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, "tesseract", stderr="Failed loading language 'deu'"),
        subprocess.TimeoutExpired("tesseract", 60),
        OSError("exec format error"),
    ],
)
def test_a_failing_tesseract_gives_an_empty_result(
    fake_tesseract: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
):
    def fail(*_, **__):
        raise error

    monkeypatch.setattr(tesseract_module.subprocess, "run", fail)

    image_path = tmp_path / "recipe.png"
    Image.new("RGB", (200, 100), "white").save(image_path)

    # empty, and marked as a failure rather than an image with no text (fork: orientation tries again later)
    assert ocr.extract_text(image_path) == ocr.OCRResult(failed=True)


@pytest.mark.parametrize(("configured", "expected"), [(None, "1"), ("4", "4")])
def test_tesseract_runs_single_threaded_unless_configured(
    fake_tesseract: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: str | None,
    expected: str,
):
    if configured is None:
        monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    else:
        monkeypatch.setenv("OMP_THREAD_LIMIT", configured)

    thread_limits: list[str] = []

    def record(args: list[str], **kwargs) -> subprocess.CompletedProcess:
        thread_limits.append(kwargs["env"]["OMP_THREAD_LIMIT"])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(tesseract_module.subprocess, "run", record)

    image_path = tmp_path / "recipe.png"
    Image.new("RGB", (200, 100), "white").save(image_path)
    ocr.extract_text(image_path)

    assert thread_limits
    assert set(thread_limits) == {expected}


def test_tsv_words_are_joined_into_lines_and_blocks():
    tsv = "\n".join(
        [
            TSV_HEADER,
            # rows above word level carry no text
            "4\t1\t1\t1\t1\t0\t0\t0\t500\t30\t-1\t",
            tsv_row(1, 1, 1, "Banana"),
            tsv_row(1, 1, 1, "Bread"),
            tsv_row(1, 1, 2, "2"),
            tsv_row(1, 1, 2, "bananas"),
            tsv_row(2, 1, 1, "Mix"),
            tsv_row(2, 1, 1, " "),
        ]
    )

    words = tesseract_module._parse_tsv(tsv)

    assert [word.text for word in words] == ["Banana", "Bread", "2", "bananas", "Mix"]
    assert tesseract_module._to_text(words) == "Banana Bread\n2 bananas\n\nMix"


def test_sideways_words_do_not_count_towards_the_orientation():
    upright = tesseract_module._parse_tsv("\n".join([TSV_HEADER, tsv_row(1, 1, 1, "banana", conf=60)]))
    sideways = tesseract_module._parse_tsv(
        "\n".join([TSV_HEADER, tsv_row(1, 1, 1, "banana", conf=95, width=30, height=100)])
    )

    assert tesseract_module._orientation_score(upright) > tesseract_module._orientation_score(sideways) == 0


def test_a_few_confident_specks_do_not_outscore_real_text():
    text = tesseract_module._parse_tsv(
        "\n".join([TSV_HEADER, tsv_row(1, 1, 1, "Mash", conf=70), tsv_row(1, 1, 1, "bananas", conf=70)])
    )
    specks = tesseract_module._parse_tsv("\n".join([TSV_HEADER, tsv_row(1, 1, 1, "ii", conf=96)]))

    assert tesseract_module._orientation_score(text) > tesseract_module._orientation_score(specks)


@requires_tesseract
def test_reads_rotated_text(default_ocr_settings: None, tmp_path: Path):
    lines = ["Banana Mug Cake", "1 ripe banana", "2 tablespoons coconut oil", "Microwave until firm"]

    image = Image.new("RGB", (1400, 520), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=56)
    for index, line in enumerate(lines):
        draw.text((60, 50 + index * 110), line, fill="black", font=font)

    # turned a quarter counter-clockwise, as if photographed sideways
    image_path = tmp_path / "sideways.png"
    image.transpose(Image.Transpose.ROTATE_90).save(image_path)

    result = ocr.extract_text(image_path)

    assert result.rotation == 90
    assert result.confidence > 50
    for line in lines:
        assert line in result.text


@requires_tesseract
def test_finds_which_way_up_a_handwritten_card_is(default_ocr_settings: None):
    """
    Tesseract reads handwriting poorly, so this only checks the orientation: the card was
    photographed sideways and has to be turned a quarter either way to be read.
    """

    result = ocr.extract_text(test_data.CWD / "cards" / "banana-mug-cake.jpg")

    assert result.rotation in (90, 270)
    assert result.text
