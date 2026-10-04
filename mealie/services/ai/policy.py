"""
A per-task policy for routed AI calls (docs/ai/PHASE2.md §10): "local only" keeps every call a recipe card causes on
the group's own network, and `job_id` tags each usage row with the card it was for.

The policy lives in a `ContextVar`, which follows awaits and `asyncio.to_thread`, so it covers code that builds its own
`OpenAIService` too. The base `AIRuntime.candidates()` filters the slot's providers with `apply_policy` before their
monthly limits apply, for every slot, and fails closed: when no local provider is left it raises
`AIProviderLocalOnlyError` rather than fall back to a cloud one. Under "local only" the provider SDKs also connect only
to the private address they check (`local.local_only_http_client`).

Whether a call is local-only is decided again for every call (`is_local_only`): a policy can carry a check, such as
the group's current setting, that turns it on for the rest of a task.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID

from mealie.core.root_logger import get_logger
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot

from .errors import AIProviderLocalOnlyError
from .local import is_local_provider

logger = get_logger(__name__)


@dataclass(frozen=True)
class AICallPolicy:
    local_only: bool = False
    """Only providers that run on the group's network (`is_local_provider`) may be called"""
    job_id: UUID | None = None
    """The recipe card job the calls are for, recorded on each usage row"""
    local_only_check: Callable[[], bool] | None = field(default=None, compare=False)
    """
    When `local_only` is off, asked before every provider call (`is_local_only`): True makes that call and the ones
    after it local-only, e.g. once a manager switches the group's setting on while a card is read. It's called on the
    event loop or in a worker thread, so it should be quick (cached). If it raises, the call is local-only.
    """
    _check_failed: threading.Event = field(default_factory=threading.Event, init=False, compare=False, repr=False)


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


def is_local_only(policy: AICallPolicy) -> bool:
    """
    Whether a call made now under `policy` must stay on the group's network: `local_only`, or else what its
    `local_only_check` says. A check that fails counts as "local only" (fail closed), and is logged once per policy.
    """
    if policy.local_only:
        return True
    if policy.local_only_check is None:
        return False

    try:
        return bool(policy.local_only_check())
    except Exception as e:
        if not policy._check_failed.is_set():
            policy._check_failed.set()
            job = f"Recipe card job {policy.job_id}: " if policy.job_id else ""
            logger.warning(
                f"{job}checking whether AI calls must stay local-only failed ({type(e).__name__}); "
                "keeping them on this server"
            )
        return True


def apply_policy(slot: AIProviderSlot, providers: list[AIProviderOut]) -> list[AIProviderOut]:
    """
    The providers the current policy allows for `slot` now (`is_local_only`), in order. Under "local only" that's the
    local ones, and an empty result raises `AIProviderLocalOnlyError` (fail closed).
    """
    if not is_local_only(current_policy()):
        return providers

    allowed = [provider for provider in providers if is_local_provider(provider)]
    if not allowed:
        raise AIProviderLocalOnlyError(
            f"This recipe card must stay on your network, but none of the AI providers for {slot.value} tasks is "
            "marked as running locally at a private address."
        )
    return allowed
