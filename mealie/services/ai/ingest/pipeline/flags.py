"""
The flags that say what to check on a card (docs/ai/PHASE2.md §4.6). `compute_flags` is pure, and the server runs it
after extraction and on every save, so the flags always describe the current draft.

The signature is final. Work item B1 provides the rules; until then no flags are raised.
"""

from collections.abc import Mapping

from mealie.schema.recipe_ingest import CardDraft, CardFlag, ExtractionMeta, FlagResolution


def compute_flags(
    draft: CardDraft, extraction: ExtractionMeta | None, resolutions: Mapping[str, FlagResolution]
) -> list[CardFlag]:
    """
    Every flag the draft raises, in reading order, keyed `"<kind>:<field>:<ref>"`, with the stored resolutions
    (by flag id) applied.
    """
    return []
