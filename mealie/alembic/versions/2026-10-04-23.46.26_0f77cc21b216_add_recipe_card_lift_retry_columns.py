"""add recipe card lift retry columns

A card waiting for a monthly limit keeps its lift backoff (how often a raised limit queued it again, and when the next
may) on its row, so every worker process waits the same (docs/ai/PHASE2.md §3.6)

Revision ID: 0f77cc21b216
Revises: 0c2bef734816
Create Date: 2026-10-04 23:46:26.158241

"""

import sqlalchemy as sa
from alembic import op

import mealie.db.migration_types

# revision identifiers, used by Alembic.
revision = "0f77cc21b216"
down_revision: str | None = "0c2bef734816"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade():
    with op.batch_alter_table("recipe_ingestion_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lift_retries", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("lift_retry_at", mealie.db.migration_types.NaiveDateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("recipe_ingestion_jobs", schema=None) as batch_op:
        batch_op.drop_column("lift_retry_at")
        batch_op.drop_column("lift_retries")
