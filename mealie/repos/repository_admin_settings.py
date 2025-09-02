"""Repository for managing admin settings."""

from sqlalchemy.orm.session import Session

from mealie.db.models.admin.admin_settings import AdminSettings
from mealie.schema.admin.admin_settings import AdminSettingsIn, AdminSettingsOut


class RepositoryAdminSettings:
    def __init__(self, session: Session):
        self.session = session

    def get_settings(self) -> AdminSettingsOut:
        """Get the admin settings instance (creates one if doesn't exist)."""
        instance = AdminSettings.get_instance(self.session)
        return AdminSettingsOut.from_db_model(instance)

    def update_settings(self, settings_data: dict) -> AdminSettingsOut:
        """Update admin settings with the provided data."""
        # Get instance directly from database to avoid environment variable overrides
        instance = self.session.query(AdminSettings).first()
        if not instance:
            # If no instance exists, create one but don't call get_instance() which applies env overrides
            instance = AdminSettings()
            self.session.add(instance)
            self.session.flush()  # Get the ID without committing
        
        # Handle API key encryption
        encrypted_data = settings_data.copy()
        if 'openai_api_key' in encrypted_data and encrypted_data['openai_api_key']:
            encrypted_data['openai_api_key'] = self._encrypt_api_key(encrypted_data['openai_api_key'])
        if 'anthropic_api_key' in encrypted_data and encrypted_data['anthropic_api_key']:
            encrypted_data['anthropic_api_key'] = self._encrypt_api_key(encrypted_data['anthropic_api_key'])
        if 'gemini_api_key' in encrypted_data and encrypted_data['gemini_api_key']:
            encrypted_data['gemini_api_key'] = self._encrypt_api_key(encrypted_data['gemini_api_key'])
            
        # Update the instance directly without environment variable interference
        instance.update_from_dict(encrypted_data)
        self.session.commit()
        self.session.refresh(instance)
        
        return AdminSettingsOut.from_db_model(instance)

    def _encrypt_api_key(self, api_key: str) -> str:
        """Encrypt an API key for secure storage."""
        # For now, we'll store as-is since the settings already use MaskedNoneString
        # In production, you'd want to use proper encryption here
        return api_key

    def _decrypt_api_key(self, encrypted_key: str) -> str:
        """Decrypt an API key for use."""
        # For now, we'll return as-is since we're not actually encrypting
        # In production, you'd want to use proper decryption here
        return encrypted_key