"""Fork-owned repositories for AI provider fallback routes and the usage log (docs/ai/PHASE1.md)"""

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import cast

import sqlalchemy as sa
from fastapi import HTTPException, status
from pydantic import UUID4
from sqlalchemy.engine import CursorResult

from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.group.ai_routing import AIProviderRoute, AIUsageLog
from mealie.schema.group.ai_providers import AIProviderSlot
from mealie.schema.group.ai_routing import (
    AIProviderRouteOut,
    AIUsageDaySummary,
    AIUsageLogCreate,
    AIUsageLogOut,
    AIUsageProviderSummary,
    AIUsageSummary,
)
from mealie.schema.response import ErrorResponse

from .repository_generic import GroupRepositoryGeneric


def as_utc(value: datetime) -> datetime:
    """Returns `value` in UTC, reading a naive datetime as UTC"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def month_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The UTC calendar month containing `now` (default: the current time) as `(start, end)`, end exclusive"""
    start = as_utc(now or datetime.now(UTC)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def _empty_provider_summary(
    provider_id: UUID4 | None, name: str, model: str, monthly_token_limit: int | None = None
) -> AIUsageProviderSummary:
    return AIUsageProviderSummary(
        provider_id=provider_id,
        provider_name=name,
        model=model,
        requests=0,
        failures=0,
        prompt_tokens=0,
        completion_tokens=0,
        monthly_token_limit=monthly_token_limit,
        last_used_at=None,
    )


class GroupRepositoryAIProviderRoutes(GroupRepositoryGeneric[AIProviderRouteOut, AIProviderRoute]):
    """Per-slot ordered fallback providers for the repository's group"""

    def _settings_id(self) -> UUID4:
        if not self.group_id:
            raise ValueError("AI provider routes belong to a group; this repository isn't scoped to one")

        return self.session.execute(
            sa.select(AIProviderSettings.id).where(AIProviderSettings.group_id == self.group_id)
        ).scalar_one()

    def get_routes(self) -> dict[AIProviderSlot, list[UUID4]]:
        """Every slot's fallback provider ids, in order. Slots without routes map to an empty list."""
        routes: dict[AIProviderSlot, list[UUID4]] = {slot: [] for slot in AIProviderSlot}

        stmt = (
            sa.select(AIProviderRoute.slot, AIProviderRoute.provider_id)
            .where(AIProviderRoute.settings_id == self._settings_id())
            .order_by(AIProviderRoute.slot, AIProviderRoute.position)
        )
        for slot, provider_id in self.session.execute(stmt).all():
            if slot in routes:  # skip slots this version doesn't know
                routes[AIProviderSlot(slot)].append(provider_id)

        return routes

    def replace_routes(self, routes: Mapping[AIProviderSlot, Iterable[UUID4]]) -> dict[AIProviderSlot, list[UUID4]]:
        """
        Replaces the routes of every slot in `routes` (an empty list clears that slot) and leaves other
        slots alone. Duplicates within a slot are dropped, keeping the first.

        Raises a 400 `HTTPException` if any id isn't a provider of this group. Returns `get_routes()`.
        """
        settings_id = self._settings_id()
        new_routes = {AIProviderSlot(slot): list(dict.fromkeys(ids)) for slot, ids in routes.items()}
        if not new_routes:
            return self.get_routes()

        requested_ids = {provider_id for ids in new_routes.values() for provider_id in ids}
        if requested_ids:
            known_ids = set(
                self.session.execute(
                    sa.select(AIProvider.id).where(
                        AIProvider.settings_id == settings_id, AIProvider.id.in_(requested_ids)
                    )
                ).scalars()
            )
            if unknown_ids := requested_ids - known_ids:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=ErrorResponse.respond(
                        message=f"Unknown AI provider id(s): {', '.join(sorted(str(x) for x in unknown_ids))}"
                    ),
                )

        try:
            # A Core DELETE runs immediately, so the inserts below can't collide with the old positions
            # (the unit of work would otherwise flush the inserts first)
            self.session.execute(
                sa.delete(AIProviderRoute).where(
                    AIProviderRoute.settings_id == settings_id,
                    AIProviderRoute.slot.in_([slot.value for slot in new_routes]),
                )
            )
            for slot, provider_ids in new_routes.items():
                for position, provider_id in enumerate(provider_ids):
                    self.session.add(
                        AIProviderRoute(
                            session=self.session,
                            settings_id=settings_id,
                            slot=slot.value,
                            position=position,
                            provider_id=provider_id,
                        )
                    )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        return self.get_routes()


class GroupRepositoryAIUsage(GroupRepositoryGeneric[AIUsageLogOut, AIUsageLog]):
    """The AI usage log: one row per provider attempt"""

    def _scoped(self, *criteria: sa.ColumnElement[bool]) -> list[sa.ColumnElement[bool]]:
        if self.group_id:
            return [*criteria, AIUsageLog.group_id == self.group_id]
        return list(criteria)

    def create(self, data: AIUsageLogCreate | dict) -> AIUsageLogOut:  # type: ignore[override]
        """
        Logs one attempt. A group-scoped repository always logs it to its own group.

        `provider_id` is kept only if it's one of the group's saved providers; usage of an unsaved
        provider (e.g. a connection test) is logged without one.
        """
        data = dict(data) if isinstance(data, dict) else data.model_dump()
        if self.group_id:
            data["group_id"] = self.group_id
        elif not data.get("group_id"):
            raise ValueError("group_id is required to log AI usage outside a group-scoped repository")

        if provider_id := data.get("provider_id"):
            saved = self.session.execute(
                sa.select(AIProvider.id)
                .join(AIProviderSettings, AIProvider.settings_id == AIProviderSettings.id)
                .where(AIProvider.id == provider_id, AIProviderSettings.group_id == data["group_id"])
            ).scalar_one_or_none()
            if saved is None:
                data["provider_id"] = None

        return super().create(data)

    def monthly_tokens(self, provider_ids: Iterable[UUID4], now: datetime | None = None) -> dict[UUID4, int]:
        """
        Prompt + completion tokens used by each provider in the UTC calendar month containing `now`
        (default: the current month). Every requested id is in the result; unused providers map to 0.
        """
        ids = list(dict.fromkeys(provider_ids))
        if not ids:
            return {}

        start, end = month_range(now)
        stmt = (
            sa.select(
                AIUsageLog.provider_id,
                sa.func.coalesce(sa.func.sum(AIUsageLog.prompt_tokens + AIUsageLog.completion_tokens), 0),
            )
            .where(
                *self._scoped(
                    AIUsageLog.provider_id.in_(ids),
                    AIUsageLog.created_at >= start,
                    AIUsageLog.created_at < end,
                )
            )
            .group_by(AIUsageLog.provider_id)
        )

        totals: dict[UUID4, int] = dict.fromkeys(ids, 0)
        for provider_id, total in self.session.execute(stmt).all():
            totals[provider_id] = int(total)

        return totals

    def summary(self, start: datetime, end: datetime) -> AIUsageSummary:
        """Usage between `start` (inclusive) and `end` (exclusive), per provider and per UTC day"""
        start, end = as_utc(start), as_utc(end)
        in_range = self._scoped(AIUsageLog.created_at >= start, AIUsageLog.created_at < end)

        # Every current provider gets a row, even if it went unused
        providers_stmt = sa.select(
            AIProvider.id, AIProvider.name, AIProvider.model, AIProvider.monthly_token_limit
        ).join(AIProviderSettings, AIProvider.settings_id == AIProviderSettings.id)
        if self.group_id:
            providers_stmt = providers_stmt.where(AIProviderSettings.group_id == self.group_id)

        by_provider: dict[object, AIUsageProviderSummary] = {
            provider_id: _empty_provider_summary(provider_id, name, model, monthly_token_limit=limit)
            for provider_id, name, model, limit in self.session.execute(providers_stmt).all()
        }

        usage_stmt = (
            sa.select(
                AIUsageLog.provider_id,
                AIUsageLog.provider_name,
                AIUsageLog.model,
                sa.func.count(AIUsageLog.id),
                sa.func.coalesce(sa.func.sum(sa.case((AIUsageLog.success.is_(False), 1), else_=0)), 0),
                sa.func.coalesce(sa.func.sum(AIUsageLog.prompt_tokens), 0),
                sa.func.coalesce(sa.func.sum(AIUsageLog.completion_tokens), 0),
                sa.func.max(AIUsageLog.created_at),
            )
            .where(*in_range)
            .group_by(AIUsageLog.provider_id, AIUsageLog.provider_name, AIUsageLog.model)
        )
        for provider_id, name, model, requests, failures, prompt, completion, last_used in self.session.execute(
            usage_stmt
        ).all():
            # Rows of deleted providers have no id, so they're grouped by the name and model they were logged with
            key = provider_id or (name, model)
            entry = by_provider.get(key)
            if entry is None:
                entry = by_provider[key] = _empty_provider_summary(provider_id, name, model)

            entry.requests += int(requests)
            entry.failures += int(failures)
            entry.prompt_tokens += int(prompt)
            entry.completion_tokens += int(completion)
            if last_used is not None:
                last_used = as_utc(last_used)
                entry.last_used_at = max(entry.last_used_at, last_used) if entry.last_used_at else last_used

        day = sa.func.date(AIUsageLog.created_at, type_=sa.Date).label("day")
        days_stmt = (
            sa.select(
                day,
                sa.func.count(AIUsageLog.id),
                sa.func.coalesce(sa.func.sum(AIUsageLog.prompt_tokens), 0),
                sa.func.coalesce(sa.func.sum(AIUsageLog.completion_tokens), 0),
            )
            .where(*in_range)
            .group_by(day)
            .order_by(day)
        )
        by_day = [
            AIUsageDaySummary(
                date=value if isinstance(value, date) else date.fromisoformat(str(value)),
                requests=int(requests),
                prompt_tokens=int(prompt),
                completion_tokens=int(completion),
            )
            for value, requests, prompt, completion in self.session.execute(days_stmt).all()
        ]

        return AIUsageSummary(
            start=start,
            end=end,
            by_provider=sorted(
                by_provider.values(),
                key=lambda x: (x.provider_id is None, x.provider_name.casefold(), x.model.casefold()),
            ),
            by_day=by_day,
        )

    def purge_older_than(self, cutoff: datetime) -> int:
        """Deletes rows created before `cutoff`; returns how many"""
        try:
            result = cast(
                CursorResult,
                self.session.execute(sa.delete(AIUsageLog).where(*self._scoped(AIUsageLog.created_at < cutoff))),
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        return result.rowcount
