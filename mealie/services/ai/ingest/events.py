"""
"Recipe cards ready" notifications (docs/ai/PHASE2.md §8): one per finished batch, through the household's Apprise
notifiers that opted in, never through `EventBusService.dispatch`.

The signatures are final. Work item B4 provides them; until then nothing is sent.
"""

from datetime import datetime
from uuid import UUID


def maybe_notify_batch(batch_id: UUID) -> bool:
    """
    Sends the batch's notification if it's due: sealed, not yet notified, created in the last 24 hours, and none of
    its cards still processing. The conditional `notified_at` update decides, so it's sent at most once across
    processes. Whether this call sent it. Blocking (Apprise).
    """
    return False


def housekeeping(now: datetime) -> None:
    """Seals idle batches and sends the notifications that became due (the dispatcher, every minute)"""
