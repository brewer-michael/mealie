"""
Fork-owned AI provider routes: the managers' provider list, per-slot fallback routes, the usage summary
and model lists (docs/ai/PHASE1.md). Kept apart from upstream's `controller_group_ai_providers.py` to
avoid sync conflicts.
"""

import re
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import UUID4

from mealie.repos.repository_ai_routing import as_utc, month_range
from mealie.routes._base import controller
from mealie.routes._base.base_controllers import BaseUserController
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, check_base_url
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

MODEL_ID_PATTERN = re.compile(r"[\w.:/@+-]{1,128}")
MODEL_NAME_PATTERN = re.compile(r"[\w .:/@+()-]{1,128}")
MAX_MODELS = 500
"""
Model lists come from whatever host `base_url` names, so only model-like ids and names (and at most
`MAX_MODELS` of them) are passed on: the endpoint mustn't relay arbitrary data from internal hosts.
"""

_CONNECTION_FIELDS = ("protocol", "base_url", "timeout", "request_headers", "request_params")
"""The `AIProviderModelsQuery` fields a saved provider's model list can override"""


def require_api_key_for_new_destination(saved: AIProviderOut, edited: AIProviderCreate, api_key: str) -> None:
    """
    Unsaved edits to a saved provider may reuse its stored key (a blank `api_key`) only while the key
    still goes where it was saved for. Raises a 400 asking for the key again if `edited` changes the
    protocol, base URL or request headers.
    """
    if api_key:
        return

    if (edited.protocol, edited.base_url, edited.request_headers) != (
        saved.protocol,
        saved.base_url,
        saved.request_headers,
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse.respond(
                message="Enter the API key again to use it with a different API type, base URL or request headers."
            ),
        )


def safe_models(models: list[AIProviderModelInfo]) -> list[AIProviderModelInfo]:
    """The first `MAX_MODELS` models with a model-like id; display names that don't look like one are dropped"""
    safe: list[AIProviderModelInfo] = []
    for model in models:
        if not MODEL_ID_PATTERN.fullmatch(model.id):
            continue

        if model.display_name and not MODEL_NAME_PATTERN.fullmatch(model.display_name):
            model = model.model_copy(update={"display_name": None})

        safe.append(model)
        if len(safe) >= MAX_MODELS:
            break

    return safe


@controller(router)
class GroupAIProviderRoutingController(BaseUserController):
    # ==========================================
    # Providers

    @router.get("/providers", response_model=list[AIProviderOut])
    def get_ai_providers(self) -> list[AIProviderOut]:
        """
        The group's providers, by name, without their API keys. `apiKeySet` is false for a key that can't
        be read (e.g. after `.secret` changed). Unlike the provider list in `/groups/self`, this is for
        group managers only, so it can say so.
        """
        self.checks.can_manage()

        return sorted(self.repos.group_ai_providers.get_all(), key=lambda provider: provider.name.casefold())

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
            # A provider saved before base URLs were checked may still have a query string
            check_base_url(provider.base_url)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=ErrorResponse.respond(message=str(e))) from None

        try:
            return safe_models(await OpenAIService(self.repos).list_models(provider))
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
        stored. Fields left out keep their saved values. A blank `apiKey` keeps the saved key, but only
        while the protocol, base URL and request headers are unchanged; otherwise it's a 400.
        """
        self.checks.can_manage()

        provider = self.repos.group_ai_providers.get_one(provider_id)
        if not provider:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=ErrorResponse.respond(message="Not found."))

        if overrides:
            changes = {
                name: getattr(overrides, name) for name in _CONNECTION_FIELDS if name in overrides.model_fields_set
            }
            edited = provider.model_copy(
                update={**changes, **({"api_key": overrides.api_key} if overrides.api_key else {})}
            )
            require_api_key_for_new_destination(provider, edited, overrides.api_key)
            provider = edited

        return await self._list_models(provider)
