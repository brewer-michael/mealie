"""
Batches (docs/ai/PHASE2.md §1.4): choosing the batch an upload joins, and sealing idle ones. A sealed batch never
gains a card: the job insert touches its batch with an `UPDATE` conditional on `sealed_at IS NULL`, in the insert's
transaction, and picks another batch when that matches nothing.

The signatures are final. Work item B2 provides the implementations.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import IngestSource


def select_batch(
    repos: IngestRepos,
    *,
    batch_id: UUID | Literal["new"] | None,
    source: IngestSource,
    created_by: UUID | None,
    source_key: str | None,
    locale: str | None,
    now: datetime,
) -> UUID:
    """
    The batch an upload goes into: the app's explicit batch while it's unsealed; otherwise (no `batch_id`) the newest
    unsealed batch of the same uploader, source and source key that saw an upload in the last 2 minutes; otherwise,
    or with `batch_id="new"`, a new batch. An unknown or foreign `batch_id` raises `NoEntryFound`.
    """
    raise NotImplementedError("Batches are work item B2")


def seal_idle_batches(session: Session, now: datetime) -> list[UUID]:
    """Seals every household's batches idle for too long (app 10 minutes, API and inbox 2); returns their ids"""
    return []
