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

MIN_TURN_SCORE = 500.0
"""
Fork: with a margin (`min_ratio` over 1), the best rotation must also score at least this much to win. A blank or
nearly blank page reads as a few specks whichever way up, and any ratio over an upright score of 0 would turn it;
the sideways banana card's wrong readings score about 300, its right one over 8000 (docs/ai/PHASE2.md F7).
"""

LINE_HEIGHT = 40
"""
Fork: the height in pixels a line is scaled to before `read_line` reads it again. A printed card's line is 80 to 100
pixels high at the page's scale, where Tesseract reads an italic "1" as "7"; at this height it reads it right: on 54
live printed card renders, every number this reading found was the card's (cropped to the digit alone and read for
digits only, Tesseract got one in ten wrong).
"""

LINE_MARGIN = 0.4
"""Fork: the margin `read_line` keeps around a line's box, in line heights (a box hugs its letters)"""

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
class OCRLine:
    """
    Fork: a line of text Tesseract found, and the box around its words in fractions (0 to 1) of the image's width and
    height as it was read, turned by `OCRResult.rotation`
    """

    text: str
    x: float
    y: float
    width: float
    height: float


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

    failed: bool = False
    """
    Fork: Tesseract timed out or failed, so the empty result says nothing about the image (a caller that settles
    something for good, like a page's orientation, tries again later)
    """

    lines: tuple[OCRLine, ...] = ()
    """Fork: the lines found, in reading order, each with its box (so a caller can point at where a line is)"""


@dataclass(frozen=True)
class _Word:
    block: int
    paragraph: int
    line: int
    width: int
    height: int
    confidence: float
    text: str
    left: int = 0
    top: int = 0

    @property
    def characters(self) -> int:
        return sum(char.isalnum() for char in self.text)


@functools.cache
def _tesseract_path() -> str | None:
    return shutil.which("tesseract")


def is_available() -> bool:
    """Whether OCR is enabled and the `tesseract` command can be found"""

    return get_app_settings().OCR_ENABLED and binary_available()


def binary_available() -> bool:
    """
    Fork: whether the `tesseract` command can be found, whatever `OCR_ENABLED` says. `OCR_ENABLED` is about reading
    recipes with OCR; a caller using Tesseract for something else (turning a recipe card upright) has its own switch.
    """

    return _tesseract_path() is not None


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
                left=int(fields[6]),
                top=int(fields[7]),
            )
        except ValueError:
            continue

        if word.confidence >= 0:
            words.append(word)

    return words


def _read_words(image: Image.Image, work_dir: Path, deadline: float, *, single_line: bool = False) -> list[_Word]:
    tesseract = _tesseract_path()
    if not tesseract:
        return []

    image_path = work_dir / "image.png"
    image.save(image_path, dpi=(300, 300))

    args = [tesseract, str(image_path), "stdout", "-l", get_app_settings().OCR_LANGUAGES]
    if single_line:
        args += ["--psm", "7"]  # fork: `read_line`
    args.append("tsv")
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
    asks for a margin. Fork: the margin (docs/ai/PHASE2.md §4.4), which also needs `MIN_TURN_SCORE`; with the
    default of 1 the best rotation wins.
    """
    best = max(scores, key=scores.__getitem__)
    if min_ratio > 1 and scores[best] < MIN_TURN_SCORE:
        return 0
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


def _to_lines(words: list[_Word], size: tuple[int, int]) -> tuple[OCRLine, ...]:
    """
    Fork: the words grouped into Tesseract's lines (by block, paragraph and line, as `_to_text` joins them), each with
    the box around its words in fractions of `size`, the image's width and height as it was read
    """
    image_width, image_height = size
    if image_width <= 0 or image_height <= 0:
        return ()

    grouped: list[list[_Word]] = []
    previous: tuple[int, int, int] | None = None
    for word in words:
        position = (word.block, word.paragraph, word.line)
        if position == previous:
            grouped[-1].append(word)
        else:
            grouped.append([word])
        previous = position

    lines: list[OCRLine] = []
    for group in grouped:
        left = _fraction(min(word.left for word in group), image_width)
        top = _fraction(min(word.top for word in group), image_height)
        right = _fraction(max(word.left + word.width for word in group), image_width)
        bottom = _fraction(max(word.top + word.height for word in group), image_height)
        text = " ".join(word.text for word in group)
        lines.append(OCRLine(text=text, x=left, y=top, width=round(right - left, 4), height=round(bottom - top, 4)))
    return tuple(lines)


def _fraction(value: int, whole: int) -> float:
    return round(min(max(value / whole, 0.0), 1.0), 4)


def extract_text(path: Path, *, min_ratio: float = 1.0, require_enabled: bool = True) -> OCRResult:
    """
    Reads the text in an image, whichever way up it was photographed. This blocks, so run it off
    the event loop. Returns an empty result if OCR is unavailable or the image can't be read.

    Fork: the image is turned only when the best rotation scores at least `min_ratio` times the upright
    one (docs/ai/PHASE2.md §4.4), and the text is read at the rotation chosen. The default of 1 keeps
    upstream's behaviour: the best rotation wins. `require_enabled=False` reads whenever Tesseract is
    installed, even with `OCR_ENABLED` off (recipe card orientation has its own switch).
    """

    if not (is_available() if require_enabled else binary_available()):
        return OCRResult()

    deadline = time.monotonic() + get_app_settings().OCR_TIMEOUT
    try:
        image = _prepare(path)
        with tempfile.TemporaryDirectory(prefix="mealie-ocr-") as work_dir:
            read = functools.partial(_read_words, work_dir=Path(work_dir), deadline=deadline)
            scores = _probe_rotations(image, read)
            rotation = _choose_rotation(scores, min_ratio)
            long_side = min(max(max(image.size), MIN_DIMENSION), MAX_DIMENSION)
            turned = _fit(_rotate(image, rotation), long_side)
            words = read(turned)
    except subprocess.TimeoutExpired:
        logger.warning(f"OCR timed out reading {path.name}")
        return OCRResult(failed=True)
    except subprocess.CalledProcessError as e:
        logger.warning(f"Tesseract failed to read {path.name}: {(e.stderr or '').strip()}")
        return OCRResult(failed=True)
    except Exception:
        logger.exception(f"Failed to read {path.name} with OCR")
        return OCRResult(failed=True)

    if not words:
        return OCRResult(rotation=rotation, rotation_scores=scores)

    confidence = sum(word.confidence for word in words) / len(words)
    logger.debug(f"OCR read {len(words)} words from {path.name} (rotation {rotation}, confidence {confidence:.0f})")
    return OCRResult(
        text=_to_text(words),
        confidence=confidence,
        rotation=rotation,
        rotation_scores=scores,
        lines=_to_lines(words, turned.size),
    )


def read_line(path: Path, x: float, y: float, width: float, height: float) -> str | None:
    """
    Fork: one line of an image read again on its own, in Tesseract's single-line mode: its box (`x`, `y`, `width` and
    `height` in fractions of the image's width and height, as `OCRLine` gives them) with a margin (`LINE_MARGIN`),
    scaled so the line is `LINE_HEIGHT` pixels high. A second reading of a number the page's reading may have got
    wrong. None when Tesseract isn't installed, the box holds nothing, or the read fails or times out (`OCR_TIMEOUT`).
    Blocking: run it off the event loop.
    """
    if not binary_available() or width <= 0 or height <= 0:
        return None

    deadline = time.monotonic() + get_app_settings().OCR_TIMEOUT
    try:
        image = _prepare(path)
        margin = height * image.height * LINE_MARGIN
        box = (
            max(0, round(x * image.width - margin)),
            max(0, round(y * image.height - margin)),
            min(image.width, round((x + width) * image.width + margin)),
            min(image.height, round((y + height) * image.height + margin)),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        line = image.crop(box)
        scale = LINE_HEIGHT / max(height * image.height, 1.0)
        line = line.resize(
            (max(1, round(line.width * scale)), max(1, round(line.height * scale))), Image.Resampling.LANCZOS
        )
        with tempfile.TemporaryDirectory(prefix="mealie-ocr-") as work_dir:
            words = _read_words(line, Path(work_dir), deadline, single_line=True)
    except subprocess.TimeoutExpired:
        logger.warning(f"OCR timed out reading a line of {path.name} again")
        return None
    except subprocess.CalledProcessError as e:
        logger.warning(f"Tesseract failed to read a line of {path.name} again: {(e.stderr or '').strip()}")
        return None
    except Exception:
        logger.exception(f"Failed to read a line of {path.name} again with OCR")
        return None

    return " ".join(word.text for word in words) or None
