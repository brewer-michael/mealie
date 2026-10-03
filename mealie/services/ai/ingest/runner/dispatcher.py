"""
`IngestDispatcher` (docs/ai/PHASE2.md §3.2): one per worker process, started by the ingest router's lifespan
(`APIRouter(lifespan=dispatcher.lifespan)`, so `app.py` needs no change), unless `AI_INGEST_WORKER` is off, as it is
under `TESTING`, where tests call `run_once()`.

The interface is final. Work item B0 provides the runner behind it (claims, the re-read slot, heartbeats, the sweep,
housekeeping, the inbox scan, the daily purge, the pause and shutdown); until then the lifespan starts nothing, and
`wake()` and `run_once()` do nothing, so queued jobs simply wait.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class IngestDispatcher:
    """Claims queued recipe card tasks from the job table and runs them (one instance per process: `dispatcher`)"""

    @asynccontextmanager
    async def lifespan(self, _app: Any) -> AsyncIterator[None]:
        """Runs the dispatcher while the app runs; on exit, stops claiming, cancels its tasks and releases leases"""
        yield

    def wake(self) -> None:
        """Asks the dispatcher to look for work now (intake calls it after inserting a job). Safe from any thread."""

    async def run_once(self) -> None:
        """One pass of every dispatcher phase that's due, for tests and for the loop itself"""


dispatcher = IngestDispatcher()
"""This process's dispatcher"""
