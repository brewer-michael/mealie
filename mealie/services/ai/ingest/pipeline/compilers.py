"""
The card compilers (docs/ai/PHASE2.md §4.1 step 2), and `capture_errors`, which records a failed compiler instead of
letting the compile step log its traceback (which could hold a provider's response body, F3).

The signatures here are final. Work item B1 provides `CardImageCompiler`, `CardOCRCompiler` and the implementation of
`capture_errors` (moved here from `mealie/scripts/eval_recipe_cards.py`).
"""

from dataclasses import dataclass

from mealie.services.recipe.import_workflow.compilers.base import SourceCompiler


@dataclass
class CapturedError:
    """A compiler that failed"""

    compiler: str
    """The compiler's class name"""
    error: BaseException
    description: str
    """`describe_provider_error(error)`: safe to store and show"""


def capture_errors(compiler: type[SourceCompiler], errors: list[CapturedError]) -> type[SourceCompiler]:
    """
    `compiler`, wrapped so that an exception it raises is appended to `errors` and its `compile()` returns None
    instead: never re-raised, never logged with a traceback. The compile step then moves on to the next compiler.
    """
    raise NotImplementedError("The card compilers are work item B1")
