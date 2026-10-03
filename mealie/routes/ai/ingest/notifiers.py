"""
The AI event toggles of a household notifier, `/api/ai/notifiers/{notifier_id}/events` (docs/ai/PHASE2.md §8).
Work item B4 adds the routes.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/ai/notifiers", tags=["AI: Recipe Cards"])
