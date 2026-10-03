"""
The recipe card task handlers (docs/ai/PHASE2.md §3.7): the runner calls one per claimed task, in the task's thread
and event loop, with the locale context and the job's AI call policy already set. A handler opens its own sessions,
returns a result and writes nothing to the job row: the runner applies the result with a write fenced on the lease.
A failure it understands is raised as `TaskFailed`.

The signatures are final. Work item B1 provides the handlers.
"""

from .runner.types import ExtractResult, RereadResult, TaskContext


async def handle_extract(ctx: TaskContext) -> ExtractResult:
    """
    A first extraction, retry or re-extract: orients pages not yet oriented, then `pipeline.extract_card` with a
    `JobOpenAIService` on a dedicated session.
    """
    raise NotImplementedError("The extraction handler is work item B1")


async def handle_reread(ctx: TaskContext) -> RereadResult:
    """A region re-read (`ctx.payload` holds the page, region and target): `pipeline.reread_region`"""
    raise NotImplementedError("The re-read handler is work item B1")
