"""Which AI providers to try, and in what order, for each kind of task (docs/ai/PHASE1.md §1)"""

from collections.abc import Mapping

from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from mealie.core.root_logger import get_logger
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot

from .errors import AIProviderLimitReachedError

logger = get_logger(__name__)

PRIMARY_SLOTS = frozenset({AIProviderSlot.default, AIProviderSlot.image, AIProviderSlot.audio})
"""Slots with an upstream primary provider (`*_provider_id` on the group's AI provider settings)"""

DEFAULT_FALLBACK_SLOTS = frozenset({AIProviderSlot.planner, AIProviderSlot.fast})
"""Slots that use the `default` slot's providers when they have no routes of their own"""


def _not_configured(slot: AIProviderSlot) -> Exception:
    # Upstream's exception and messages ("No default provider set"), which callers already map to
    # "AI isn't set up". Imported here because mealie.services.openai imports this module.
    from mealie.services.openai.openai import OpenAINotEnabledException

    return OpenAINotEnabledException(f"No {slot.value} provider set")


class AIProviderRouter:
    """
    Resolves a slot's candidate providers for one group:

    - `default`, `image`, `audio`: that slot's upstream primary provider, then the slot's routes. Without a
      primary the slot has no providers at all, as upstream's checks (e.g. `ai_enabled`) assume
    - `planner`, `fast`: the slot's routes; if it has none, the `default` candidates
    - `embedding`: the slot's routes only

    Duplicates are dropped, keeping the first, and providers at or over their monthly token limit
    are skipped.
    """

    def __init__(self, repos: AllRepositories, primaries: Mapping[AIProviderSlot, AIProviderOut | None]) -> None:
        self.repos = repos
        self.primaries = primaries

    def candidates(self, slot: AIProviderSlot) -> list[AIProviderOut]:
        """
        The providers to try for `slot`, in order.

        Raises upstream's `OpenAINotEnabledException` if the slot has no providers at all, and
        `AIProviderLimitReachedError` if every one of them has reached its monthly token limit.
        """
        routes = self._get_routes()
        if slot in DEFAULT_FALLBACK_SLOTS and not routes.get(slot):
            slot = AIProviderSlot.default

        providers = self._resolve(slot, routes)
        if not providers:
            raise _not_configured(slot)

        available = self._within_limits(providers)
        if not available:
            names = ", ".join(provider.name for provider in providers)
            raise AIProviderLimitReachedError(
                f"Every AI provider for {slot.value} tasks has reached its monthly token limit ({names})."
            )

        return available

    def _get_routes(self) -> Mapping[AIProviderSlot, list[UUID4]]:
        try:
            return self.repos.group_ai_provider_routes.get_routes()
        except NoResultFound:
            # A group without AI provider settings has no routes either
            return {}

    def _resolve(self, slot: AIProviderSlot, routes: Mapping[AIProviderSlot, list[UUID4]]) -> list[AIProviderOut]:
        candidates: dict[UUID4, AIProviderOut] = {}
        if slot in PRIMARY_SLOTS:
            if not (primary := self.primaries.get(slot)):
                return []

            candidates[primary.id] = primary

        route_ids = [provider_id for provider_id in routes.get(slot, []) if provider_id not in candidates]
        if route_ids:
            providers = {provider.id: provider for provider in self.repos.group_ai_providers.get_all()}
            for provider_id in route_ids:
                if provider := providers.get(provider_id):
                    candidates.setdefault(provider_id, provider)

        return list(candidates.values())

    def _within_limits(self, providers: list[AIProviderOut]) -> list[AIProviderOut]:
        limits = {
            provider.id: provider.monthly_token_limit
            for provider in providers
            if isinstance(provider.monthly_token_limit, int)
        }
        if not limits:
            return providers

        used = self.repos.group_ai_usage.monthly_tokens(limits)
        available: list[AIProviderOut] = []
        for provider in providers:
            if provider.id in limits and used.get(provider.id, 0) >= limits[provider.id]:
                logger.info(f"Skipping AI provider '{provider.name}': it has reached its monthly token limit")
                continue

            available.append(provider)

        return available
