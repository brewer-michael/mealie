"""
Helpers for the recipe card runner's tests (docs/ai/PHASE2.md §3): jobs in a test household, task results, fake
handlers and the phase recorders. The fixtures that use them are in this folder's `conftest.py`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardFlag,
    CardProposal,
    CardProposalKind,
    ExtractionMeta,
    IngestReadPath,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
    ProposalTarget,
)
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.types import ExtractResult, RereadResult, TaskContext
from tests.utils.fixture_schemas import TestUser

Job = RecipeIngestionJob


def page(index: int = 0, *, oriented: bool = False) -> PageMeta:
    return PageMeta(
        index=index,
        width=1536,
        height=2048,
        view_width=1536,
        view_height=2048,
        oriented=oriented,
        raw_sha256="a" * 64,
        page_sha256="b" * 64,
        format="jpeg",
        raw_bytes=1000,
    )


def extract_result(name: str = "Banana Mug Cake", *, flags: list[CardFlag] | None = None) -> ExtractResult:
    draft = CardDraft(name=name, ingredients=[CardDraftIngredient(original_text="1 T. coconut oil")])
    return ExtractResult(
        draft=draft,
        flags=flags or [],
        transcription=f"{name}\n1 T. coconut oil",
        extraction=ExtractionMeta(read_path=IngestReadPath.image, provider="Fake"),
        pages=[page(oriented=True)],
    )


def reread_result(text: str = "1/4 tsp salt") -> RereadResult:
    return RereadResult(
        proposal=CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="ingredients"), text=text)
    )


class Jobs:
    """Creates jobs in the test user's household and reads them back; the queue fixture only sees these"""

    def __init__(self, session: Session, user: TestUser) -> None:
        self.session = session
        self.repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
        self.ids: set[UUID] = set()
        self._batch: UUID | None = None

    @property
    def batch_id(self) -> UUID:
        if self._batch is None:
            self._batch = self.repos.batches.create(source=IngestSource.app, created_by=None)
        return self._batch

    def create(
        self,
        *,
        status: IngestStatus = IngestStatus.processing,
        kind: IngestTaskKind | None = IngestTaskKind.extract,
        state: IngestTaskState | None = IngestTaskState.queued,
        priority: int | None = None,
        **values: Any,
    ) -> UUID:
        """A job with a queued task by default (an extraction, priority 10; a re-read gets priority 0)"""
        if priority is None:
            priority = limits.PRIORITY_REREAD if kind == IngestTaskKind.reread else limits.PRIORITY_EXTRACT
        job_id = self.repos.jobs.create(
            {
                "batch_id": self.batch_id,
                "position": len(self.ids),
                "source": IngestSource.app.value,
                "status": status.value,
                "pages": [page()],
                "source_sha256": uuid4().hex * 2,
                "locale": "en-US",
                "task_kind": kind.value if kind else None,
                "task_state": state.value if state else None,
                "task_priority": priority,
                **values,
            }
        )
        self.ids.add(job_id)
        return job_id

    def ready(
        self,
        *,
        kind: IngestTaskKind | None = None,
        state: IngestTaskState | None = None,
        **values: Any,
    ) -> UUID:
        """A `ready` job with a draft nobody edited (and no task, unless one is given)"""
        result = extract_result()
        defaults: dict[str, Any] = {
            "draft": result.draft,
            "flags": [],
            "title": result.draft.name,
            "draft_version": 1,
            "extracted_version": 1,
        }
        return self.create(status=IngestStatus.ready, kind=kind, state=state, **{**defaults, **values})

    def row(self, job_id: UUID) -> RowMapping:
        with session_context() as session:
            return session.execute(sa.select(*Job.__table__.columns).where(Job.id == job_id)).mappings().one()

    def update(self, job_id: UUID, **values: Any) -> None:
        with session_context() as session:
            session.execute(sa.update(Job).where(Job.id == job_id).values(**values))
            session.commit()


Handler = Callable[[TaskContext], Awaitable[Any]]


@dataclass
class FakeHandlers:
    """`tasks.handle_extract` and `handle_reread`, answering a fixed result unless a test sets a behaviour"""

    calls: list[TaskContext] = field(default_factory=list)
    behaviour: dict[UUID, Handler] = field(default_factory=dict)
    default: Handler | None = None

    async def _handle(self, ctx: TaskContext, fallback: Callable[[], Any]) -> Any:
        self.calls.append(ctx)
        handler = self.behaviour.get(ctx.job_id) or self.default
        return await handler(ctx) if handler else fallback()

    async def extract(self, ctx: TaskContext) -> ExtractResult:
        return await self._handle(ctx, extract_result)

    async def reread(self, ctx: TaskContext) -> RereadResult:
        return await self._handle(ctx, reread_result)


def blocking(gate: Any, result: Callable[[], Any] = extract_result) -> Handler:
    """A handler that waits (cancellably) until `gate` (a `threading.Event`) is set, then answers `result()`"""

    async def handler(ctx: TaskContext) -> Any:
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return result()

    return handler


def raising(error: BaseException) -> Handler:
    async def handler(ctx: TaskContext) -> Any:
        raise error

    return handler


@dataclass
class PhaseCalls:
    housekeeping: list[datetime] = field(default_factory=list)
    commits: list[datetime] = field(default_factory=list)
    inbox: int = 0
    purge: list[datetime] = field(default_factory=list)
    notified: list[UUID] = field(default_factory=list)


def run[T](coro: Awaitable[T]) -> T:
    """Runs a coroutine on a new event loop, as the app's would"""

    async def main() -> T:
        return await coro

    return asyncio.run(main())


async def settle(dispatcher: IngestDispatcher, timeout: float = 10) -> None:
    """Waits for the dispatcher's tasks to finish"""
    assert await dispatcher.drain(timeout), "the runner's tasks didn't finish"


async def wait_for(condition: Callable[[], bool], timeout: float = 10) -> None:
    """Polls `condition` on the event loop until it holds"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)
