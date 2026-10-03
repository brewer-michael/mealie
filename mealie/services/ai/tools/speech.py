"""
Helpers for writing a tool result's `speech`: short, plain sentences a voice assistant can read aloud.

Anything put into a sentence from the database or from the caller (a recipe name, a list item, a plan title) goes
through `spoken`, which keeps it short and stops it from adding sentences of its own.
"""

import re
import unicodedata
from collections.abc import Sequence
from datetime import date

_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_LINE_MARKUP = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
_MD_INLINE_MARKUP = re.compile(r"\*+|`+|~~|(?<!\w)_+|_+(?!\w)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_SENTENCE_PUNCTUATION = re.compile(r"[.!?]+(?=\s|$)")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+(?=[.,!?;:](?:\s|$))")
_VULGAR_FRACTION = re.compile("(?<=\\d)(?=[\u00bc-\u00be\u2150-\u215e])")


def plain_numbers(text: str) -> str:
    """`text` with fraction and superscript glyphs (`¹/₂`, `1½`) written in plain digits (`1/2`, `1 1/2`)"""
    # `1½` is one and a half, not 11/2
    text = _VULGAR_FRACTION.sub(" ", text)
    return unicodedata.normalize("NFKC", text).replace("\u2044", "/")  # the fraction slash


def plain_text(text: str | None) -> str:
    """`text` without markdown, HTML tags or URLs, on one line, with its numbers in plain digits"""
    if not text:
        return ""

    text = plain_numbers(text)
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub(" ", text)
    text = _URL.sub("", text)
    text = _MD_LINE_MARKUP.sub("", text)
    text = _MD_INLINE_MARKUP.sub("", text)
    # a removed tag leaves a space, which mustn't be left before punctuation: `the <b>oven</b>.`
    return _SPACE_BEFORE_PUNCTUATION.sub("", " ".join(text.split()))


def _cut(text: str, max_chars: int) -> str:
    """`text` cut at the last whole word that fits in `max_chars`"""
    if len(text) <= max_chars:
        return text

    head = text[: max_chars + 1]
    cut = head.rsplit(" ", 1)[0] if " " in head else head[:max_chars]
    return cut.rstrip(" ,;:-")


def spoken(text: str | None, max_chars: int = 60) -> str:
    """
    A name or item from the database or the caller, to put in a sentence: plain, at most `max_chars` (cut at a
    word), and without sentence-ending punctuation, so it can't run on or add sentences of its own
    """
    return _cut(_SENTENCE_PUNCTUATION.sub("", plain_text(text)), max_chars)


def first_sentences(text: str, max_chars: int = 280, max_sentences: int | None = None) -> str:
    """
    The leading sentences of `text` (at most `max_sentences` of them) that fit in `max_chars`. A first
    sentence that's longer than that on its own is cut at the last whole word that fits.
    """
    text = plain_text(text)
    sentences = _SENTENCE_END.split(text)
    if max_sentences is not None:
        sentences = sentences[:max_sentences]

    kept = ""
    for sentence in sentences:
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > max_chars:
            break
        kept = candidate

    return kept or _cut(sentences[0], max_chars)


def end_sentence(text: str) -> str:
    """`text` ending with a full stop, unless it already ends a sentence"""
    text = text.rstrip(" ,;:-")
    return text if not text or text[-1] in ".!?" else f"{text}."


def join_words(words: Sequence[str], conjunction: str = "and") -> str:
    """`a`, `a and b`, `a, b and c`"""
    words = [w for w in words if w]
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} {conjunction} {words[-1]}"


def count_of(count: int, singular: str, plural: str | None = None) -> str:
    """`1 recipe`, `3 recipes`"""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def number(value: float) -> str:
    """A quantity as it's said: `4`, `2.5`, `0.33`"""
    return f"{round(value, 2):g}"


def spoken_minutes(minutes: int) -> str:
    """`45 minutes`, `1 hour 30 minutes`, `2 days`"""
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = [(days, "day"), (hours, "hour"), (minutes, "minute")]
    return " ".join(count_of(amount, unit) for amount, unit in parts if amount) or "0 minutes"


def spoken_date(day: date, today: date) -> str:
    """`today`, `tomorrow`, `yesterday`, or `Friday, October 9` (with the year when it isn't this year)"""
    offset = (day - today).days
    if offset == 0:
        return "today"
    if offset == 1:
        return "tomorrow"
    if offset == -1:
        return "yesterday"

    weekday = f"{day:%A}, {day:%B} {day.day}"
    return weekday if day.year == today.year else f"{weekday}, {day.year}"


def spoken_date_phrase(day: date, today: date) -> str:
    """`spoken_date` after a preposition: `today`, `tomorrow`, or `on Friday, October 9`"""
    said = spoken_date(day, today)
    return said if said in ("today", "tomorrow", "yesterday") else f"on {said}"
