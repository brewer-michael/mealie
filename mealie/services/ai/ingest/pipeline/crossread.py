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
from dataclasses import dataclass

from rapidfuzz import fuzz

from mealie.services.openai import OpenAIService

from .attachments import CardImage
from .cardtext import BLANK, LIST_MARKER_RE, SalientToken, canonical_markers, letters_only, salient_tokens
from .compilers import page_label
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

_AMOUNT_FIRST = re.compile(r"^\s*[-•*]?\s*[\d½⅓⅔¼¾⅛⅜⅝⅞\[]")


class CrossReadFailed(Exception):
    """The second reading gave nothing to compare with"""


def transcript_lines(text: str) -> list[str]:
    """The transcript's lines of writing, markers written canonically, empty lines dropped"""
    return [line.strip() for line in canonical_markers(text).splitlines() if line.strip()]


async def read_transcript(pages: Sequence[CardPage], *, ai: OpenAIService) -> list[str]:
    """
    Reads every line written on the card, on `ai`'s image slot, with `card-transcribe.txt`. Raises the provider's
    error, or `CrossReadFailed` when the answer holds nothing to compare with.
    """
    count = len(pages)
    labels = ", ".join(page_label(index, count) for index in range(count))
    response = await ai.get_response(
        ai.get_prompt(CARD_TRANSCRIBE_PROMPT),
        f"Attached {'is' if count == 1 else 'are'} {labels} of one recipe card.",
        response_schema=OpenAIRecipeCardTranscript,
        attachments=[CardImage(path=page.view_path) for page in pages],
    )
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


def align_step(text: str, lines: Sequence[str]) -> tuple[int, int] | None:
    """
    The transcript lines a step reads as, `(start, end)`, or None. Windows of up to `STEP_MAX_LINES` lines whose
    `partial_ratio` reaches `STEP_MIN_SCORE`, and longer ones (grown only while still shorter than the step) whose
    `ratio` does, are candidates, and the one most like the whole step by `ratio` wins: by `partial_ratio` alone, any
    one line of a wrapped step that both reads word for word scores 100, and beats the whole step's window when the
    reads differ by a word elsewhere in it.

    It runs on every save, so the work stays near linear in the transcript, however long the step: a longer window is
    compared whole, by `ratio` alone (`partial_ratio` on strings that long takes far more than linear time), and a
    window whose length alone keeps its `ratio` below what it needs (the best so far, or `STEP_MIN_SCORE`) is skipped.
    """
    target = letters_only(text)
    if not target:
        return None

    words = [letters_only(line) for line in lines]
    best: tuple[float, float, int, int] | None = None
    best_window: tuple[int, int] | None = None
    for start in range(len(lines)):
        length = 0  # of the window's text: its lines' words, joined by spaces
        for end in range(start + 1, len(lines) + 1):
            longer = end - start > STEP_MAX_LINES
            if longer and length >= len(target):
                break
            if words[end - 1]:
                length += len(words[end - 1]) + (1 if length else 0)
            if not length:
                continue
            # candidates rank by `ratio` first, which is at most what the two lengths allow
            needed = max(best[0] if best else 0, STEP_MIN_SCORE if longer else 0)
            if 200 * min(length, len(target)) / (length + len(target)) < needed - 1e-9:
                continue
            window = " ".join(word for word in words[start:end] if word)
            ratio = fuzz.ratio(target, window, score_cutoff=needed)
            if ratio < needed:
                continue
            if longer:
                partial = ratio
            else:
                partial = fuzz.partial_ratio(target, window, score_cutoff=STEP_MIN_SCORE)
                if partial < STEP_MIN_SCORE:
                    continue
            score = (ratio, partial, -(end - start), -start)
            if best is None or score > best:
                best, best_window = score, (start, end)

    return best_window


@dataclass(frozen=True)
class Disagreement:
    window: str
    """The aligned transcript text"""
    missing: list[SalientToken]
    """The draft line's numbers, units and temperatures that the window lacks, in order"""
    window_blank: bool
    """The window has `[blank]`"""


def compare(line: str, window: str) -> Disagreement | None:
    """What the second reading's `window` says differently from the draft's `line`; None when it agrees"""
    window_tokens = set(salient_tokens(window))
    missing: list[SalientToken] = []
    for token in salient_tokens(line):
        if token[0] != "marker" and token not in window_tokens and token not in missing:
            missing.append(token)

    if not missing:
        return None
    return Disagreement(window=window, missing=missing, window_blank=BLANK in window)
