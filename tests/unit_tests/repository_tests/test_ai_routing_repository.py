from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_ai_routing import as_utc, month_range
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser


def _create_provider(user: TestUser) -> AIProviderOut:
    return user.repos.group_ai_providers.create(AIProviderCreate(name=random_string(), model="gpt-4o", api_key="key"))


def _usage(provider: AIProviderOut, prompt_tokens: int, completion_tokens: int = 0, **kwargs) -> AIUsageLogCreate:
    return AIUsageLogCreate(
        **{
            "provider_id": provider.id,
            "provider_name": provider.name,
            "model": provider.model,
            "protocol": provider.protocol,
            "slot": AIProviderSlot.default,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "success": True,
            **kwargs,
        }
    )


def test_month_range():
    assert month_range(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    # Naive times are read as UTC
    naive = datetime.fromisoformat("2026-02-01T00:00:00")
    assert month_range(naive) == (datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC))

    # Months are UTC months: this is still February locally, but March in UTC
    assert month_range(datetime.fromisoformat("2026-02-28T22:00:00-05:00"))[0] == datetime(2026, 3, 1, tzinfo=UTC)


def test_as_utc_reads_naive_as_utc():
    assert as_utc(datetime.fromisoformat("2026-01-01T12:00:00")) == datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert as_utc(datetime.fromisoformat("2026-01-01T12:00:00+02:00")) == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_usage_create_uses_the_repository_group(unique_user: TestUser, g2_user: TestUser):
    provider = _create_provider(unique_user)

    try:
        # Even when told otherwise, a group-scoped repository logs to its own group
        row = unique_user.repos.group_ai_usage.create(_usage(provider, 1, group_id=g2_user.group_id))
        assert str(row.group_id) == unique_user.group_id
        assert row.created_at is not None
    finally:
        unique_user.repos.group_ai_providers.delete(provider.id)


def test_usage_create_needs_a_group(session):
    repos = get_repositories(session, group_id=None, household_id=None)
    entry = AIUsageLogCreate(provider_name="x", model="m", protocol="openai", slot="default", success=True)

    with pytest.raises(ValueError):
        repos.group_ai_usage.create(entry)


def test_monthly_tokens(unique_user: TestUser, g2_user: TestUser):
    p1, p2, unused = (_create_provider(unique_user) for _ in range(3))
    usage = unique_user.repos.group_ai_usage
    now = datetime(2026, 5, 15, 12, tzinfo=UTC)

    try:
        for entry, created_at in [
            (_usage(p1, 10, 20), now),
            (_usage(p1, 5, 0, success=False, error_type="RateLimitError"), datetime(2026, 5, 1, tzinfo=UTC)),
            (_usage(p1, 1000, 1000), datetime(2026, 4, 30, 23, 59, 59, tzinfo=UTC)),
            (_usage(p1, 1000, 1000), datetime(2026, 6, 1, tzinfo=UTC)),
            (_usage(p2, 7, 7), datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)),
        ]:
            usage.create({**entry.model_dump(), "created_at": created_at})

        assert usage.monthly_tokens([p1.id, p2.id, unused.id], now) == {p1.id: 35, p2.id: 14, unused.id: 0}
        assert usage.monthly_tokens([p1.id], datetime(2026, 4, 2, tzinfo=UTC)) == {p1.id: 2000}
        assert usage.monthly_tokens([]) == {}

        # Another group can't see these providers' usage
        assert g2_user.repos.group_ai_usage.monthly_tokens([p1.id], now) == {p1.id: 0}
    finally:
        for provider in (p1, p2, unused):
            unique_user.repos.group_ai_providers.delete(provider.id)


def test_purge_older_than(unique_user: TestUser, g2_user: TestUser):
    provider = _create_provider(unique_user)
    g2_provider = _create_provider(g2_user)
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    try:
        for user, p in [(unique_user, provider), (g2_user, g2_provider)]:
            for created_at in [cutoff - timedelta(seconds=1), cutoff, cutoff + timedelta(days=1)]:
                user.repos.group_ai_usage.create({**_usage(p, 1).model_dump(), "created_at": created_at})

        # A group-scoped purge only touches its own group
        assert unique_user.repos.group_ai_usage.purge_older_than(cutoff) == 1
        assert g2_user.repos.group_ai_usage.monthly_tokens([g2_provider.id], cutoff - timedelta(seconds=1)) == {
            g2_provider.id: 1
        }
        assert unique_user.repos.group_ai_usage.monthly_tokens([provider.id], cutoff) == {provider.id: 2}
    finally:
        unique_user.repos.group_ai_providers.delete(provider.id)
        g2_user.repos.group_ai_providers.delete(g2_provider.id)


def test_replace_routes_validates_providers(unique_user: TestUser, g2_user: TestUser):
    own = _create_provider(unique_user)
    foreign = _create_provider(g2_user)
    routes = unique_user.repos.group_ai_provider_routes

    try:
        with pytest.raises(HTTPException) as e:
            routes.replace_routes({AIProviderSlot.fast: [own.id, foreign.id]})
        assert e.value.status_code == 400

        with pytest.raises(HTTPException):
            routes.replace_routes({AIProviderSlot.fast: [uuid4()]})

        assert routes.replace_routes({AIProviderSlot.fast: [own.id]})[AIProviderSlot.fast] == [own.id]
        assert routes.replace_routes({})[AIProviderSlot.fast] == [own.id]
    finally:
        unique_user.repos.group_ai_providers.delete(own.id)
        g2_user.repos.group_ai_providers.delete(foreign.id)


def test_routes_need_a_group(session):
    repos = get_repositories(session, group_id=None, household_id=None)

    with pytest.raises(ValueError):
        repos.group_ai_provider_routes.get_routes()
