"""
On-device text recognition with Tesseract.

Runs the `tesseract` command rather than binding to it, so it adds no Python dependency and is simply
unavailable when Tesseract isn't installed. Reading is best-effort: a failure is logged and returns an
empty result, never an exception.
"""

import functools
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from mealie.core.config import get_app_settings
from mealie.core.root_logger import get_logger

register_heif_opener()

logger = get_logger(__name__)

PROBE_DIMENSION = 1200
"""Long side of the downscaled copy that's read to work out which way up the image is"""

MIN_DIMENSION = 1500
"""Smaller images are scaled up before they're read for their text, since Tesseract misses small text"""

MAX_DIMENSION = 3000
"""Larger images are scaled down before they're read for their text; Tesseract gains nothing from huge photos"""

ROTATIONS = (0, 90, 180, 270)

OMP_THREAD_LIMIT = "1"
"""
Threads each Tesseract process may use, unless the environment already sets `OMP_THREAD_LIMIT`.
By default Tesseract starts a thread per core, and processes that overlap (several imports at once,
or other busy containers on the host) slow each other down to the point of timing out. One thread
is also faster for a single image this size.
"""
_TRANSPOSE = {
    # Pillow's ROTATE_* turn counter-clockwise
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


@dataclass(frozen=True)
class OCRResult:
    text: str = ""
    """The text read, one line per line found, with a blank line between paragraphs"""

    confidence: float = 0.0
    """Mean word confidence, from 0 to 100"""

    rotation: int = 0
    """Degrees the image was turned clockwise to read it"""

    rotation_scores: dict[int, float] = field(default_factory=dict)
    """
    Fork: how well the image read at each rotation it was probed at (see `_orientation_score`), so a caller can see
    how sure the chosen rotation was. Empty when nothing was probed.
    """


@dataclass(frozen=True)
class _Word:
    block: int
    paragraph: int
    line: int
    width: int
    height: int
    confidence: float
    text: str

    @property
    def characters(self) -> int:
        return sum(char.isalnum() for char in self.text)


@functools.cache
def _tesseract_path() -> str | None:
    return shutil.which("tesseract")


def is_available() -> bool:
    """Whether OCR is enabled and the `tesseract` command can be found"""

    return get_app_settings().OCR_ENABLED and _tesseract_path() is not None


def _prepare(path: Path) -> Image.Image:
    with Image.open(path) as image:
        upright = ImageOps.exif_transpose(image)
        if upright.mode in ("RGBA", "LA", "PA") or "transparency" in upright.info:
            # transparent pixels are often black underneath, which would hide dark text
            rgba = upright.convert("RGBA")
            upright = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)

        return ImageOps.autocontrast(upright.convert("L"), cutoff=1)


def _fit(image: Image.Image, long_side: int) -> Image.Image:
    scale = long_side / max(image.size)
    if scale == 1:
        return image

    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _rotate(image: Image.Image, rotation: int) -> Image.Image:
    return image.transpose(_TRANSPOSE[rotation]) if rotation else image


def _parse_tsv(tsv: str) -> list[_Word]:
    words: list[_Word] = []
    for row in tsv.splitlines()[1:]:
        # level, page, block, paragraph, line, word, left, top, width, height, confidence, text
        fields = row.split("\t", 11)
        if len(fields) < 12 or fields[0] != "5" or not fields[11].strip():
            continue

        try:
            word = _Word(
                block=int(fields[2]),
                paragraph=int(fields[3]),
                line=int(fields[4]),
                width=int(fields[8]),
                height=int(fields[9]),
                confidence=float(fields[10]),
                text=fields[11].strip(),
            )
        except ValueError:
            continue

        if word.confidence >= 0:
            words.append(word)

    return words


def _read_words(image: Image.Image, work_dir: Path, deadline: float) -> list[_Word]:
    tesseract = _tesseract_path()
    if not tesseract:
        return []

    image_path = work_dir / "image.png"
    image.save(image_path, dpi=(300, 300))

    args = [tesseract, str(image_path), "stdout", "-l", get_app_settings().OCR_LANGUAGES, "tsv"]
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise subprocess.TimeoutExpired(args, 0)

    env = {**os.environ, "OMP_THREAD_LIMIT": os.environ.get("OMP_THREAD_LIMIT", OMP_THREAD_LIMIT)}
    result = subprocess.run(
        args, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, check=True, env=env
    )
    return _parse_tsv(result.stdout)


def _orientation_score(words: list[_Word]) -> float:
    """
    How plausibly the words were read the right way up: their confidence, weighted by how many characters
    they hold so that a few confident specks don't outscore real text. Only words laid out horizontally
    count, since Tesseract will also read a sideways line, and often with high confidence.
    """

    return sum(word.confidence * word.characters for word in words if word.characters > 1 and word.width >= word.height)


def _probe_rotations(image: Image.Image, read: Callable[[Image.Image], list[_Word]]) -> dict[int, float]:
    # Tesseract's own orientation detection (--psm 0) gives up on handwriting, so read
    # a small copy every way up and score each reading
    probe = _fit(image, PROBE_DIMENSION)
    return {rotation: _orientation_score(read(_rotate(probe, rotation))) for rotation in ROTATIONS}


def _choose_rotation(scores: dict[int, float], min_ratio: float = 1.0) -> int:
    """
    The best-scoring rotation, but only when it scores at least `min_ratio` times the upright one; otherwise 0.
    Handwriting scores low every way up, so a caller that turns the image for good (rather than just reading it)
    asks for a margin. Fork: the margin (docs/ai/PHASE2.md §4.4); with the default of 1 the best rotation wins.
    """
    best = max(scores, key=scores.__getitem__)
    if best and scores[best] >= min_ratio * scores.get(0, 0.0):
        return best
    return 0


def _to_text(words: list[_Word]) -> str:
    lines: list[str] = []
    previous: tuple[int, int, int] | None = None
    for word in words:
        position = (word.block, word.paragraph, word.line)
        if previous and position[:2] != previous[:2]:
            lines.append("")

        if position == previous:
            lines[-1] += f" {word.text}"
        else:
            lines.append(word.text)

        previous = position

    return "\n".join(lines)


def extract_text(path: Path, *, min_ratio: float = 1.0) -> OCRResult:
    """
    Reads the text in an image, whichever way up it was photographed. This blocks, so run it off
    the event loop. Returns an empty result if OCR is unavailable or the image can't be read.

    Fork: the image is turned only when the best rotation scores at least `min_ratio` times the upright
    one (docs/ai/PHASE2.md §4.4), and the text is read at the rotation chosen. The default of 1 keeps
    upstream's behaviour: the best rotation wins.
    """

    if not is_available():
        return OCRResult()

    deadline = time.monotonic() + get_app_settings().OCR_TIMEOUT
    try:
        image = _prepare(path)
        with tempfile.TemporaryDirectory(prefix="mealie-ocr-") as work_dir:
            read = functools.partial(_read_words, work_dir=Path(work_dir), deadline=deadline)
            scores = _probe_rotations(image, read)
            rotation = _choose_rotation(scores, min_ratio)
            long_side = min(max(max(image.size), MIN_DIMENSION), MAX_DIMENSION)
            words = read(_fit(_rotate(image, rotation), long_side))
    except subprocess.TimeoutExpired:
        logger.warning(f"OCR timed out reading {path.name}")
        return OCRResult()
    except subprocess.CalledProcessError as e:
        logger.warning(f"Tesseract failed to read {path.name}: {(e.stderr or '').strip()}")
        return OCRResult()
    except Exception:
        logger.exception(f"Failed to read {path.name} with OCR")
        return OCRResult()

    if not words:
        return OCRResult(rotation=rotation, rotation_scores=scores)

    confidence = sum(word.confidence for word in words) / len(words)
    logger.debug(f"OCR read {len(words)} words from {path.name} (rotation {rotation}, confidence {confidence:.0f})")
    return OCRResult(text=_to_text(words), confidence=confidence, rotation=rotation, rotation_scores=scores)
