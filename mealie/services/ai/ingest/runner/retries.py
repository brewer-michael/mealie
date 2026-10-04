"""
Cards waiting for a monthly limit (docs/ai/PHASE2.md §3.6): a card whose first reading failed because every provider
it needs was over its monthly token limit (`limit_reached`) isn't left for the user to retry by hand. Finalize sets its
`auto_retry_at` to the next reset (the first instant of next month, UTC), and the dispatcher's retry phase (every
`HOUSEKEEPING_INTERVAL`) reads it again, as a manual retry does (`IngestQueue.retry_after_limit`), once that time has
come, or sooner once the limit no longer applies: a manager raised it, or added or changed a provider. That check is
made once per run per group and policy, under the policy the card is read with (its own `local_only`, or its group's
setting as it is now), with the same rule as the capture page's warning (`intake.reading_readiness`): the default slot
builds every recipe, and the image slot reads the photo unless OCR can. Only a "still applies" answer is kept, for
`LIMIT_RECHECK_INTERVAL`: a card a lift queued that fails `limit_reached` again (the raised budget ran out after a few
cards) waits for a check that says so, rather than being read again at every run while a "lifted" answer is kept.

Everything here is a conditional update on the card still waiting, so every worker process running it is harmless.
"""

import threading
import time
from datetime import datetime
from uuid import UUID

from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestQueue, IngestRepos, LimitWait
from mealie.schema.group.ai_providers import AIProviderSlot
from mealie.services import ocr
from mealie.services.ai.errors import AIProviderLimitReachedError
from mealie.services.ai.policy import ai_call_policy
from mealie.services.openai import OpenAIService

from .. import limits, storage
from .classify import safe_trace

logger = get_logger(__name__)

_LimitKey = tuple[UUID, bool]
"""(group, local only)"""

_still_applies: dict[_LimitKey, float] = {}
"""When the limit was last found to still apply (by `time.monotonic()`), kept for `LIMIT_RECHECK_INTERVAL`"""
_still_applies_lock = threading.Lock()


def _slot_over_limit(service: OpenAIService, slot: AIProviderSlot) -> bool | None:
    """True when the slot's providers allowed now are all over their monthly limit; False when one is within it;
    None when the slot can't be used for another reason (none set up, none local)"""
    try:
        service.runtime.candidates(slot)
    except AIProviderLimitReachedError:
        return True
    except Exception:
        return None
    return False


def limit_applies(group_id: UUID, household_id: UUID, *, local_only: bool) -> bool | None:
    """
    Whether a card of the group read now, under that policy, would still fail `limit_reached`: True or False, or None
    when it couldn't be read for another reason (then it waits for its retry time, and fails for that reason then).
    Blocking: reads the group's providers and their usage.
    """
    with session_context() as session:
        service = OpenAIService(get_repositories(session, group_id=group_id, household_id=household_id))
        with ai_call_policy(local_only=local_only):
            default = _slot_over_limit(service, AIProviderSlot.default)
            if default is not False:
                return default
            image = _slot_over_limit(service, AIProviderSlot.image)
        session.commit()
    if image is False or ocr.is_available():
        return False
    return image


def _lifted(wait: LimitWait, group_local_only: bool, this_run: dict[_LimitKey, bool]) -> bool:
    """
    Whether the card's limit no longer applies: checked once per run (`this_run`) per group and policy, and not again
    for `LIMIT_RECHECK_INTERVAL` once it said the limit still applies
    """
    local_only = wait.local_only or group_local_only
    key = (wait.group_id, local_only)
    if key in this_run:
        return this_run[key]
    now = time.monotonic()
    with _still_applies_lock:
        checked = _still_applies.get(key)
        if checked is not None and now - checked < limits.LIMIT_RECHECK_INTERVAL:
            this_run[key] = False
            return False
    try:
        lifted = limit_applies(wait.group_id, wait.household_id, local_only=local_only) is False
    except Exception as e:
        logger.warning(f"Recipe card group {wait.group_id}: couldn't check its monthly limits ({type(e).__name__})")
        lifted = False
    this_run[key] = lifted
    with _still_applies_lock:
        if lifted:
            _still_applies.pop(key, None)
        else:
            _still_applies[key] = now
    return lifted


def forget_checks() -> None:
    """Clears the per-group limit checks (tests)"""
    with _still_applies_lock:
        _still_applies.clear()


def retry_waiting(now: datetime) -> int:
    """
    Reads again the cards waiting for a monthly limit whose retry time has come or whose limit no longer applies;
    how many were queued (wake the dispatcher then). Stops quietly while a backup restore pauses ingestion.
    """
    if storage.is_paused():
        return 0
    with session_context() as session:
        waiting = IngestQueue(session).waiting_for_limit()
    if not waiting:
        return 0

    group_local_only: dict[UUID, bool] = {}
    this_run: dict[_LimitKey, bool] = {}
    retried = 0
    for wait in waiting:
        if storage.is_paused():
            break
        try:
            due = wait.auto_retry_at <= now
            if not due:
                if wait.group_id not in group_local_only:
                    with session_context() as session:
                        group_local_only[wait.group_id] = (
                            IngestRepos(session, wait.group_id, None).settings.get().local_only
                        )
                        session.commit()
                if not _lifted(wait, group_local_only[wait.group_id], this_run):
                    continue
            with session_context() as session:
                if IngestQueue(session).retry_after_limit(wait.job_id, wait.household_id):
                    retried += 1
                    reason = "its retry time has come" if due else "its monthly limit no longer applies"
                    logger.info(f"Recipe card job {wait.job_id}: read again, {reason}")
        except Exception as e:
            if storage.is_paused():
                break  # a backup restore began: the rows are being replaced
            logger.error(f"Recipe card job {wait.job_id}: couldn't queue it again after its limit:\n{safe_trace(e)}")
    return retried
