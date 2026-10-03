import sqlalchemy as sa
from fastapi import HTTPException, status
from pydantic import UUID4

from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.group.ai_routing import AIProviderRoute, AIUsageLog
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut

from .repository_generic import GroupRepositoryGeneric


class GroupRepositoryAIProvider(GroupRepositoryGeneric[AIProviderOut, AIProvider]):
    def create(self, data: AIProviderCreate | dict):
        if isinstance(data, AIProviderCreate):
            api_key = data.api_key
            data = data.model_dump()
            data["api_key"] = api_key

        if not data.get("api_key"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="API key cannot be empty")

        if not data.get("settings_id"):
            data["settings_id"] = self.session.execute(
                sa.select(AIProviderSettings.id).where(AIProviderSettings.group_id == self.group_id)
            ).scalar_one()

        return super().create(data)

    def update(self, match_value: str | int | UUID4, new_data: AIProviderCreate | dict):
        if isinstance(new_data, AIProviderCreate):
            # api_key is excluded from the schema's dump, so carry it over by hand, the same
            # way create does. Without this every update looks like it omitted the key.
            api_key = new_data.api_key
            new_data = new_data.model_dump()
            new_data["api_key"] = api_key

        # No key given: leave the stored (encrypted) one untouched. Copying the decrypted value back
        # would replace a key that can't currently be decrypted (e.g. after `.secret` changed) with "".
        if not new_data.get("api_key"):
            new_data.pop("api_key", None)

        return super().update(match_value, new_data)

    def delete(self, value, match_key: str | None = None) -> AIProviderOut:
        # Null out any settings references to this provider before deleting
        self.session.execute(
            sa.update(AIProviderSettings)
            .where(AIProviderSettings.default_provider_id == value)
            .values(default_provider_id=None)
        )
        self.session.execute(
            sa.update(AIProviderSettings)
            .where(AIProviderSettings.audio_provider_id == value)
            .values(audio_provider_id=None)
        )
        self.session.execute(
            sa.update(AIProviderSettings)
            .where(AIProviderSettings.image_provider_id == value)
            .values(image_provider_id=None)
        )

        # Drop the provider's fallback routes, and keep its usage history without the reference
        routes = sa.delete(AIProviderRoute).where(AIProviderRoute.provider_id == value)
        usage = sa.update(AIUsageLog).where(AIUsageLog.provider_id == value).values(provider_id=None)
        if self.group_id:
            group_settings = sa.select(AIProviderSettings.id).where(AIProviderSettings.group_id == self.group_id)
            routes = routes.where(AIProviderRoute.settings_id.in_(group_settings))
            usage = usage.where(AIUsageLog.group_id == self.group_id)
        self.session.execute(routes)
        self.session.execute(usage)

        # Delete
        return super().delete(value, match_key)

    def update_many(self, data):
        raise NotImplementedError
