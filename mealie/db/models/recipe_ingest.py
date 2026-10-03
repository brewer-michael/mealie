"""
Fork-owned tables for recipe card ingestion (docs/ai/PHASE2.md §13): the jobs (each one both a card's review document
and its single pending task), the batches they arrive in, the group's card settings, and which notifiers send the
"recipe cards ready" event.

Kept apart from upstream's model packages so upstream syncs don't conflict. The relationships back to upstream's
models are declared here as backrefs for the same reason. They are what delete these rows along with a group, a
household or a notifier: SQLite doesn't enforce foreign keys, so the database itself won't.

Rules every column here follows, so a backup restores it unchanged (F15): GUID primary keys; JSON stored as text
(`JsonText`); `NaiveDateTime` timestamps, each listed in `AlchemyExporter.look_for_datetime`; and no generated string
value that `uuid.UUID` would accept (tokens are GUID columns, hashes are 64 hex characters, `source_name` starts with a
prefix holding a `/`). Users and recipes are referenced without foreign keys, so deleting either is never blocked.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import orm

from ._model_base import BaseMixins, SqlAlchemyBase
from ._model_utils.auto_init import auto_init
from ._model_utils.datetime import NaiveDateTime
from ._model_utils.guid import GUID
from ._model_utils.json_text import JsonText
from .household.events import GroupEventNotifierModel

if TYPE_CHECKING:
    from .group import Group
    from .household import Household


class RecipeIngestionBatch(SqlAlchemyBase, BaseMixins):
    """
    One capture session, Shortcut run or inbox burst (§1.4). It groups the queue, orders the review and sends one
    notification once it's sealed and none of its cards is still being read.
    """

    __tablename__ = "recipe_ingestion_batches"
    __table_args__ = (
        sa.Index("ix_recipe_ingestion_batches_household_source_sealed", "household_id", "source", "sealed_at"),
    )

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)

    group_id: orm.Mapped[GUID] = orm.mapped_column(GUID, sa.ForeignKey("groups.id"), nullable=False, index=True)
    group: orm.Mapped[Group] = orm.relationship(
        "Group", backref=orm.backref("recipe_ingestion_batches", cascade="all, delete-orphan")
    )
    household_id: orm.Mapped[GUID] = orm.mapped_column(GUID, sa.ForeignKey("households.id"), nullable=False, index=True)
    household: orm.Mapped[Household] = orm.relationship(
        "Household", backref=orm.backref("recipe_ingestion_batches", cascade="all, delete-orphan")
    )

    created_by: orm.Mapped[GUID | None] = orm.mapped_column(GUID, nullable=True)
    """The uploader; none for the inbox. No foreign key, so deleting the user is never blocked."""
    source: orm.Mapped[str] = orm.mapped_column(sa.String(16), nullable=False)
    """`app`, `api` or `inbox` (`IngestSource`)"""
    source_key: orm.Mapped[str | None] = orm.mapped_column(sa.String(255), nullable=True)
    """The inbox folder an inbox batch auto-joins on"""
    locale: orm.Mapped[str | None] = orm.mapped_column(sa.String, nullable=True)
    """The notification's language"""

    last_upload_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)
    sealed_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)
    """Set once; a sealed batch never gains a card"""
    notified_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)

    @auto_init()
    def __init__(self, **_) -> None:
        pass


class RecipeIngestionJob(SqlAlchemyBase, BaseMixins):
    """
    One recipe card (one or more pages). `status` is its review lifecycle; the `task_*` and lease columns are its
    single pending piece of work (§3.1). Its files are under `DATA_DIR/groups/<group_id>/ai-ingest/<id>/`.
    """

    __tablename__ = "recipe_ingestion_jobs"
    __table_args__ = (
        sa.Index("ix_recipe_ingestion_jobs_task_state_priority_created", "task_state", "task_priority", "created_at"),
        sa.Index("ix_recipe_ingestion_jobs_household_status_created", "household_id", "status", "created_at"),
        sa.Index("ix_recipe_ingestion_jobs_household_source_sha256", "household_id", "source_sha256"),
        sa.Index("ix_recipe_ingestion_jobs_batch_position", "batch_id", "position"),
    )

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)

    group_id: orm.Mapped[GUID] = orm.mapped_column(GUID, sa.ForeignKey("groups.id"), nullable=False, index=True)
    group: orm.Mapped[Group] = orm.relationship(
        "Group", backref=orm.backref("recipe_ingestion_jobs", cascade="all, delete-orphan")
    )
    household_id: orm.Mapped[GUID] = orm.mapped_column(GUID, sa.ForeignKey("households.id"), nullable=False, index=True)
    household: orm.Mapped[Household] = orm.relationship(
        "Household", backref=orm.backref("recipe_ingestion_jobs", cascade="all, delete-orphan")
    )
    batch_id: orm.Mapped[GUID] = orm.mapped_column(
        GUID, sa.ForeignKey("recipe_ingestion_batches.id"), nullable=False, index=True
    )
    batch: orm.Mapped[RecipeIngestionBatch] = orm.relationship(
        RecipeIngestionBatch, backref=orm.backref("jobs", cascade="all, delete-orphan")
    )
    position: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    """Capture order within the batch; ties sort by `created_at`"""

    created_by: orm.Mapped[GUID | None] = orm.mapped_column(GUID, nullable=True)
    committed_by: orm.Mapped[GUID | None] = orm.mapped_column(GUID, nullable=True)
    source: orm.Mapped[str] = orm.mapped_column(sa.String(16), nullable=False)
    """`app`, `api` or `inbox`: the batch's"""
    source_name: orm.Mapped[str | None] = orm.mapped_column(sa.String(255), nullable=True)
    """`upload/<file name>` or `inbox/<group>/<household>/<path>`: never a bare UUID (F15)"""
    integration_id: orm.Mapped[str | None] = orm.mapped_column(sa.String, nullable=True)
    locale: orm.Mapped[str | None] = orm.mapped_column(sa.String, nullable=True)
    local_only: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)
    """A snapshot taken at intake, so later settings changes never loosen a queued job"""

    status: orm.Mapped[str] = orm.mapped_column(sa.String(16), nullable=False, index=True)
    """`IngestStatus`"""
    title: orm.Mapped[str | None] = orm.mapped_column(sa.String(255), nullable=True)
    """The draft's name, for lists"""
    draft_version: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    """The review page's concurrency token, bumped only when the draft changes"""
    extracted_version: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    """The `draft_version` the last extraction wrote; equal to it while nobody has edited the draft"""
    row_version: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    """Bumped by every write of the JSON, error or status columns, for optimistic read-modify-writes (§3.3)"""
    error_count: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    warning_count: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)

    task_kind: orm.Mapped[str | None] = orm.mapped_column(sa.String(16), nullable=True)
    """`extract` or `reread` (`IngestTaskKind`)"""
    task_state: orm.Mapped[str | None] = orm.mapped_column(sa.String(16), nullable=True)
    """`NULL` (idle), `queued` or `running` (`IngestTaskState`)"""
    task_priority: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    attempts: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    rate_limit_retries: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    task_payload: orm.Mapped[Any] = orm.mapped_column(JsonText, nullable=True)
    not_before: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)
    lease_expires_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)
    task_started_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)
    lease_token: orm.Mapped[GUID | None] = orm.mapped_column(GUID, nullable=True)
    """New for every claim; every write by a running task is fenced on it"""
    lease_owner: orm.Mapped[str | None] = orm.mapped_column(sa.String(64), nullable=True)
    """`host:pid:instance`, for logs"""
    cancel_requested: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)
    progress_key: orm.Mapped[str | None] = orm.mapped_column(sa.String(64), nullable=True)

    pages: orm.Mapped[Any] = orm.mapped_column(JsonText, nullable=False, default=list)
    """`list[PageMeta]`"""
    source_sha256: orm.Mapped[str] = orm.mapped_column(sa.String(64), nullable=False)
    """SHA-256 of the ordered pages' `raw_sha256`s, for finding duplicates (§2)"""
    transcription: orm.Mapped[str | None] = orm.mapped_column(sa.Text, nullable=True)
    extraction: orm.Mapped[Any] = orm.mapped_column(JsonText, nullable=True)
    """`ExtractionMeta`"""
    draft: orm.Mapped[Any] = orm.mapped_column(JsonText, nullable=True)
    """`CardDraft`"""
    flags: orm.Mapped[Any] = orm.mapped_column(JsonText, nullable=True)
    """`list[CardFlag]`, with their resolutions"""
    proposals: orm.Mapped[Any] = orm.mapped_column(JsonText, nullable=True)
    """`list[CardProposal]`"""
    error_code: orm.Mapped[str | None] = orm.mapped_column(sa.String(64), nullable=True)
    """`IngestErrorCode`"""
    error_params: orm.Mapped[Any] = orm.mapped_column(JsonText, nullable=True)

    commit_recipe_id: orm.Mapped[GUID | None] = orm.mapped_column(GUID, nullable=True)
    """The recipe id a commit reserved before creating anything (§7)"""
    recipe_id: orm.Mapped[GUID | None] = orm.mapped_column(GUID, nullable=True, index=True)
    """The committed recipe: the job is its provenance"""
    commit_asset_token: orm.Mapped[str | None] = orm.mapped_column(sa.String(32), nullable=True)
    """Base64url; makes the card asset names unguessable"""
    commit_started_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)
    """The commit's lease"""
    committed_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)

    @auto_init()
    def __init__(self, **_) -> None:
        pass


class RecipeIngestionSettings(SqlAlchemyBase, BaseMixins):
    """A group's recipe card settings. No row means the defaults."""

    __tablename__ = "recipe_ingestion_settings"

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)

    group_id: orm.Mapped[GUID] = orm.mapped_column(
        GUID, sa.ForeignKey("groups.id"), nullable=False, unique=True, index=True
    )
    group: orm.Mapped[Group] = orm.relationship(
        "Group", backref=orm.backref("recipe_ingestion_settings", uselist=False, cascade="all, delete-orphan")
    )

    local_only: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)
    """Every card job of the group is local only (§10)"""
    cross_read: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)
    """Read every card a second time and compare (§4.5)"""

    @auto_init()
    def __init__(self, **_) -> None:
        pass


class AIEventNotifierOptions(SqlAlchemyBase, BaseMixins):
    """Which of the fork's AI events a notifier sends (off unless a row says so)"""

    __tablename__ = "ai_event_notifier_options"

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)

    notifier_id: orm.Mapped[GUID] = orm.mapped_column(
        GUID, sa.ForeignKey("group_events_notifiers.id"), nullable=False, unique=True, index=True
    )
    # Deleting the notifier (or its household or group) deletes its options
    notifier: orm.Mapped[GroupEventNotifierModel] = orm.relationship(
        GroupEventNotifierModel,
        backref=orm.backref("ai_event_options", uselist=False, cascade="all, delete-orphan"),
    )

    recipe_ingestion_ready: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)

    @auto_init()
    def __init__(self, **_) -> None:
        pass
