"""
Saving reviewed cards as eval cases, and managing them (docs/ai/PHASE2.md §9, §11.6, §14). Group managers only.

Cases live in `DATA_DIR/groups/<group_id>/eval-cards/`, the group's private eval set that
`python -m mealie.scripts.eval_recipe_cards --cards …` reads. The job is looked up in the manager's own household, like
every other job route; the list, update, download and delete cover the whole group's set. Saving, updating, deleting
and downloading touch files under `groups/`, so they run in the ingest write section and answer 503 while a backup
restore pauses ingestion.
"""

from fastapi import APIRouter, Path, Response, status
from pydantic import UUID4

from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.routes._base import controller
from mealie.schema.recipe_ingest import EvalCaseOut, EvalCaseRequest, EvalCaseSummary, EvalCaseUpdate
from mealie.schema.recipe_ingest.ingest_requests import EVAL_CASE_SLUG_PATTERN
from mealie.services.ai.ingest import eval_export
from mealie.services.ai.ingest.review import BUSY, JobActionError, settle_turns

from ._deps import IngestController, ingest_error, require_enabled, require_not_paused, write_section

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])

NOT_FOUND = "not_found"


@controller(router)
class RecipeIngestEvalCasesController(IngestController):
    @router.post("/jobs/{job_id}/eval-case", response_model=EvalCaseOut, status_code=status.HTTP_201_CREATED)
    def save_eval_case(self, job_id: UUID4, data: EvalCaseRequest) -> EvalCaseOut:
        """
        Saves a `ready` or `committed` card (until its files are purged) as an eval case: `409` when the slug is
        taken (`eval_case_exists`), the card has no draft to export (`not_exportable`), its files are gone
        (`files_missing`) or a running task is turning one of its pages (`busy`).
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
                job = self._settled(job)
                case = eval_export.build_eval_case(job, data.slug, data.verified, tags=data.tags, notes=data.notes)
                return eval_export.save_eval_case(job.group_id, case)
        except eval_export.EvalCaseError as e:
            raise ingest_error(status.HTTP_409_CONFLICT, e.code) from e

    def _settled(self, job: RecipeIngestionJob) -> RecipeIngestionJob:
        """
        The job once a page turn a stop left half done is settled (`settle_turns`), so its pages are read as their
        metadata describes them; 409 `busy` while a running task is turning one
        """
        try:
            job, settled = settle_turns(self.ingest_repos, job)
        except JobActionError as e:
            raise ingest_error(e.status_code, e.code, **e.params) from e
        except FileNotFoundError as e:
            raise eval_export.EvalCaseFilesMissing() from e
        except TimeoutError as e:
            raise ingest_error(status.HTTP_409_CONFLICT, BUSY) from e
        if not settled:
            raise ingest_error(status.HTTP_409_CONFLICT, BUSY)
        return job

    @router.get("/eval-cases", response_model=list[EvalCaseSummary])
    def list_eval_cases(self) -> list[EvalCaseSummary]:
        self.checks.can_manage()
        return eval_export.list_eval_cases(self.group_id)

    @router.put("/eval-cases/{slug}", response_model=EvalCaseSummary)
    def update_eval_case(
        self, data: EvalCaseUpdate, slug: str = Path(pattern=EVAL_CASE_SLUG_PATTERN)
    ) -> EvalCaseSummary:
        """Ticks or unticks "verified", and changes the reviewer's tags and the notes of a saved case"""
        self.checks.can_manage()
        require_not_paused(self.translator)

        try:
            with write_section(self.translator):
                summary = eval_export.update_eval_case(self.group_id, slug, data)
        except eval_export.EvalCaseError as e:
            raise ingest_error(status.HTTP_409_CONFLICT, e.code) from e
        if summary is None:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return summary

    @router.get(
        "/eval-cases/{slug}/download",
        response_class=Response,
        responses={200: {"content": {"application/zip": {}}}},
    )
    def download_eval_case(self, slug: str = Path(pattern=EVAL_CASE_SLUG_PATTERN)) -> Response:
        """A zip of the case's JSON and its photos, to move into `tests/data/cards/` or run the eval elsewhere"""
        self.checks.can_manage()
        require_not_paused(self.translator)

        with write_section(self.translator):
            archive = eval_export.eval_case_archive(self.group_id, slug)
        if archive is None:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return Response(
            content=archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{slug}.zip"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/eval-cases/{slug}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_eval_case(self, slug: str = Path(pattern=EVAL_CASE_SLUG_PATTERN)) -> None:
        self.checks.can_manage()
        require_not_paused(self.translator)

        with write_section(self.translator):
            deleted = eval_export.delete_eval_case(self.group_id, slug)
        if not deleted:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
