"""Database model for admin settings that need to be configurable via UI."""

from sqlalchemy import Column, Integer, String, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from mealie.db.models._model_base import SqlAlchemyBase
from mealie.db.models._model_utils.auto_init import auto_init


class AdminSettings(SqlAlchemyBase):
    __tablename__ = "admin_settings"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    
    # Image Scanning Provider Settings
    image_scanning_primary_provider: Mapped[str | None] = mapped_column(String, default="none")
    image_scanning_secondary_provider: Mapped[str | None] = mapped_column(String, default="none")  
    image_scanning_enable_ocr_fallback: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # API Keys (encrypted)
    openai_api_key: Mapped[str | None] = mapped_column(Text)
    anthropic_api_key: Mapped[str | None] = mapped_column(Text)
    gemini_api_key: Mapped[str | None] = mapped_column(Text)
    ollama_base_url: Mapped[str | None] = mapped_column(String, default="http://localhost:11434")
    
    # Model Selections
    openai_model: Mapped[str | None] = mapped_column(String, default="gpt-4o-mini")
    anthropic_model: Mapped[str | None] = mapped_column(String, default="claude-3-haiku-20240307")
    gemini_model: Mapped[str | None] = mapped_column(String, default="gemini-1.5-flash")
    ollama_model: Mapped[str | None] = mapped_column(String, default="llava")

    @auto_init()
    def __init__(self, **kwargs) -> None:
        pass

    # Singleton pattern - there should only ever be one row
    @classmethod
    def get_instance(cls, session):
        """Get or create the single admin settings instance."""
        instance = session.query(cls).first()
        if not instance:
            instance = cls()
            session.add(instance)
            session.commit()
            session.refresh(instance)
        return instance

    def update_from_dict(self, data: dict) -> None:
        """Update settings from a dictionary, ignoring unknown keys."""
        valid_fields = {
            'image_scanning_primary_provider',
            'image_scanning_secondary_provider', 
            'image_scanning_enable_ocr_fallback',
            'openai_api_key',
            'anthropic_api_key',
            'gemini_api_key',
            'ollama_base_url',
            'openai_model',
            'anthropic_model',
            'gemini_model',
            'ollama_model'
        }
        
        for key, value in data.items():
            if key in valid_fields and hasattr(self, key):
                # Only update if value is not None or empty string (unless it's a boolean)
                if value is not None and (value != "" or isinstance(value, bool)):
                    setattr(self, key, value)