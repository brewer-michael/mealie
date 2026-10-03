"""Recording AI provider attempts in the usage log (docs/ai/PHASE1.md §4)"""

from dataclasses import dataclass

from mealie.core.root_logger import get_logger
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate

logger = get_logger(__name__)


@dataclass
class AITokenUsage:
    """Tokens a provider reported using for one request. Filled in by whoever reads the response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None
    """The model that answered, if the provider reports one other than the configured model (e.g. a fallback)"""


def record_ai_usage(
    repos: AllRepositories,
    provider: AIProviderOut,
    *,
    slot: AIProviderSlot,
    feature: str | None,
    usage: AITokenUsage,
    latency_ms: int,
    error: BaseException | None = None,
    error_type: str | None = None,
) -> None:
    """
    Logs one provider attempt, which failed if there's an `error` or an `error_type` (for a failure that
    isn't an exception, such as an empty answer). A failure to write the row is logged and otherwise
    ignored: the usage log must never break the AI call it describes.
    """
    if error is not None and error_type is None:
        error_type = type(error).__name__

    try:
        repos.group_ai_usage.create(
            AIUsageLogCreate(
                provider_id=provider.id,
                provider_name=provider.name,
                model=usage.model or provider.model,
                protocol=provider.protocol,
                slot=slot,
                feature=feature,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                latency_ms=latency_ms,
                success=error_type is None,
                error_type=error_type,
            )
        )
    except Exception:
        logger.exception(f"Failed to record AI usage for provider '{provider.name}'")
