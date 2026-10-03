"""
The AI service a recipe card task uses (docs/ai/PHASE2.md §3.7, F11). The task's dedicated session is only used for
routing reads, the usage log and read-only lookups, and **no transaction stays open across an await**: on PostgreSQL a
session idle in a transaction while a provider answers would block a backup restore's `DROP` and DDL.

`JobAIRuntime` ends the session's transaction after the base runtime's reads (`candidates`) and usage writes
(`record_attempt`, whose `refresh` reopens one), since the runtime goes straight from those to awaiting a provider.
Other awaits that follow a read on the session call `end_transaction` first. The base `AIRuntime` keeps its
request-path behaviour; only the job's own session is ended, and nothing is ever rolled back.

Work item B1 adds the per-feature token, latency and model tallies for `ExtractionMeta`.
"""

from functools import cached_property

from sqlalchemy.orm import Session

from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.services.ai.runtime import AIRuntime
from mealie.services.ai.usage import AITokenUsage
from mealie.services.openai import OpenAIService


def end_transaction(session: Session) -> None:
    """Commits the session's transaction if one is open (it only ever holds reads and usage rows); else does nothing"""
    if session.in_transaction():
        session.commit()


class JobAIRuntime(AIRuntime):
    """The base runtime, ending the job session's transaction before every provider await"""

    def candidates(self, slot: AIProviderSlot) -> list[AIProviderOut]:
        try:
            return super().candidates(slot)
        finally:
            end_transaction(self.service.repos.session)

    def record_attempt(
        self,
        provider: AIProviderOut,
        *,
        slot: AIProviderSlot,
        feature: str,
        usage: AITokenUsage,
        latency_ms: int,
        error: BaseException | None = None,
        error_type: str | None = None,
    ) -> None:
        try:
            super().record_attempt(
                provider,
                slot=slot,
                feature=feature,
                usage=usage,
                latency_ms=latency_ms,
                error=error,
                error_type=error_type,
            )
        finally:
            end_transaction(self.service.repos.session)


class JobOpenAIService(OpenAIService):
    """`OpenAIService` on a task's dedicated session, routing through `JobAIRuntime`"""

    @cached_property
    def runtime(self) -> JobAIRuntime:
        return JobAIRuntime(self)
