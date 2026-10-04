"""
`POST /api/ai/ingest` and the batch routes (docs/ai/PHASE2.md §1.2, §1.4, §14).

The upload's only parameter is `request: Request`, so FastAPI reads no body before the controller's auth has run;
`mealie.services.ai.ingest.upload` makes every check in §1.2's order and streams the body through a byte counter.
"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request, status

from mealie.core.root_logger import get_logger
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.routes._base import controller
from mealie.schema.recipe_ingest import (
    IngestResponse,
    IngestSource,
    RecipeIngestionBatchJob,
    RecipeIngestionBatchOut,
)
from mealie.services.ai.ingest import batches, events
from mealie.services.ai.ingest.upload import NOT_FOUND, UploadHandler, UploadRefused, resolve_locale

from ._deps import IngestController, ingest_error, require_enabled, require_not_paused

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])

logger = get_logger(__name__)


def _batch_out(repos: IngestRepos, batch_id: UUID) -> RecipeIngestionBatchOut:
    batch = repos.batches.get(batch_id)
    if batch is None:
        raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return RecipeIngestionBatchOut(
        id=batch.id,
        source=IngestSource(batch.source),
        created_at=batch.created_at,
        last_upload_at=batch.last_upload_at,
        sealed_at=batch.sealed_at,
        notified_at=batch.notified_at,
        counts=repos.batches.counts(batch_id),
        jobs=[
            RecipeIngestionBatchJob(
                id=job.id,
                position=job.position,
                status=job.status,
                error_count=job.error_count,
                warning_count=job.warning_count,
            )
            for job in repos.batches.jobs(batch_id)
        ],
    )


def _notify_if_due(batch_id: UUID) -> None:
    try:
        events.maybe_notify_batch(batch_id)
    except Exception:
        # housekeeping tries again; a seal never fails because of a notifier
        logger.exception(f"Couldn't send the notification of recipe card batch {batch_id}")


@controller(router)
class RecipeIngestUploadController(IngestController):
    @router.post(
        "",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=IngestResponse,
        openapi_extra={
            "requestBody": {
                "content": {
                    "multipart/form-data": {"schema": {"type": "object"}},
                    "image/*": {"schema": {"type": "string", "format": "binary"}},
                    "application/json": {"schema": {"type": "object"}},
                }
            }
        },
    )
    async def ingest(self, request: Request) -> IngestResponse:
        """
        Uploads one recipe card (front first), or with `split=true` one card per image, as a multipart form (any file
        field, `files` by convention; text fields `batchId`, `position`, `split`, `localOnly`, `allowDuplicate`), a
        raw image body (options in the query string) or JSON `{"images": [{"data": "<base64>", "filename": ...}]}`.
        Needs the `Authorization` header. Answers 202 with the queued jobs, the rejected images and a `summary` for
        a notification; 400 (the same body in `detail`) when nothing was accepted.
        """
        handler = UploadHandler(request, self.session, self.user, self.integration_id)
        try:
            return await handler.handle()
        except UploadRefused as e:
            raise ingest_error(
                e.status_code,
                e.code,
                message_key=e.message_key,
                message_params=e.message_params,
                translator=handler.translator,
                headers=e.headers or None,
                **e.params,
            ) from e

    @router.post("/batches", status_code=status.HTTP_201_CREATED, response_model=RecipeIngestionBatchOut)
    def create_batch(self, request: Request) -> RecipeIngestionBatchOut:
        """Starts an app batch: the capture session's cards send its id, and Done seals it"""
        require_not_paused(self.translator)
        require_enabled(self.translator)
        batch_id = self.ingest_repos.batches.create(
            source=IngestSource.app,
            created_by=self.user.id,
            locale=resolve_locale(request.headers.get("accept-language")),
        )
        return _batch_out(self.ingest_repos, batch_id)

    @router.post("/batches/{batch_id}/seal", response_model=RecipeIngestionBatchOut)
    def seal_batch(self, batch_id: UUID, background_tasks: BackgroundTasks) -> RecipeIngestionBatchOut:
        """
        Marks a batch done (sent once every card of it has uploaded or failed for good). A sealed batch never gains a
        card; its notification goes out once none of its cards is still being read. Sealing again is harmless.
        """
        require_not_paused(self.translator)
        require_enabled(self.translator)
        repos = self.ingest_repos
        if repos.batches.get(batch_id) is None:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        if batches.seal(repos, batch_id, utcnow()):
            background_tasks.add_task(_notify_if_due, batch_id)
        return _batch_out(repos, batch_id)

    @router.get("/batches/{batch_id}", response_model=RecipeIngestionBatchOut)
    def get_batch(self, batch_id: UUID) -> RecipeIngestionBatchOut:
        """A batch with its counts and its cards in review order"""
        return _batch_out(self.ingest_repos, batch_id)
