"""
Fork-owned AI provider routes: per-slot fallback routes, the usage summary and model lists
(docs/ai/PHASE1.md). Kept apart from upstream's `controller_group_ai_providers.py` to avoid sync conflicts.
"""

from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import UUID4

from mealie.repos.repository_ai_routing import as_utc, month_range
from mealie.routes._base import controller
from mealie.routes._base.base_controllers import BaseUserController
from mealie.schema.group.ai_providers import AIProviderOut
from mealie.schema.group.ai_routing import (
    AIProviderModelInfo,
    AIProviderModelsQuery,
    AIProviderRoutesOut,
    AIProviderRoutesUpdate,
    AIUsageSummary,
)
from mealie.schema.response import ErrorResponse
from mealie.services.ai.errors import describe_provider_error
from mealie.services.openai import OpenAIService

router = APIRouter(prefix="/groups/ai-providers", tags=["Groups: AI Provider Routing"])


@controller(router)
class GroupAIProviderRoutingController(BaseUserController):
    # ==========================================
    # Fallback routes

    @router.get("/routes", response_model=AIProviderRoutesOut)
    def get_ai_provider_routes(self) -> AIProviderRoutesOut:
        """Every slot's ordered fallback providers (an empty list for a slot without any)"""
        self.checks.can_manage()

        return AIProviderRoutesOut(routes=self.repos.group_ai_provider_routes.get_routes())

    @router.put("/routes", response_model=AIProviderRoutesOut)
    def update_ai_provider_routes(self, data: AIProviderRoutesUpdate) -> AIProviderRoutesOut:
        """
        Replaces the fallback providers of every slot included (an empty list clears a slot); other
        slots are left alone. Duplicates within a slot are dropped. Providers must belong to this group.
        """
        self.checks.can_manage()

        return AIProviderRoutesOut(routes=self.repos.group_ai_provider_routes.replace_routes(data.routes))

    # ==========================================
    # Usage

    @router.get("/usage", response_model=AIUsageSummary)
    def get_ai_usage(self, start: datetime | None = None, end: datetime | None = None) -> AIUsageSummary:
        """
        AI usage from `start` (inclusive) to `end` (exclusive), per provider and per UTC day. Times
        without a timezone are read as UTC.

        Without either bound, this is the current UTC calendar month; with only one, it's the calendar
        month starting at `start`, or ending at `end`.
        """
        self.checks.can_manage()

        if start is None:
            start = month_range(as_utc(end) - timedelta(microseconds=1))[0] if end else month_range()[0]
        if end is None:
            end = month_range(start)[1]

        start, end = as_utc(start), as_utc(end)
        if start >= end:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=ErrorResponse.respond(message="start must be before end")
            )

        return self.repos.group_ai_usage.summary(start, end)

    # ==========================================
    # Model lists

    async def _list_models(self, provider: AIProviderOut) -> list[AIProviderModelInfo]:
        try:
            return await OpenAIService(self.repos).list_models(provider)
        except Exception as e:
            # Only the error's type and status reach the caller (see `describe_provider_error`)
            self.logger.exception("Listing AI provider models failed")
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=ErrorResponse.respond(message=describe_provider_error(e))
            ) from None

    @router.post("/providers/models", response_model=list[AIProviderModelInfo])
    async def list_ai_provider_models(self, data: AIProviderModelsQuery) -> list[AIProviderModelInfo]:
        """List the models an unsaved provider configuration offers"""
        self.checks.can_manage()

        if not data.api_key:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=ErrorResponse.respond(message="API key cannot be empty")
            )

        # Ephemeral provider, never persisted; the name and model aren't needed to list models
        provider = AIProviderOut(
            id=uuid4(),
            name="unsaved",
            model="unset",
            api_key=data.api_key,
            base_url=data.base_url,
            timeout=data.timeout,
            protocol=data.protocol,
            request_headers=data.request_headers,
            request_params=data.request_params,
        )
        return await self._list_models(provider)

    @router.post("/providers/{provider_id}/models", response_model=list[AIProviderModelInfo])
    async def list_saved_ai_provider_models(
        self, provider_id: UUID4, overrides: AIProviderModelsQuery | None = None
    ) -> list[AIProviderModelInfo]:
        """
        List the models a saved provider offers, with its saved API key.

        Like the saved-provider connection test, this accepts unsaved edits to use instead of what's
        stored; a blank `apiKey` keeps the saved key.
        """
        self.checks.can_manage()

        provider = self.repos.group_ai_providers.get_one(provider_id)
        if not provider:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=ErrorResponse.respond(message="Not found."))

        if overrides:
            provider = provider.model_copy(
                update={
                    "protocol": overrides.protocol,
                    "base_url": overrides.base_url,
                    "timeout": overrides.timeout,
                    "request_headers": overrides.request_headers,
                    "request_params": overrides.request_params,
                    **({"api_key": overrides.api_key} if overrides.api_key else {}),
                }
            )
        return await self._list_models(provider)
