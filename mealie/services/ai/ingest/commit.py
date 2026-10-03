"""
Committing a reviewed card as a recipe (docs/ai/PHASE2.md §7): crash-safe and idempotent, with a server-owned recipe id
and asset token persisted before anything is created, and resumed by the dispatcher when a commit stalls.

The signature is final. Work item B3 provides the commit; until then nothing is resumed.
"""

from datetime import datetime


def resume_stale_commits(now: datetime) -> int:
    """Resumes commits whose lease (`commit_started_at`) is older than `COMMIT_LEASE`: the number resumed"""
    return 0
