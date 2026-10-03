"""
Retention (docs/ai/PHASE2.md §16): the daily purge of committed and failed cards' files, empty batches and orphan
job directories. Ready and committing jobs, eval cases and `recipes/` are never touched.

The signature is final. Work item B3 provides the purge; until then nothing is purged.
"""

from datetime import datetime


def purge_once(now: datetime) -> None:
    """One idempotent pass of the purge; each job's file work runs inside the ingest write lock"""
