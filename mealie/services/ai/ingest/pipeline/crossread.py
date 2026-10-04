"""
The opt-in second reading of a card (docs/ai/PHASE2.md §4.5).

The most damaging error is a number the vision read invents in a blank: the transcription itself contains it, so no
check against that transcription can see it. An independent transcript of every line on the card is the only signal
that can. `read_transcript` asks for it on the image slot of the same `ai` as the main read (so it's filtered by the
same policy and, in the eval, pinned to the same provider), concurrently with the main read.

The rest is pure: the transcript is split into lines, each draft ingredient is aligned with its best line and each
step with a window of up to 4 consecutive lines (steps wrap), and `compare` says which of the draft line's salient
tokens the aligned text lacks. `flags.compute_flags` turns that into `read_disagreement` and `blank` flags.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from mealie.services.openai import OpenAIService

from .attachments import CardImage
from .cardtext import BLANK, SalientToken, canonical_markers, letters_only, salient_tokens
from .compilers import page_label
from .llm_schemas import OpenAIRecipeCardTranscript
from .models import CardPage

CARD_TRANSCRIBE_PROMPT = "recipes.card-transcribe"

INGREDIENT_MIN_SCORE = 60
"""An ingredient aligns with its best transcript line by `token_set_ratio` on letters only, if it scores this much"""
STEP_MIN_SCORE = 70
"""A step aligns with its best window of transcript lines by `partial_ratio`, if it scores this much"""
STEP_MAX_LINES = 4


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


def align_ingredient(line: str, lines: Sequence[str]) -> int | None:
    """The index of the transcript line an ingredient line reads as, or None if none is close enough"""
    target = letters_only(line)
    if not target:
        return None

    best: tuple[float, float, int] | None = None
    for index, candidate in enumerate(lines):
        words = letters_only(candidate)
        if not words:
            continue
        score = (fuzz.token_set_ratio(target, words), fuzz.ratio(target, words), -index)
        if best is None or score > best:
            best = score

    if best is None or best[0] < INGREDIENT_MIN_SCORE:
        return None
    return -best[2]


def align_step(text: str, lines: Sequence[str]) -> tuple[int, int] | None:
    """
    The transcript lines a step reads as, `(start, end)`, or None. Among windows of up to `STEP_MAX_LINES` lines,
    the best `partial_ratio` wins, and between equals the one closest to the whole step (a single line that is only
    part of a wrapped step scores as well by `partial_ratio`).
    """
    target = letters_only(text)
    if not target:
        return None

    words = [letters_only(line) for line in lines]
    best: tuple[float, float, int, int] | None = None
    best_window: tuple[int, int] | None = None
    for start in range(len(lines)):
        for end in range(start + 1, min(start + STEP_MAX_LINES, len(lines)) + 1):
            window = " ".join(word for word in words[start:end] if word)
            if not window:
                continue
            score = (fuzz.partial_ratio(target, window), fuzz.ratio(target, window), -(end - start), -start)
            if best is None or score > best:
                best, best_window = score, (start, end)

    if best is None or best[0] < STEP_MIN_SCORE:
        return None
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
