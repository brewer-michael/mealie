"""
Where on a card a field's text probably is: the review page's re-read selection starts there, rather than as a band
across the middle of the card (docs/ai/PHASE2.md §6.5). Pure: it reads only what is stored with the job.

1. Tesseract's lines (`PageOCR.lines`, stored when orientation read the page, with boxes in fractions of the upright
   page): the line most like the text (`partial_ratio` of at least `OCR_MIN_SCORE`), with the lines next to it that
   hold more of a longer text, widened to a band across the card (`BAND_WIDTH`) half a line above and below.
   A text too short to be told from its neighbours this way (under `MIN_MATCH_CHARACTERS` once its markers are out:
   "1 C. [illegible]" is "1 c.", like every "1 C. ..." line) is found between the Tesseract lines most like the
   transcription's lines before and after it as a whole, whatever Tesseract made of it ("1G eae"), or in the gap
   between them when it read nothing there; else (without a marker) at a Tesseract line that says about what it
   says, as a whole (`fuzz.ratio`: "3 eggs" is in many a step, "1. Beat eggs, sugar, ..."); else just below the line
   before it, or just above the line after, when Tesseract read only one of them (a heading it didn't read above "3
   eggs"). It's never looked for inside longer lines, which put it on a step on the card's other side.
2. Else the text's line in the transcription: its place among the lines of its page's part of the transcription
   ("Front:", "Back:" and the like start a page's part; without them, a card's pages share its lines in order) gives
   a band `POSITION_BAND_HEIGHT` high at that place on that page; for a short text, the transcription's line that
   says exactly what it says.

A short line the card says twice ("1 egg" in the cake and in the frosting) is found by which of them it is
(`occurrence`), never always at the first.

None when neither finds the text (the reviewer typed it, or the card has no transcription).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from mealie.schema.recipe_ingest import OCRLine, PageMeta, RegionHintSource

from .cardtext import MARKER_RE

OCR_MIN_SCORE = 70
"""A Tesseract line points at the text when `partial_ratio` scores it at least this much"""
TRANSCRIPTION_MIN_SCORE = 70
"""Likewise for a line of the transcription"""
MIN_MATCH_CHARACTERS = 8
"""
A line is compared with a longer text only when it has at least this many characters: `partial_ratio` would find a
short one ("Salt", "350") inside almost any step
"""
BAND_WIDTH = 0.9
"""A hint's width, in fractions of the page's width, centred: across the card, since columns and margins vary"""
MIN_BAND_HEIGHT = 0.05
POSITION_BAND_HEIGHT = 0.12
"""The height of a hint placed by the text's position in the transcription, which is only an estimate"""
EXTEND_BELOW_SHARE = 0.8
"""A best line holding less than this share of the text's characters is joined by the lines around it that hold more"""

_SPACES = re.compile(r"\s+")
_LIST_MARKER = re.compile(r"^[\s#*_>•-]+")
"""What starts a transcription's line before its text: a list or heading marker ("- 1 C. sugar", "## Ingredients")"""
_PAGE_HEADER = re.compile(
    r"^[\s#*_>-]*(?:(?P<front>front)|(?P<back>back)|(?P<next>next page)|(?:image|page)\s+(?P<number>\d+))"
    r"(?:\s*\((?:front|back)\))?(?:\s+(?:of\s+(?:the\s+)?card|side))?[\s*_]*:?[\s*_]*$",
    re.IGNORECASE,
)
"""A line that starts a page's part of a transcription: "Front:", "**Back:**", "## Image 2 (back)", "Next page:" """


@dataclass(frozen=True)
class RegionHint:
    """A band on an upright page, in fractions of its width and height, where a field's text probably is"""

    page: int
    """The page's `index`"""
    x: float
    y: float
    width: float
    height: float
    source: RegionHintSource


def _comparable(text: str) -> str:
    """Text as lines are compared: no markers, lower case, single spaces"""
    return _SPACES.sub(" ", MARKER_RE.sub(" ", text)).strip().lower()


def _score(target: str, line: str, *, whole: bool = False) -> float:
    """
    How much of `line` is like part of `target` (or the reverse), or 0 for a line too short to tell; `whole`: how
    much the whole line, without a list marker, is like the whole text (for a short text, found in many a longer line)
    """
    if not target or not line or min(len(target), len(line)) < min(len(target), MIN_MATCH_CHARACTERS):
        return 0.0
    if whole:
        return fuzz.ratio(target, _LIST_MARKER.sub("", line))
    return fuzz.partial_ratio(target, line)


def _band(page: int, top: float, bottom: float, line_height: float, source: RegionHintSource) -> RegionHint:
    """A band across the page from half a line above `top` to half a line below `bottom`, kept on the page"""
    start, end = top - line_height / 2, bottom + line_height / 2
    if end - start < MIN_BAND_HEIGHT:
        middle = (start + end) / 2
        start, end = middle - MIN_BAND_HEIGHT / 2, middle + MIN_BAND_HEIGHT / 2
    height = min(end - start, 1.0)
    y = min(max(start, 0.0), 1.0 - height)
    return RegionHint(
        page=page,
        x=round((1 - BAND_WIDTH) / 2, 4),
        y=round(y, 4),
        width=BAND_WIDTH,
        height=round(height, 4),
        source=source,
    )


def _ocr_hint(pages: Sequence[PageMeta], target: str, *, whole: bool = False, occurrence: int = 0) -> RegionHint | None:
    """
    The band around the Tesseract line most like `target` (`_score`), and the lines around it that hold more of it;
    of lines alike as much, the `occurrence`th (a line the card says twice)
    """
    found: list[tuple[float, PageMeta, int]] = []
    for page in pages:
        for index, line in enumerate(page.ocr.lines if page.ocr else []):
            score = _score(target, _comparable(line.text), whole=whole)
            if score >= OCR_MIN_SCORE:
                found.append((score, page, index))
    if not found:
        return None

    best = max(score for score, _, _ in found)
    alike = [(page, index) for score, page, index in found if score == best]
    page, index = alike[min(occurrence, len(alike) - 1)]
    assert page.ocr is not None
    lines = page.ocr.lines
    chosen: list[OCRLine] = [lines[index]]
    first, last = index, index

    def held() -> int:
        return sum(len(_comparable(line.text)) for line in chosen)

    # a text longer than its best line (a step written over several lines): the lines around it that hold more of it
    while held() < EXTEND_BELOW_SHARE * len(target):
        before = lines[first - 1] if first > 0 else None
        after = lines[last + 1] if last + 1 < len(lines) else None
        scores = [
            (_score(target, _comparable(line.text)), offset, line)
            for offset, line in ((-1, before), (1, after))
            if line is not None
        ]
        scores = [entry for entry in scores if entry[0] >= OCR_MIN_SCORE]
        if not scores:
            break
        _, offset, line = max(scores, key=lambda entry: entry[0])
        chosen.append(line)
        first, last = (first - 1, last) if offset < 0 else (first, last + 1)

    top = min(line.y for line in chosen)
    bottom = max(line.y + line.height for line in chosen)
    return _band(page.index, top, bottom, lines[index].height, RegionHintSource.ocr)


def _as_written(text: str) -> str:
    """A line as the transcription and the draft both write it: markers kept, without a list marker, lower case"""
    return _SPACES.sub(" ", _LIST_MARKER.sub("", text)).strip().lower()


def _target_line(lines: Sequence[str], target_text: str, occurrence: int = 0) -> int | None:
    """
    Which of the transcription's `lines` is the target's own: the one saying exactly what it says, else holding it;
    of several, the `occurrence`th (the frosting's "1 egg" is the card's second), else the last
    """
    wanted = _as_written(target_text)
    if not wanted:
        return None
    written = [_as_written(text) for text in lines]
    for same in (
        [i for i, text in enumerate(written) if text == wanted],
        [i for i, text in enumerate(written) if wanted in text],
    ):
        if same:
            return same[min(occurrence, len(same) - 1)]
    return None


def _scores(page: PageMeta, text: str) -> list[float]:
    """
    How much each of the Tesseract lines of `page` is like `text` (a transcription's line), as a whole: "2 cups
    powdered sugar" holds much of "2 cups flour", and "Buttermilk Pancakes" much of "1 cup buttermilk"
    """
    target = _comparable(_LIST_MARKER.sub("", text))
    return [_score(target, _comparable(line.text), whole=True) for line in (page.ocr.lines if page.ocr else [])]


def _neighbours(transcription: str, target_text: str, occurrence: int) -> tuple[str | None, str | None] | None:
    """The transcription's lines right before and after the target's own (None at either end), or None without it"""
    lines = [raw.strip() for raw in transcription.splitlines() if raw.strip() and not _PAGE_HEADER.match(raw)]
    index = _target_line(lines, target_text, occurrence)
    if index is None:
        return None
    return (lines[index - 1] if index else None), (lines[index + 1] if index + 1 < len(lines) else None)


def _between_neighbours(
    pages: Sequence[PageMeta], transcription: str, target_text: str, occurrence: int = 0
) -> RegionHint | None:
    """
    For a text too short to find among Tesseract's lines by itself: the band between the Tesseract lines most like the
    transcription's lines right before and after its own, the first above the second, on the page where the two are
    most alike (the closest such pair): the lines Tesseract read between them, or the gap there when it read none
    """
    neighbours = _neighbours(transcription, target_text, occurrence)
    if neighbours is None or None in neighbours:
        return None
    before, after = neighbours
    assert before is not None and after is not None

    best: tuple[float, int, PageMeta, int, int] | None = None  # score, -gap, page, line above, line below
    for page in pages:
        above, below = _scores(page, before), _scores(page, after)
        for first, first_score in enumerate(above):
            if first_score < OCR_MIN_SCORE:
                continue
            for second in range(first + 1, len(below)):
                if below[second] < OCR_MIN_SCORE:
                    continue
                candidate = (first_score + below[second], first - second, page, first, second)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
    if best is None:
        return None

    _, _, page, first, second = best
    assert page.ocr is not None
    ocr_lines = page.ocr.lines
    if between := ocr_lines[first + 1 : second]:
        top, bottom = min(line.y for line in between), max(line.y + line.height for line in between)
        return _band(page.index, top, bottom, between[0].height, RegionHintSource.ocr)
    # Tesseract read nothing there: the gap between the two lines
    upper, lower = ocr_lines[first], ocr_lines[second]
    return _band(page.index, upper.y + upper.height, lower.y, upper.height, RegionHintSource.ocr)


def _beside_a_neighbour(
    pages: Sequence[PageMeta], transcription: str, target_text: str, occurrence: int = 0
) -> RegionHint | None:
    """
    For a short text whose neighbours Tesseract didn't both read (a heading above "3 eggs", a line of handwriting it
    couldn't): a line's height just below the Tesseract line most like the transcription's line before it, or just
    above the one most like the line after it (`_scores`), on that line's page
    """
    neighbours = _neighbours(transcription, target_text, occurrence)
    if neighbours is None:
        return None
    best: tuple[float, PageMeta, int, bool] | None = None  # score, page, its line, whether it's the line before
    for neighbour, is_before in zip(neighbours, (True, False), strict=True):
        for page in pages if neighbour is not None else []:
            for index, score in enumerate(_scores(page, neighbour or "")):
                if score >= OCR_MIN_SCORE and (best is None or score > best[0]):
                    best = (score, page, index, is_before)
    if best is None:
        return None

    _, page, index, is_before = best
    assert page.ocr is not None
    line = page.ocr.lines[index]
    top = line.y + line.height if is_before else line.y - line.height
    return _band(page.index, top, top + line.height, 0.0, RegionHintSource.ocr)


def _page_of(header: re.Match[str], current: int) -> int:
    if header.group("front"):
        return 0
    if header.group("back"):
        return 1
    if header.group("number"):
        return max(int(header.group("number")) - 1, 0)
    return current + 1  # "Next page"


def _sections(transcription: str, page_count: int) -> list[tuple[int, list[str]]]:
    """
    The transcription's lines by page, as `(page position, lines)`: each "Front:", "Back:", "Image 2" or "Next page"
    line starts a page's part. Without any, the card's pages share its lines in order, as evenly as they go.
    """
    sections: list[tuple[int, list[str]]] = []
    current: list[str] = []
    page = 0
    headed = False
    for raw in transcription.splitlines():
        if not raw.strip():
            continue
        if header := _PAGE_HEADER.match(raw):
            if current or headed:
                sections.append((page, current))
            page, current, headed = _page_of(header, page), [], True
            continue
        current.append(raw.strip())
    if current or headed:
        sections.append((page, current))

    if headed or page_count <= 1 or not sections:
        return [(page, lines) for page, lines in sections if lines]

    lines = sections[0][1]
    size = -(-len(lines) // page_count)  # rounded up
    return [(position, lines[start : start + size]) for position, start in enumerate(range(0, len(lines), size))]


def _position_hint(
    pages: Sequence[PageMeta], transcription: str, target: str, *, exactly: str | None = None, occurrence: int = 0
) -> RegionHint | None:
    """
    A band at the text's place in its page's part of the transcription: the line most like it, or with `exactly` (a
    short text, like every "1 C. ..." line once its marker is out) the line saying exactly that, the `occurrence`th
    of several
    """
    best: tuple[float, int, int, int] | None = None  # score, page position, line index, lines in the section
    same: list[tuple[float, int, int, int]] = []
    for position, lines in _sections(transcription, len(pages)):
        for index, line in enumerate(lines):
            if exactly is not None:
                if _as_written(line) == _as_written(exactly):
                    same.append((100.0, position, index, len(lines)))
                continue
            score = _score(target, _comparable(line))
            if score >= TRANSCRIPTION_MIN_SCORE and (best is None or score > best[0]):
                best = (score, position, index, len(lines))
    if same:
        best = same[min(occurrence, len(same) - 1)]
    if best is None:
        return None

    _, position, index, count = best
    page = pages[min(position, len(pages) - 1)]
    middle = (index + 0.5) / count
    top = middle - POSITION_BAND_HEIGHT / 2
    return _band(page.index, top, top + POSITION_BAND_HEIGHT, 0.0, RegionHintSource.position)


def region_hint(
    pages: Sequence[PageMeta], transcription: str | None, target_text: str, occurrence: int = 0
) -> RegionHint | None:
    """
    Where on the card `target_text` (a field's text as the draft has it) probably is: by Tesseract's lines, else by
    its place in the transcription (see the module docstring); None when neither finds it. `occurrence`: which of the
    field's lines saying the same it is (0 for the first), for a short line the card says twice.
    """
    target = _comparable(target_text)
    ordered = sorted(pages, key=lambda page: page.index)
    if not target or not ordered:
        return None
    text = transcription if transcription and transcription.strip() else None
    if text is not None and len(target) < MIN_MATCH_CHARACTERS:
        # too short to be told from the lines around it ("1 C. [illegible]" is "1 c."): found between them, never
        # inside a longer line ("3 eggs" in "1. Beat eggs, sugar, ...", a step on the card's back)
        marked = MARKER_RE.search(target_text) is not None
        if hint := _between_neighbours(ordered, text, target_text, occurrence):
            return hint
        if not marked and (hint := _ocr_hint(ordered, target, whole=True, occurrence=occurrence)):
            return hint
        if hint := _beside_a_neighbour(ordered, text, target_text, occurrence):
            return hint
        if hint := _position_hint(ordered, text, target, exactly=target_text, occurrence=occurrence):
            return hint
        if not marked:
            return _position_hint(ordered, text, target)
    if hint := _ocr_hint(ordered, target):
        return hint
    if text is not None:
        return _position_hint(ordered, text, target)
    return None
