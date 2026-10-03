"""
A per-task policy for routed AI calls (docs/ai/PHASE2.md §10): "local only" keeps every call a recipe card causes on
the group's own network, and `job_id` tags each usage row with the card it was for.

The policy lives in a `ContextVar`, which follows awaits and `asyncio.to_thread`, so it covers code that builds its own
`OpenAIService` too. The base `AIRuntime.candidates()` ends with `apply_policy`, which filters every slot and fails
closed: when no local provider is left it raises `AIProviderLocalOnlyError` rather than fall back to a cloud one.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot

from .errors import AIProviderLocalOnlyError
from .local import is_local_provider


@dataclass(frozen=True)
class AICallPolicy:
    local_only: bool = False
    """Only providers that run on the group's network (`is_local_provider`) may be called"""
    job_id: UUID | None = None
    """The recipe card job the calls are for, recorded on each usage row"""


NO_POLICY = AICallPolicy()

_policy: ContextVar[AICallPolicy] = ContextVar("ai_call_policy", default=NO_POLICY)


@contextmanager
def ai_call_policy(
    policy: AICallPolicy | None = None, *, local_only: bool = False, job_id: UUID | None = None
) -> Iterator[AICallPolicy]:
    """
    Applies a policy to every routed AI call made inside the block (in this task, and in threads and tasks started
    from it): `ai_call_policy(AICallPolicy(local_only=..., job_id=...))` or `ai_call_policy(local_only=...)`.
    """
    applied = policy if policy is not None else AICallPolicy(local_only=local_only, job_id=job_id)
    token = _policy.set(applied)
    try:
        yield applied
    finally:
        _policy.reset(token)


def current_policy() -> AICallPolicy:
    """The policy of the current context; without one, anything goes and nothing is tagged"""
    return _policy.get()


def apply_policy(slot: AIProviderSlot, providers: list[AIProviderOut]) -> list[AIProviderOut]:
    """
    The providers the current policy allows for `slot`, in order. Under "local only" that's the local ones, and an
    empty result raises `AIProviderLocalOnlyError` (fail closed).
    """
    if not current_policy().local_only:
        return providers

    allowed = [provider for provider in providers if is_local_provider(provider)]
    if not allowed:
        raise AIProviderLocalOnlyError(
            f"This recipe card must stay on your network, but none of the AI providers for {slot.value} tasks is "
            "marked as running locally at a private address."
        )
    return allowed
