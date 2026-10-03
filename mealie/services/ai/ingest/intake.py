"""
Intake (docs/ai/PHASE2.md §2): turning one card's uploaded images into a job, inside the ingest write lock. Pages are
normalized into the new job's directory, then one transaction checks for a duplicate, touches the batch (unsealed
only) and inserts the job with its extraction queued; the dispatcher is woken. The uploaded bytes never reach
`DATA_DIR`. Used by the upload route and the inbox.

Work item B2 provides `IntakeService`.
"""

from uuid import UUID

from sqlalchemy.orm import Session


class IntakeService:
    """Creates recipe card jobs for one group and household"""

    def __init__(self, session: Session, group_id: UUID, household_id: UUID) -> None:
        self.session = session
        self.group_id = group_id
        self.household_id = household_id
