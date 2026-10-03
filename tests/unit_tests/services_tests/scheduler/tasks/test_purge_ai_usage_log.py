from datetime import UTC, datetime, timedelta

from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.services.scheduler.tasks.purge_ai_usage_log import AI_USAGE_RETENTION_DAYS, purge_ai_usage_log
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser


def test_purge_with_nothing_to_purge():
    purge_ai_usage_log()


def test_purge_removes_rows_past_retention(unique_user: TestUser, g2_user: TestUser):
    assert AI_USAGE_RETENTION_DAYS == 400

    now = datetime.now(UTC)
    providers = []
    try:
        for user in (unique_user, g2_user):
            provider = user.repos.group_ai_providers.create(
                AIProviderCreate(name=random_string(), model="gpt-4o", api_key="key")
            )
            providers.append((user, provider))
            for age, tokens in [(timedelta(days=401), 1), (timedelta(days=399), 10), (timedelta(0), 100)]:
                entry = AIUsageLogCreate(
                    provider_id=provider.id,
                    provider_name=provider.name,
                    model=provider.model,
                    protocol=provider.protocol,
                    slot=AIProviderSlot.default,
                    prompt_tokens=tokens,
                    success=True,
                )
                user.repos.group_ai_usage.create({**entry.model_dump(), "created_at": now - age})

        purge_ai_usage_log()

        # Every group's old rows are gone; the rest are kept
        for user, provider in providers:
            user.repos.session.expire_all()
            summary = user.repos.group_ai_usage.summary(now - timedelta(days=500), now + timedelta(days=1))
            row = next(x for x in summary.by_provider if x.provider_id == provider.id)
            assert (row.requests, row.prompt_tokens) == (2, 110)
    finally:
        for user, provider in providers:
            user.repos.group_ai_providers.delete(provider.id)
