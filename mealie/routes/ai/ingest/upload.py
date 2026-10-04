"""
`POST /api/ai/ingest` and the batch routes (docs/ai/PHASE2.md §1.2, §1.4, §14).

The upload's only parameters are `request: Request` and the background tasks, so FastAPI reads no body before the
controller's auth has run; `mealie.services.ai.ingest.upload` makes every check in §1.2's order and streams the body
through a byte counter. `done=true` seals the batch once the card is in, as the batch's seal route does.

**Every refusal of the upload carries a top-level `summary`** beside the usual `detail` (its own route class, so auth
failures from the controller's dependencies get one too): an iOS Shortcut shows the same key whether the upload worked
or not. The batch routes keep the usual error body. `POST /batches/{id}/touch` is the capture page's heartbeat.
"""

from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

import anyio.to_thread
from fastapi import APIRouter, BackgroundTasks, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

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
from mealie.services.ai.ingest.i18n import translator_for
from mealie.services.ai.ingest.upload import NOT_FOUND, UploadHandler, UploadRefused, resolve_locale

from ._deps import IngestController, ingest_error, require_enabled, require_not_paused

BATCH_SEALED = "batch_sealed"

_FALLBACK_SUMMARIES = {
    400: "bad-request",
    401: "unauthorized",
    403: "forbidden",
    404: "not-found",
    413: "too-large",
    415: "bad-request",
    422: "bad-request",
    429: "busy",
    503: "unavailable",
}
"""`recipe-ingest.upload-failed.<key>` for a refusal whose detail has no text of its own (the auth dependency's)"""


def error_summary(request: Request, error: HTTPException) -> str:
    """
    What a Shortcut shows for a refused upload: the detail's `summary` (400 `nothing_accepted`) or translated
    `message`, else a short text for the status in the request's language
    """
    detail = error.detail
    if isinstance(detail, dict):
        for key in ("summary", "message"):
            if isinstance(text := detail.get(key), str) and text:
                return text
    translator = translator_for(resolve_locale(request.headers.get("accept-language")))
    key = _FALLBACK_SUMMARIES.get(error.status_code, "other")
    return translator.t(f"recipe-ingest.upload-failed.{key}", status=error.status_code)


class UploadRoute(APIRoute):
    """
    `POST /api/ai/ingest`'s route: an `HTTPException` raised by its dependencies or its handler is answered with
    `{"detail": <as usual>, "summary": ...}` (`error_summary`), the same status and the same headers (`Retry-After`,
    `WWW-Authenticate`)
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except HTTPException as e:
                return JSONResponse(
                    {"detail": e.detail, "summary": error_summary(request, e)},
                    status_code=e.status_code,
                    headers=dict(e.headers) if e.headers else None,
                )

        return route_handler


router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])
upload_router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"], route_class=UploadRoute)

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


@controller(upload_router)
class RecipeIngestUploadController(IngestController):
    @upload_router.post(
        "",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=IngestResponse,
        openapi_extra={
            "requestBody": {
                "content": {
                    "multipart/form-data": {"schema": {"type": "object"}},
                    "image/*": {"schema": {"type": "string", "format": "binary"}},
                    "application/pdf": {"schema": {"type": "string", "format": "binary"}},
                    "application/json": {"schema": {"type": "object"}},
                }
            }
        },
    )
    async def ingest(self, request: Request, background_tasks: BackgroundTasks) -> IngestResponse | Response:
        """
        Uploads one recipe card (front first), or with `split=true` one card per file, as a multipart form (any file
        field, `files` by convention; text fields `batchId`, `position`, `split`, `localOnly`, `allowDuplicate`,
        `done`), a raw image or PDF body (options in the query string) or JSON `{"images": [{"data": "<base64>",
        "filename": ...}]}`; with `AI_INGEST_URL_FETCH` on, a JSON image may be `{"url": "http://..."}`, which the
        server fetches. A PDF or a multi-page TIFF gives the card all its pages (at most 4). `done=true` seals the
        card's batch once the card is in. Needs the `Authorization` header. Answers 202 with the queued jobs, the
        rejected images and a `summary` for a notification; 400 (the same body in `detail`) when nothing was
        accepted. Every refusal has a top-level `summary` too.
        """
        handler = UploadHandler(request, self.session, self.user, self.integration_id)
        try:
            response = await handler.handle()
        except ClientDisconnect:
            # the client went away mid-body (a phone losing signal, a logout aborting the queue): nothing was stored,
            # and nobody is left to read the answer
            logger.debug("A recipe card upload ended: the client disconnected before its body arrived")
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
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

        if handler.options is not None and handler.options.done and response.batch_id is not None:
            # like the batch's seal route: the notification goes out once none of its cards is still being read
            batch_id = response.batch_id
            if await anyio.to_thread.run_sync(batches.seal, self.ingest_repos, batch_id, utcnow()):
                background_tasks.add_task(_notify_if_due, batch_id)
        return response


@controller(router)
class RecipeIngestBatchController(IngestController):
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

    @router.post("/batches/{batch_id}/touch", response_model=RecipeIngestionBatchOut)
    def touch_batch(self, batch_id: UUID) -> RecipeIngestionBatchOut:
        """
        The capture page's heartbeat, sent every few minutes while it's open with this batch: the batch's idle time
        (10 minutes for an app batch) counts from now. Only for an app batch the caller started; 409 `batch_sealed`
        when it's sealed (it stays so: the next card starts a new batch).
        """
        require_not_paused(self.translator)
        require_enabled(self.translator)
        repos = self.ingest_repos
        outcome = batches.heartbeat(repos, batch_id, self.user.id, utcnow())
        if outcome is None:
            raise ingest_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        if outcome == "sealed":
            raise ingest_error(status.HTTP_409_CONFLICT, BATCH_SEALED)
        return _batch_out(repos, batch_id)

    @router.get("/batches/{batch_id}", response_model=RecipeIngestionBatchOut)
    def get_batch(self, batch_id: UUID) -> RecipeIngestionBatchOut:
        """A batch with its counts and its cards in review order"""
        return _batch_out(self.ingest_repos, batch_id)


router.include_router(upload_router)
