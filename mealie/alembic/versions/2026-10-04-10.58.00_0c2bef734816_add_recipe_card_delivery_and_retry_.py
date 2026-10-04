"""add recipe card notification delivery, automatic retry and recipe event columns

Revision ID: 0c2bef734816
Revises: cc5357be7e71
Create Date: 2026-10-04 10:58:00.000000

"""

import sqlalchemy as sa
from alembic import op

import mealie.db.migration_types

# revision identifiers, used by Alembic.
revision = "0c2bef734816"
down_revision: str | None = "cc5357be7e71"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade():
    with op.batch_alter_table("recipe_ingestion_batches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("notify_claimed_at", mealie.db.migration_types.NaiveDateTime(), nullable=True))
        batch_op.add_column(sa.Column("notify_attempts", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("notify_delivered", sa.Text(), nullable=True))

    with op.batch_alter_table("recipe_ingestion_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("auto_retry_at", mealie.db.migration_types.NaiveDateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("recipe_event_claimed_at", mealie.db.migration_types.NaiveDateTime(), nullable=True)
        )
        batch_op.add_column(sa.Column("recipe_event_sent_at", mealie.db.migration_types.NaiveDateTime(), nullable=True))
        batch_op.create_index("ix_recipe_ingestion_jobs_status_auto_retry", ["status", "auto_retry_at"], unique=False)
        batch_op.create_index(
            "ix_recipe_ingestion_jobs_status_event_sent", ["status", "recipe_event_sent_at"], unique=False
        )

    # Cards committed before this revision had their recipe_created sent (or lost) already: don't send it again
    jobs = sa.table(
        "recipe_ingestion_jobs",
        sa.column("status", sa.String),
        sa.column("committed_at", mealie.db.migration_types.NaiveDateTime()),
        sa.column("update_at", mealie.db.migration_types.NaiveDateTime()),
        sa.column("created_at", mealie.db.migration_types.NaiveDateTime()),
        sa.column("recipe_event_sent_at", mealie.db.migration_types.NaiveDateTime()),
    )
    op.execute(
        jobs.update()
        .where(jobs.c.status == "committed")
        .values(recipe_event_sent_at=sa.func.coalesce(jobs.c.committed_at, jobs.c.update_at, jobs.c.created_at))
    )


def downgrade():
    with op.batch_alter_table("recipe_ingestion_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_recipe_ingestion_jobs_status_event_sent")
        batch_op.drop_index("ix_recipe_ingestion_jobs_status_auto_retry")
        batch_op.drop_column("recipe_event_sent_at")
        batch_op.drop_column("recipe_event_claimed_at")
        batch_op.drop_column("auto_retry_at")

    with op.batch_alter_table("recipe_ingestion_batches", schema=None) as batch_op:
        batch_op.drop_column("notify_delivered")
        batch_op.drop_column("notify_attempts")
        batch_op.drop_column("notify_claimed_at")
