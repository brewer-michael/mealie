"""
The rules shared by the flags, the review page and the eval (docs/ai/PHASE2.md §4.6): which flags are highlighted,
which can be kept as written, and when a card is clean.
"""

from collections.abc import Iterable

from mealie.schema.recipe_ingest import CardFlag, CardFlagKind, CardFlagSeverity

HIGHLIGHTED_SEVERITIES = frozenset({CardFlagSeverity.error, CardFlagSeverity.warning})
"""What the review page highlights and the eval's flag calibration counts as flagged"""

KEEPABLE_KINDS = frozenset({CardFlagKind.illegible, CardFlagKind.blank})
"""Errors the reviewer can resolve with "Keep as written"; every other error has to be fixed"""

REVIEW_CONFIDENCE = 0.85
"""
Below this average NLP confidence a line is flagged `check_parse`. Mirrors `confidenceThreshold` in
frontend/app/composables/recipes/use-parse-ingredients-dialog.ts.
"""


def count_unresolved(flags: Iterable[CardFlag]) -> tuple[int, int]:
    """Unresolved errors and unresolved warnings. A flag with any resolution (kept or dismissed) counts as resolved."""
    errors = warnings = 0
    for flag in flags:
        if flag.resolution is not None:
            continue
        if flag.severity == CardFlagSeverity.error:
            errors += 1
        elif flag.severity == CardFlagSeverity.warning:
            warnings += 1
    return errors, warnings


def is_clean(flags: Iterable[CardFlag]) -> bool:
    """Whether a card has no unresolved error or warning: one tap to commit"""
    return count_unresolved(flags) == (0, 0)
