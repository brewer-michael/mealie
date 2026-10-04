"""
Saving reviewed cards as eval cases, and managing them (docs/ai/PHASE2.md §9, §11.6, §14). Group managers only.

Cases live in `DATA_DIR/groups/<group_id>/eval-cards/`, the group's private eval set that
`python -m mealie.scripts.eval_recipe_cards --cards …` reads. The job is looked up in the manager's own household, like
every other job route; the list and delete cover the whole group's set. Saving and deleting write under `groups/`, so
they run in the ingest write section and answer 503 while a backup restore pauses ingestion.
"""

from fastapi import APIRouter, Path, status
from pydantic import UUID4

from mealie.routes._base import controller
from mealie.schema.recipe_ingest import EvalCaseOut, EvalCaseRequest, EvalCaseSummary
from mealie.schema.recipe_ingest.ingest_requests import EVAL_CASE_SLUG_PATTERN
from mealie.services.ai.ingest import eval_export

from ._deps import IngestController, ingest_error, require_enabled, require_not_paused, write_section

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])

NOT_FOUND = "not_found"


@controller(router)
class RecipeIngestEvalCasesController(IngestController):
    @router.post("/jobs/{job_id}/eval-case", response_model=EvalCaseOut, status_code=status.HTTP_201_CREATED)
    def save_eval_case(self, job_id: UUID4, data: EvalCaseRequest) -> EvalCaseOut:
        """
        Saves a `ready` or `committed` card (until its files are purged) as an eval case: `409` when the slug is
        taken (`eval_case_exists`), the card has no draft to export (`not_exportable`) or its files are gone
        (`files_missing`).
        """
        self.checks.can_manage()
        require_enabled(self.translator)
        require_not_paused(self.translator)

        job = self.ingest_repos.jobs.get(job_id)
        if job is None:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)

        try:
            # the job's files are read in the write section too, so a restore can't replace them mid-read
            with write_section(self.translator):
                case = eval_export.build_eval_case(job, data.slug, data.verified)
                return eval_export.save_eval_case(job.group_id, case)
        except eval_export.EvalCaseError as e:
            raise ingest_error(status.HTTP_409_CONFLICT, e.code) from e

    @router.get("/eval-cases", response_model=list[EvalCaseSummary])
    def list_eval_cases(self) -> list[EvalCaseSummary]:
        self.checks.can_manage()
        return eval_export.list_eval_cases(self.group_id)

    @router.delete("/eval-cases/{slug}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_eval_case(self, slug: str = Path(pattern=EVAL_CASE_SLUG_PATTERN)) -> None:
        self.checks.can_manage()
        require_not_paused(self.translator)

        with write_section(self.translator):
            deleted = eval_export.delete_eval_case(self.group_id, slug)
        if not deleted:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
