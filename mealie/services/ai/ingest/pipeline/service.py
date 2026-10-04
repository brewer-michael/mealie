"""
The AI service a recipe card task uses (docs/ai/PHASE2.md §3.7, F11). The task's dedicated session is only used for
routing reads, the usage log and read-only lookups, and **no transaction stays open across an await**: on PostgreSQL a
session idle in a transaction while a provider answers would block a backup restore's `DROP` and DDL.

`JobAIRuntime` ends the session's transaction after the base runtime's reads (`candidates`) and usage writes
(`record_attempt`, whose `refresh` reopens one), since the runtime goes straight from those to awaiting a provider.
Other awaits that follow a read on the session call `end_transaction` first. The base `AIRuntime` keeps its
request-path behaviour; only the job's own session is ended, and nothing is ever rolled back.

`JobAIRuntime` also tallies each attempt's tokens and time per feature and provider, and which model answered, for the
job's `ExtractionMeta` (the usage log has the same rows, but a task shouldn't have to read them back).
"""

from functools import cached_property

from sqlalchemy.orm import Session

from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.schema.recipe_ingest import ExtractionUsage
from mealie.services.ai.runtime import AIRuntime
from mealie.services.ai.usage import AITokenUsage
from mealie.services.openai import OpenAIService


def end_transaction(session: Session) -> None:
    """Commits the session's transaction if one is open (it only ever holds reads and usage rows); else does nothing"""
    if session.in_transaction():
        session.commit()


class JobAIRuntime(AIRuntime):
    """The base runtime, ending the job session's transaction before every provider await, and tallying usage"""

    def __init__(self, service: OpenAIService) -> None:
        super().__init__(service)
        self._usage: dict[tuple[str, str, str], ExtractionUsage] = {}
        self._answered: dict[str, tuple[str, str]] = {}

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

        self._tally(provider, slot, feature, usage, latency_ms, failed=error is not None or error_type is not None)

    def _tally(
        self,
        provider: AIProviderOut,
        slot: AIProviderSlot,
        feature: str,
        usage: AITokenUsage,
        latency_ms: int,
        *,
        failed: bool,
    ) -> None:
        model = usage.model or provider.model
        key = (feature, slot.value, provider.name)
        tally = self._usage.get(key)
        if tally is None:
            tally = self._usage[key] = ExtractionUsage(feature=feature, slot=slot.value, provider=provider.name)
        tally.model = model
        tally.requests += 1
        tally.failures += int(failed)
        tally.prompt_tokens += usage.prompt_tokens
        tally.completion_tokens += usage.completion_tokens
        tally.latency_ms += latency_ms
        if not failed:
            self._answered[feature] = (provider.name, model)

    @property
    def usage(self) -> list[ExtractionUsage]:
        """Every attempt so far, tallied per feature (the response schema's name), slot and provider"""
        return [tally.model_copy() for tally in self._usage.values()]

    def answered_by(self, feature: str) -> tuple[str, str] | None:
        """The provider and model that last answered a request for `feature`, if any did"""
        return self._answered.get(feature)


class JobOpenAIService(OpenAIService):
    """`OpenAIService` on a task's dedicated session, routing through `JobAIRuntime`"""

    @cached_property
    def runtime(self) -> JobAIRuntime:
        return JobAIRuntime(self)
