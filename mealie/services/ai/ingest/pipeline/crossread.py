"""
The opt-in second reading of a card (docs/ai/PHASE2.md §4.5).

The most damaging error is a number the vision read invents in a blank: the transcription itself contains it, so no
check against that transcription can see it. An independent transcript of every line on the card is the only signal
that can. `read_transcript` asks for it on the image slot of the same `ai` as the main read (so it's filtered by the
same policy and, in the eval, pinned to the same provider), concurrently with the main read.

The rest is pure: the transcript is split into lines, each draft ingredient is aligned with its best line and each
step with a window of consecutive lines (steps wrap), and `compare` says which of the draft line's salient tokens the
aligned text lacks. `flags.compute_flags` turns that into `read_disagreement` and `blank` flags.

Alignment first filters by a score that forgives the two reads wrapping or wording a line differently
(`token_set_ratio`, `partial_ratio`), then picks the candidate most like the whole line (`ratio`), and an ingredient
prefers a line shaped as it is: starting with an amount as it does, or saying nothing it doesn't (its own line, read
without the amount). Ranking by the forgiving score alone picks any text the line contains, or that contains the
line: a step line that mentions "eggs" for the ingredient "2 eggs", or one short line of a wrapped step, which lacks
the step's numbers. Lines that read the same but for their amounts ("1 c. sugar" for the cake, "1/2 c. sugar" for
the frosting) are told apart by order: the ingredients are aligned in the draft's order, each preferring a line after
the previous one's.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from mealie.services.openai import OpenAIService

from .attachments import CardImage
from .cardtext import (
    BLANK,
    LIST_MARKER_RE,
    SalientToken,
    canonical_markers,
    letters_only,
    salient_token_spans,
    salient_tokens,
)
from .compilers import may_take_fewer_images, page_label, reads_one_image_at_a_time
from .llm_schemas import OpenAIRecipeCardTranscript
from .models import CardPage

CARD_TRANSCRIBE_PROMPT = "recipes.card-transcribe"

INGREDIENT_MIN_SCORE = 60
"""
An ingredient can align with a transcript line whose `token_set_ratio` on letters only is at least this; the best
of those by the mean of `token_set_ratio` and `ratio` wins
"""
STEP_MIN_SCORE = 70
"""
A step can align with a window of transcript lines whose `partial_ratio` on letters only is at least this (`ratio`,
for a window of more than `STEP_MAX_LINES` lines); the best of those by `ratio` wins
"""
STEP_MAX_LINES = 4
"""A step's window has up to this many lines, or more while it's still shorter than the step"""
SHORT_LINE = 20
"""
Transcript lines with fewer letters than this are short (a word or two: a narrow column, a page read one word per
line); a run of more than `STEP_MAX_LINES` of them is windowed in pieces of `CHUNK_LENGTH` letters or more
"""
CHUNK_LENGTH = 40
MAX_WINDOW_GROWTH = 1.5
"""A window longer than `STEP_MAX_LINES` lines grows no further than this many times the step's length"""

_AMOUNT_FIRST = re.compile(r"^\s*[-•*]?\s*[\d½⅓⅔¼¾⅛⅜⅝⅞\[]")


class CrossReadFailed(Exception):
    """The second reading gave nothing to compare with"""


def transcript_lines(text: str) -> list[str]:
    """The transcript's lines of writing, markers written canonically, empty lines dropped"""
    return [line.strip() for line in canonical_markers(text).splitlines() if line.strip()]


async def read_transcript(pages: Sequence[CardPage], *, ai: OpenAIService) -> list[str]:
    """
    Reads every line written on the card, on `ai`'s image slot, with `card-transcribe.txt`: all pages in one request,
    or page by page for a provider that takes one image per request (as the main read does, `compilers`). Raises the
    provider's error, or `CrossReadFailed` when the answer holds nothing to compare with.
    """
    count = len(pages)
    if count > 1 and reads_one_image_at_a_time(ai):
        return await _read_page_by_page(pages, ai=ai)

    labels = ", ".join(page_label(index, count) for index in range(count))
    try:
        response = await ai.get_response(
            ai.get_prompt(CARD_TRANSCRIBE_PROMPT),
            f"Attached {'is' if count == 1 else 'are'} {labels} of one recipe card.",
            response_schema=OpenAIRecipeCardTranscript,
            attachments=[CardImage(path=page.view_path) for page in pages],
        )
    except Exception as e:
        if count == 1 or not may_take_fewer_images(e):
            raise
        return await _read_page_by_page(pages, ai=ai)
    return _lines(response)


async def _read_page_by_page(pages: Sequence[CardPage], *, ai: OpenAIService) -> list[str]:
    """Each page's lines, read in a request of its own, in page order"""
    count = len(pages)
    responses: list[OpenAIRecipeCardTranscript | None] = []
    for index, page in enumerate(pages):
        responses.append(
            await ai.get_response(
                ai.get_prompt(CARD_TRANSCRIBE_PROMPT),
                f"Attached is {page_label(index, count)} of one recipe card that has {count} images; the other "
                "images are read on their own. Transcribe this image only.",
                response_schema=OpenAIRecipeCardTranscript,
                attachments=[CardImage(path=page.view_path)],
            )
        )
    found = [response for response in responses if response is not None and response.contains_recipe]
    joined = OpenAIRecipeCardTranscript(
        contains_recipe=bool(found), text="\n".join(response.text for response in found)
    )
    return _lines(joined)


def _lines(response: OpenAIRecipeCardTranscript | None) -> list[str]:
    if response is None or not response.contains_recipe:
        raise CrossReadFailed("The second reading found no recipe on the card")

    lines = transcript_lines(response.text)
    if not lines:
        raise CrossReadFailed("The second reading was empty")
    return lines


def _amount_first(text: str) -> bool:
    """
    Whether a line starts with an amount, or a gap or unreadable spot where one would be, as ingredient lines do and
    steps don't (a numbered step's "2." isn't an amount)
    """
    return bool(_AMOUNT_FIRST.match(LIST_MARKER_RE.sub("", text, count=1)))


def align_ingredient(line: str, lines: Sequence[str], after: int = -1) -> int | None:
    """
    The index of the transcript line an ingredient line reads as, or None if none is close enough. `after` is the line
    the ingredient before it aligned with: of lines that read alike, the first one after it wins.
    """
    target = letters_only(line)
    if not target:
        return None

    shape = _amount_first(line)
    target_words = set(target.split())
    best: tuple[bool, float, float, bool, bool, int] | None = None
    for index, candidate in enumerate(lines):
        words = letters_only(candidate)
        if not words:
            continue
        token_set = fuzz.token_set_ratio(target, words)
        if token_set < INGREDIENT_MIN_SCORE:
            continue
        # `token_set_ratio` is 100 for any line holding all the ingredient's words, so a step's "Add eggs" would beat
        # the ingredient's own line read as "2 egg": a line shaped like the ingredient comes first, then `ratio`
        # prefers the line that says that and little more. A line saying nothing the ingredient doesn't is its own
        # line read without the amount ("c. sugar" for "1 c. sugar"), never "1 c. brown sugar"
        same_shape = _amount_first(candidate) == shape
        shaped = same_shape or set(words.split()) <= target_words
        # lines that read alike but for their amounts ("1 c. sugar", "1/2 c. sugar"): the next one in order
        score = (shaped, (token_set + fuzz.ratio(target, words)) / 2, token_set, same_shape, index > after, -index)
        if best is None or score > best:
            best = score

    return None if best is None else -best[5]


def _chunks(words: Sequence[str]) -> list[tuple[int, int]]:
    """
    The transcript's lines as the units a step's longer windows (more than `STEP_MAX_LINES` lines) grow by,
    `(start, end)` in line indices: each line on its own, but a run of more than `STEP_MAX_LINES` short lines
    (`SHORT_LINE`) in pieces of `CHUNK_LENGTH` letters or more. A page read one word per line would otherwise give a
    step of 600 letters a hundred lines to grow its windows over, from every one of its lines.
    """
    chunks: list[tuple[int, int]] = []
    line = 0
    while line < len(words):
        run_end = line
        while run_end < len(words) and len(words[run_end]) < SHORT_LINE:
            run_end += 1
        if run_end - line <= STEP_MAX_LINES:
            chunks.extend((index, index + 1) for index in range(line, max(run_end, line + 1)))
            line = max(run_end, line + 1)
            continue
        while line < run_end:
            end, length = line + 1, len(words[line])
            while end < run_end and length < CHUNK_LENGTH:
                length += len(words[end]) + (1 if length and words[end] else 0)
                end += 1
            chunks.append((line, end))
            line = end
    return chunks


def align_step(text: str, lines: Sequence[str]) -> tuple[int, int] | None:
    """
    The transcript lines a step reads as, `(start, end)`, or None. Windows of up to `STEP_MAX_LINES` lines whose
    `partial_ratio` reaches `STEP_MIN_SCORE`, and longer ones (grown only while still shorter than the step, and never
    past `MAX_WINDOW_GROWTH` times its length) whose `ratio` does, are candidates, and the one most like the whole step
    by `ratio` wins: by `partial_ratio` alone, any one line of a wrapped step that both reads word for word scores 100,
    and beats the whole step's window when the reads differ by a word elsewhere in it.

    Every line starts windows of up to `STEP_MAX_LINES` lines, so a short step ("Add eggs.", "Preheat oven to 350°.")
    finds its own line among the short lines around it (ingredient lines, other terse steps). It runs on every save,
    so the work stays near linear in the transcript, however long the step and however short its lines: the longer
    windows grow by whole lines, or by runs of short lines (`_chunks`), and are compared first, whole, by `ratio` alone
    (`partial_ratio` on strings that long takes far more than linear time); a window whose length alone keeps its
    `ratio` below what it needs (the best so far, or `STEP_MIN_SCORE`) is skipped, so a long step's best window rules
    out nearly every short one unscored.
    """
    target = letters_only(text)
    if not target:
        return None

    words = [letters_only(line) for line in lines]
    best: tuple[float, float, int, int] | None = None
    best_window: tuple[int, int] | None = None

    def consider(start: int, end: int, length: int, longer: bool) -> None:
        nonlocal best, best_window
        # candidates rank by `ratio` first, which is at most what the two lengths allow
        needed = max(best[0] if best else 0, STEP_MIN_SCORE if longer else 0)
        if 200 * min(length, len(target)) / (length + len(target)) < needed - 1e-9:
            return
        window = " ".join(word for word in words[start:end] if word)
        ratio = fuzz.ratio(target, window, score_cutoff=needed)
        if ratio < needed:
            return
        if longer:
            partial = ratio
        else:
            partial = fuzz.partial_ratio(target, window, score_cutoff=STEP_MIN_SCORE)
            if partial < STEP_MIN_SCORE:
                return
        score = (ratio, partial, -(end - start), -start)
        if best is None or score > best:
            best, best_window = score, (start, end)

    # longer windows, from each chunk, grown chunk by chunk
    chunks = _chunks(words)
    for first, (start, _) in enumerate(chunks):
        length = 0  # of the window's text: its lines' words, joined by spaces
        for chunk_start, end in chunks[first:]:
            longer = end - start > STEP_MAX_LINES
            if longer and length >= len(target):
                break
            for word in words[chunk_start:end]:
                if word:
                    length += len(word) + (1 if length else 0)
            if longer and length > MAX_WINDOW_GROWTH * len(target):
                break
            if longer and length:
                consider(start, end, length, longer=True)

    # windows of up to `STEP_MAX_LINES` lines, from every line
    for start in range(len(words)):
        length = 0
        for end in range(start + 1, min(start + STEP_MAX_LINES, len(words)) + 1):
            if words[end - 1]:
                length += len(words[end - 1]) + (1 if length else 0)
            if length:
                consider(start, end, length, longer=False)

    return best_window


@dataclass(frozen=True)
class Disagreement:
    window: str
    """The aligned transcript text"""
    missing: list[SalientToken]
    """The draft line's numbers, units and temperatures that the window lacks, in order"""
    window_blank: bool
    """The window has `[blank]`"""
    spans: list[tuple[int, int]] = field(default_factory=list)
    """Where each of `missing` is written in the draft's line"""


def compare(line: str, window: str) -> Disagreement | None:
    """What the second reading's `window` says differently from the draft's `line`; None when it agrees"""
    window_tokens = set(salient_tokens(window))
    missing: list[SalientToken] = []
    spans: list[tuple[int, int]] = []
    for token, span in salient_token_spans(line):
        if token[0] != "marker" and token not in window_tokens and token not in missing:
            missing.append(token)
            spans.append(span)

    if not missing:
        return None
    return Disagreement(window=window, missing=missing, window_blank=BLANK in window, spans=spans)
