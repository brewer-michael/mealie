"""Schema for admin settings API."""

from pydantic import BaseModel, Field


class AdminSettingsBase(BaseModel):
    """Base schema for admin settings."""
    
    # Image Scanning Provider Settings
    image_scanning_primary_provider: str | None = Field(default="none", description="Primary AI provider for image scanning")
    image_scanning_secondary_provider: str | None = Field(default="none", description="Secondary AI provider for fallback")
    image_scanning_enable_ocr_fallback: bool = Field(default=True, description="Enable OCR as final fallback method")
    
    # Model Selections
    openai_model: str | None = Field(default="gpt-4o-mini", description="OpenAI model to use")
    anthropic_model: str | None = Field(default="claude-3-haiku-20240307", description="Anthropic model to use")
    gemini_model: str | None = Field(default="gemini-1.5-flash", description="Gemini model to use")
    ollama_model: str | None = Field(default="llava", description="Ollama model to use")
    ollama_base_url: str | None = Field(default="http://localhost:11434", description="Ollama base URL")


class AdminSettingsIn(AdminSettingsBase):
    """Schema for updating admin settings (includes sensitive fields)."""
    
    # API Keys (input only)
    openai_api_key: str | None = Field(default=None, description="OpenAI API Key")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API Key")
    gemini_api_key: str | None = Field(default=None, description="Google Gemini API Key")


class AdminSettingsOut(AdminSettingsBase):
    """Schema for returning admin settings (excludes raw API keys)."""
    
    id: int = Field(description="Settings record ID")
    
    # API Key Status (masked for security)
    openai_api_key_set: bool = Field(default=False, description="Whether OpenAI API key is configured")
    anthropic_api_key_set: bool = Field(default=False, description="Whether Anthropic API key is configured")
    gemini_api_key_set: bool = Field(default=False, description="Whether Gemini API key is configured")
    
    @classmethod
    def from_db_model(cls, db_model) -> "AdminSettingsOut":
        """Convert database model to output schema."""
        return cls(
            id=db_model.id,
            image_scanning_primary_provider=db_model.image_scanning_primary_provider,
            image_scanning_secondary_provider=db_model.image_scanning_secondary_provider,
            image_scanning_enable_ocr_fallback=db_model.image_scanning_enable_ocr_fallback,
            openai_model=db_model.openai_model,
            anthropic_model=db_model.anthropic_model,
            gemini_model=db_model.gemini_model,
            ollama_model=db_model.ollama_model,
            ollama_base_url=db_model.ollama_base_url,
            openai_api_key_set=bool(db_model.openai_api_key),
            anthropic_api_key_set=bool(db_model.anthropic_api_key),
            gemini_api_key_set=bool(db_model.gemini_api_key),
        )

    class Config:
        from_attributes = True