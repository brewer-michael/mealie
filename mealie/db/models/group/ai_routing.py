"""
Fork-owned AI provider tables: per-slot fallback routes and the usage log.

Kept apart from upstream's `ai_providers.py` so upstream syncs don't conflict. The relationships back
to upstream's models are declared here as backrefs for the same reason.
"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy

from .._model_base import BaseMixins, SqlAlchemyBase
from .._model_utils.auto_init import auto_init
from .._model_utils.guid import GUID
from .ai_providers import AIProvider, AIProviderSettings

if TYPE_CHECKING:
    from .group import Group


class AIProviderRoute(SqlAlchemyBase, BaseMixins):
    """One entry in a slot's ordered fallback list: `provider` is tried at `position` (0-based) for `slot`"""

    __tablename__ = "ai_provider_routes"
    __table_args__ = (
        sa.UniqueConstraint("settings_id", "slot", "position", name="ai_provider_routes_settings_id_slot_position_key"),
        sa.UniqueConstraint(
            "settings_id", "slot", "provider_id", name="ai_provider_routes_settings_id_slot_provider_id_key"
        ),
    )

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)
    group_id: AssociationProxy[GUID] = association_proxy("settings", "group_id")

    settings_id: orm.Mapped[GUID] = orm.mapped_column(
        GUID, sa.ForeignKey("ai_provider_settings.id"), nullable=False, index=True
    )
    # Deleting a group's settings (i.e. the group) deletes its routes
    settings: orm.Mapped[AIProviderSettings] = orm.relationship(
        AIProviderSettings, backref=orm.backref("routes", cascade="all, delete-orphan")
    )

    slot: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    position: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False)

    provider_id: orm.Mapped[GUID] = orm.mapped_column(
        GUID, sa.ForeignKey("ai_providers.id"), nullable=False, index=True
    )
    # No cascade: deleting a single provider removes its routes explicitly (GroupRepositoryAIProvider.delete).
    # The relationship makes the unit of work delete routes before providers when a whole group goes.
    provider: orm.Mapped[AIProvider] = orm.relationship(AIProvider)

    @auto_init()
    def __init__(self, **_) -> None:
        pass


class AIUsageLog(SqlAlchemyBase, BaseMixins):
    """One row per provider attempt (successful or not) made by the AI service"""

    __tablename__ = "ai_usage_log"

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)

    group_id: orm.Mapped[GUID] = orm.mapped_column(GUID, sa.ForeignKey("groups.id"), nullable=False, index=True)
    # Deleting a group deletes its usage history
    group: orm.Mapped[Group] = orm.relationship(
        "Group", backref=orm.backref("ai_usage_logs", cascade="all, delete-orphan")
    )

    # Nulled when the provider is deleted (GroupRepositoryAIProvider.delete), so the history survives it
    provider_id: orm.Mapped[GUID | None] = orm.mapped_column(
        GUID, sa.ForeignKey("ai_providers.id"), nullable=True, index=True
    )
    provider: orm.Mapped[AIProvider | None] = orm.relationship(AIProvider)

    provider_name: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    model: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    protocol: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    slot: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    feature: orm.Mapped[str | None] = orm.mapped_column(sa.String, nullable=True)
    """The response schema's class name, e.g. `OpenAIRecipe`"""

    prompt_tokens: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    completion_tokens: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)
    latency_ms: orm.Mapped[int] = orm.mapped_column(sa.Integer, nullable=False, default=0)

    success: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False)
    error_type: orm.Mapped[str | None] = orm.mapped_column(sa.String, nullable=True)
    """The exception's class name, if the attempt failed"""

    @auto_init()
    def __init__(self, **_) -> None:
        pass
