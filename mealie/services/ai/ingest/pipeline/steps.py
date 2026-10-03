"""
The card workflow's steps (docs/ai/PHASE2.md §4.1): upstream's import workflow with card compilers and prompts, and
no translation or `finalize_scraped_recipe`.

The signature is final. Work item B1 provides the implementation.
"""

from mealie.services.recipe.import_workflow.base import WorkflowStep

from . import CardPipelineOptions
from .compilers import CapturedError


def card_workflow_steps(options: CardPipelineOptions, errors: list[CapturedError]) -> list[WorkflowStep]:
    """
    `CompileSourceStep` with the card compilers for `options.read_path`, each wrapped by `capture_errors(…, errors)`;
    then the card build step; then `ResolveOrganizersStep` when `options.suggest_organizers`.
    """
    raise NotImplementedError("The card workflow is work item B1")
