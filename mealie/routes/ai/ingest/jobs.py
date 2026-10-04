"""
The job routes under `/api/ai/ingest/jobs` (docs/ai/PHASE2.md §14): review, re-read, rotate, page images, commit and
discard. `/jobs/counts` is declared before `/jobs/{id}`.

Every route is household-scoped (another household's job, images included, is a 404) and answers 503 while
`AI_INGEST_ENABLED` is off. The routes that write files (rotate, commit, discard) also answer 503
`paused_for_restore` while a backup restore pauses ingestion, both up front and when their write section can't start.
The work is in `mealie.services.ai.ingest.review` and `.commit`; refusals come back as `{"detail": {"code", ...}}`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cached_property
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import UUID4

from mealie.routes._base import controller
from mealie.schema.recipe_ingest import (
    CardDraftSaved,
    CardDraftUpdate,
    CommitOut,
    CommitRequest,
    IngestStatus,
    PageOut,
    RecipeIngestionJobCounts,
    RecipeIngestionJobOut,
    RecipeIngestionJobPagination,
    RecipeIngestionJobState,
    RereadRequest,
    RotateRequest,
)
from mealie.services.ai.errors import IngestPaused
from mealie.services.ai.ingest.commit import commit_job
from mealie.services.ai.ingest.review import JobActionError, PageImage, ReviewService

from ._deps import IngestController, ingest_error, paused_error, require_enabled, require_not_paused, write_section

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])

PageImageKind = Literal["page", "view", "thumb"]

MAX_PER_PAGE = 500


def _matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    candidates = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
    return etag in candidates


def _image_headers(image: PageImage, requested_version: str | None) -> dict[str, str]:
    # a URL carrying the page's current version never changes; any other is checked against the ETag each time
    cache = "private, max-age=31536000, immutable" if requested_version == image.version else "private, no-cache"
    return {"Cache-Control": cache, "ETag": image.etag, "X-Content-Type-Options": "nosniff"}


@controller(router)
class RecipeIngestJobsController(IngestController):
    @cached_property
    def review(self) -> ReviewService:
        return ReviewService(self.ingest_repos, self.user)

    @contextmanager
    def _answer(self, *, writes_files: bool = False) -> Iterator[None]:
        """The checks every route makes first, and refusals turned into `{"detail": {"code": ...}}` bodies"""
        require_enabled(self.translator)
        if writes_files:
            require_not_paused(self.translator)
        try:
            yield
        except JobActionError as e:
            raise ingest_error(e.status_code, e.code, translator=self.translator, **e.params) from e
        except IngestPaused as e:
            raise paused_error(self.translator) from e

    # ==================================================================================================================
    # Lists

    @router.get("/jobs", response_model=RecipeIngestionJobPagination)
    def get_jobs(
        self,
        status_filter: list[IngestStatus] | None = Query(None, alias="status"),
        batch_id: UUID4 | None = Query(None, alias="batchId"),
        page: int = Query(1, ge=1),
        per_page: int = Query(50, alias="perPage", ge=-1, le=MAX_PER_PAGE),
    ) -> RecipeIngestionJobPagination:
        """The household's recipe cards, newest first; `perPage=-1` returns them all"""
        with self._answer():
            result = self.review.list_jobs(
                statuses=status_filter, batch_id=batch_id, page=page, per_page=per_page if per_page != 0 else 50
            )
            query = {"status": status_filter, "batchId": batch_id, "page": result.page, "perPage": per_page}
            result.set_pagination_guides(router.url_path_for("get_jobs"), {k: v for k, v in query.items() if v})
            return result

    @router.get("/jobs/counts", response_model=RecipeIngestionJobCounts)
    def get_counts(self) -> RecipeIngestionJobCounts:
        """Processing, ready, ready with something to check, and failed cards (the sidebar and HA's sensor)"""
        with self._answer():
            return self.review.counts()

    # ==================================================================================================================
    # One job

    @router.get("/jobs/{job_id}", response_model=RecipeIngestionJobOut)
    def get_job(self, job_id: UUID4) -> RecipeIngestionJobOut:
        with self._answer():
            return self.review.get_job(job_id)

    @router.get("/jobs/{job_id}/state", response_model=RecipeIngestionJobState)
    def get_job_state(self, job_id: UUID4) -> RecipeIngestionJobState:
        """What the review page polls while a task runs"""
        with self._answer():
            return self.review.get_state(job_id)

    @router.put("/jobs/{job_id}", response_model=CardDraftSaved)
    def update_job(self, job_id: UUID4, data: CardDraftUpdate) -> CardDraftSaved:
        """
        Saves the draft (§6.6). A stale `draftVersion` is `409 {detail: {code: "version_conflict", current}}`, with
        no `message`: the page shows its own Reload dialog.
        """
        with self._answer():
            return self.review.save_draft(job_id, data)

    @router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    def discard_job(self, job_id: UUID4) -> Response:
        """Deletes the card and its photos: the uploader, anyone for inbox cards, otherwise household managers"""
        with self._answer(writes_files=True), write_section(self.translator):
            self.review.discard(job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ==================================================================================================================
    # Tasks

    @router.post(
        "/jobs/{job_id}/reextract", status_code=status.HTTP_202_ACCEPTED, response_model=RecipeIngestionJobState
    )
    def reextract_job(self, job_id: UUID4) -> RecipeIngestionJobState:
        """Reads the whole card again; `409 {detail: {code: "busy"}}` while a task is active"""
        with self._answer():
            return self.review.reextract(job_id)

    @router.post("/jobs/{job_id}/reread", status_code=status.HTTP_202_ACCEPTED, response_model=RecipeIngestionJobState)
    def reread_region(self, job_id: UUID4, data: RereadRequest) -> RecipeIngestionJobState:
        """
        Reads one region of an upright page again (fractions of its width and height) for one field; the result
        arrives as a proposal. `409 {detail: {code: "busy"}}` while a task is active.
        """
        with self._answer():
            return self.review.reread(job_id, data)

    @router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED, response_model=RecipeIngestionJobState)
    def retry_job(self, job_id: UUID4) -> RecipeIngestionJobState:
        """A failed card is read again from the start"""
        with self._answer():
            return self.review.retry(job_id)

    @router.post("/jobs/{job_id}/cancel", response_model=RecipeIngestionJobState)
    def cancel_job(self, job_id: UUID4) -> RecipeIngestionJobState:
        with self._answer():
            return self.review.cancel(job_id)

    # ==================================================================================================================
    # Pages

    @router.post("/jobs/{job_id}/pages/{index}/rotate", response_model=PageOut)
    def rotate_page(self, job_id: UUID4, index: int, data: RotateRequest) -> PageOut:
        """Turns a page clockwise; `409 {detail: {code: "busy"}}` while a task is active"""
        with self._answer(writes_files=True), write_section(self.translator):
            return self.review.rotate(job_id, index, data.degrees)

    @router.get(
        "/jobs/{job_id}/pages/{index}/{kind}",
        response_class=FileResponse,
        responses={200: {"content": {"image/jpeg": {}, "image/webp": {}}}},
    )
    def get_page_image(self, request: Request, job_id: UUID4, index: int, kind: PageImageKind) -> Response:
        """
        A page's `page.jpg` (up to 4096 px), `view.jpg` (2048 px) or `thumb.webp`, for the job's household only, with
        an ETag that changes when the page is turned
        """
        with self._answer():
            image = self.review.page_image(job_id, index, kind)

        headers = _image_headers(image, request.query_params.get("v"))
        if _matches(request.headers.get("if-none-match"), image.etag):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return FileResponse(image.path, media_type=image.media_type, headers=headers, stat_result=image.stat)

    # ==================================================================================================================
    # Commit

    @router.post("/jobs/{job_id}/commit", status_code=status.HTTP_201_CREATED, response_model=CommitOut)
    def commit(
        self, job_id: UUID4, data: CommitRequest, response: Response, background_tasks: BackgroundTasks
    ) -> CommitOut:
        """
        Adds the card as a recipe (§7): `201` with the recipe and the batch's next card to review, `200` with the same
        recipe if it was already committed, `422 {detail: {code: "unresolved_flags", flags}}` while errors remain,
        `409 {detail: {code}}` otherwise
        """
        with self._answer(writes_files=True):
            result = commit_job(
                self.ingest_repos,
                self.user,
                job_id,
                data,
                translator=self.translator,
                integration_id=self.integration_id,
                background=background_tasks,
            )
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result.out
