"""Fork-owned schemas for AI provider fallback routes, the usage log and model lists (docs/ai/PHASE1.md)"""

import datetime as dt
from typing import Any

from pydantic import UUID4, ConfigDict, Field, field_validator

from mealie.schema._mealie import MealieModel

from .ai_providers import AIProviderProtocol, AIProviderSlot, check_base_url

# ==========================================
# Fallback routes


class AIProviderRouteOut(MealieModel):
    """One entry in a slot's ordered fallback list"""

    id: UUID4
    settings_id: UUID4
    slot: AIProviderSlot
    position: int
    provider_id: UUID4

    model_config = ConfigDict(from_attributes=True)


class AIProviderRoutesUpdate(MealieModel):
    """
    Ordered fallback providers per slot. Only the slots included are replaced; an empty list clears a
    slot. Duplicate ids within a slot are dropped, keeping the first.
    """

    routes: dict[AIProviderSlot, list[UUID4]] = {}


class AIProviderRoutesOut(MealieModel):
    """Ordered fallback providers for every slot (an empty list when a slot has none)"""

    routes: dict[AIProviderSlot, list[UUID4]]


# ==========================================
# Usage log


class AIUsageLogCreate(MealieModel):
    """One provider attempt. `group_id` defaults to the repository's group."""

    group_id: UUID4 | None = None
    provider_id: UUID4 | None = None
    provider_name: str
    model: str
    protocol: AIProviderProtocol
    slot: AIProviderSlot
    feature: str | None = None
    """The response schema's class name, e.g. `OpenAIRecipe`"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    success: bool
    error_type: str | None = None
    """The exception's class name, if the attempt failed"""


class AIUsageLogOut(AIUsageLogCreate):
    id: UUID4
    group_id: UUID4
    created_at: dt.datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AIUsageProviderSummary(MealieModel):
    """
    Usage of one provider over the summary's range.

    Every current provider of the group has a row, even when unused. Usage by deleted providers is
    reported with `provider_id` unset, one row per provider name and model.
    """

    provider_id: UUID4 | None
    provider_name: str
    model: str
    requests: int
    failures: int
    prompt_tokens: int
    completion_tokens: int
    monthly_token_limit: int | None
    last_used_at: dt.datetime | None


class AIUsageDaySummary(MealieModel):
    date: dt.date
    """The UTC day"""

    requests: int
    prompt_tokens: int
    completion_tokens: int


class AIUsageSummary(MealieModel):
    """AI usage between `start` (inclusive) and `end` (exclusive)"""

    start: dt.datetime
    end: dt.datetime
    by_provider: list[AIUsageProviderSummary]
    by_day: list[AIUsageDaySummary]


# ==========================================
# Model lists


class AIProviderModelsQuery(MealieModel):
    """
    Connection details used to list a provider's models. Only these fields are needed, so the name
    and model can still be blank; a full `AIProviderCreate` body is accepted too.

    When listing a saved provider's models, a blank `api_key` means "use the saved key".
    """

    protocol: AIProviderProtocol = AIProviderProtocol.openai
    base_url: str | None = None
    api_key: str = Field("", exclude=True)
    timeout: int = Field(300, ge=0)

    request_headers: dict[str, str] = {}
    request_params: dict[str, str] = {}

    @field_validator("base_url", mode="before")
    def validate_as_none(val: Any | None) -> Any | None:
        return val or None

    @field_validator("base_url")
    def validate_base_url(val: str | None) -> str | None:
        return check_base_url(val)


class AIProviderModelInfo(MealieModel):
    id: str
    display_name: str | None
    supports_images: bool | None
    """Whether the model reads images; `None` when the provider doesn't say"""
