"""
Unified image scanning service that implements provider fallback chain.
Primary -> Secondary -> OCR fallback
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mealie.core.config import get_app_settings
from mealie.lang.providers import Translator
# ADMIN SETTINGS INTEGRATION: Added to support checking AI provider configuration via admin panel
from mealie.repos.repository_admin_settings import RepositoryAdminSettings
from mealie.schema.recipe.recipe import CreateRecipe
from mealie.services.scraper import cleaner

logger = logging.getLogger(__name__)


class ImageScanningService:
    """Unified service for scanning recipe images with provider fallback"""
    
    def __init__(self, translator: Translator, session: Session | None = None):
        self.translator = translator
        self.settings = get_app_settings()
        # ADMIN SETTINGS INTEGRATION: Added session parameter and admin settings loading
        # This allows the service to check admin panel configuration instead of only environment variables
        self.session = session
        self._admin_settings = None
        if session:
            admin_settings_repo = RepositoryAdminSettings(session)
            self._admin_settings = admin_settings_repo.get_settings()
    
    async def scan_images_for_recipe(self, images: list[Path], translate_language: str | None = None) -> CreateRecipe:
        """
        Scan recipe images using the configured provider chain:
        1. Primary AI provider (if configured)
        2. Secondary AI provider (if configured and primary fails)
        3. OCR fallback (if enabled and all AI providers fail)
        
        Returns a CreateRecipe object with extracted recipe data.
        """
        errors = []
        
        # ADMIN SETTINGS INTEGRATION: Use helper methods to get provider settings
        # This allows checking admin settings first, then falling back to environment variables
        
        # Try primary provider
        primary_provider = self._get_primary_provider().lower()
        if primary_provider != "none":
            try:
                logger.info(f"Attempting recipe extraction with primary provider: {primary_provider}")
                recipe_data = await self._scan_with_provider(images, primary_provider, translate_language)
                if recipe_data:
                    return cleaner.clean(recipe_data, self.translator)
            except Exception as e:
                error_msg = f"Primary provider {primary_provider} failed: {str(e)}"
                logger.warning(error_msg)
                errors.append(error_msg)
        
        # Try secondary provider
        secondary_provider = self._get_secondary_provider().lower()
        if secondary_provider != "none" and secondary_provider != primary_provider:
            try:
                logger.info(f"Attempting recipe extraction with secondary provider: {secondary_provider}")
                recipe_data = await self._scan_with_provider(images, secondary_provider, translate_language)
                if recipe_data:
                    return cleaner.clean(recipe_data, self.translator)
            except Exception as e:
                error_msg = f"Secondary provider {secondary_provider} failed: {str(e)}"
                logger.warning(error_msg)
                errors.append(error_msg)
        
        # Try OCR fallback
        if self.get_ocr_fallback_enabled():
            try:
                logger.info("Attempting recipe extraction with OCR fallback")
                recipe_data = await self._scan_with_ocr(images)
                if recipe_data:
                    return cleaner.clean(recipe_data, self.translator)
            except Exception as e:
                error_msg = f"OCR fallback failed: {str(e)}"
                logger.warning(error_msg)
                errors.append(error_msg)
        
        # If all methods fail, raise an exception with all error details
        error_summary = f"All image scanning methods failed. Errors: {'; '.join(errors)}"
        raise Exception(error_summary)
    
    async def _scan_with_provider(self, images: list[Path], provider: str, translate_language: str | None = None) -> CreateRecipe | None:
        """Scan images with a specific AI provider"""
        if provider == "openai":
            return await self._scan_with_openai(images, translate_language)
        elif provider == "anthropic":
            return await self._scan_with_anthropic(images, translate_language)
        elif provider == "gemini":
            return await self._scan_with_gemini(images, translate_language)
        elif provider == "ollama":
            return await self._scan_with_ollama(images, translate_language)
        else:
            raise ValueError(f"Unknown provider: {provider}")
    
    async def _scan_with_openai(self, images: list[Path], translate_language: str | None = None) -> CreateRecipe | None:
        """Scan images using OpenAI"""
        if not self._is_provider_enabled("openai"):
            raise Exception("OpenAI is not configured")
        
        # Import here to avoid circular dependencies  
        from mealie.services.recipe.recipe_service import OpenAIRecipeService
        from mealie.repos.all_repositories import get_repositories
        from mealie.schema.user.user import PrivateUser
        from mealie.schema.household.household import HouseholdInDB
        
        # Create a minimal OpenAI recipe service instance
        # Note: This is a bit of a hack - we need repos/user/household to create the service
        # but we only need the recipe creation logic, not the database operations
        repos = get_repositories()  # This will need proper initialization
        
        # For now, let's use the direct OpenAI service approach
        from mealie.services.openai import OpenAIService, OpenAILocalImage, OpenAIDataInjection
        from mealie.schema.openai.recipe import OpenAIRecipe
        import os
        
        openai_service = OpenAIService()
        prompt = openai_service.get_prompt(
            "recipes.parse-recipe-image",
            data_injections=[
                OpenAIDataInjection(
                    description=(
                        "This is the JSON response schema. You must respond in valid JSON that follows this schema. "
                        "Your payload should be as compact as possible, eliminating unncessesary whitespace. "
                        "Any fields with default values which you do not populate should not be in the payload."
                    ),
                    value=OpenAIRecipe,
                )
            ],
        )

        openai_images = [OpenAILocalImage(filename=os.path.basename(image), path=image) for image in images]
        message = (
            f"Please extract the recipe from the {'images' if len(openai_images) > 1 else 'image'} provided."
            "There should be exactly one recipe."
        )

        if translate_language:
            message += f" Please translate the recipe to {translate_language}."

        response = await openai_service.chat_completion_with_images(
            prompt=prompt,
            message=message,
            images=openai_images,
        )
        
        openai_recipe = OpenAIRecipe.model_validate_json(response.choices[0].message.content)
        
        # Convert OpenAI recipe to CreateRecipe format
        from mealie.schema.recipe.recipe import CreateRecipe
        from mealie.schema.recipe.recipe_ingredient import RecipeIngredient  
        from mealie.schema.recipe.recipe_step import RecipeStep
        from mealie.schema.recipe.recipe_notes import RecipeNote
        
        return CreateRecipe(
            name=openai_recipe.name,
            description=openai_recipe.description,
            recipe_yield=openai_recipe.recipe_yield,
            total_time=openai_recipe.total_time,
            prep_time=openai_recipe.prep_time,
            perform_time=openai_recipe.perform_time,
            recipe_ingredient=[
                RecipeIngredient(title=ingredient.title, note=ingredient.text)
                for ingredient in openai_recipe.ingredients
                if ingredient.text
            ],
            recipe_instructions=[
                RecipeStep(title=instruction.title, text=instruction.text)
                for instruction in openai_recipe.instructions
                if instruction.text
            ],
            notes=[RecipeNote(title=note.title or "", text=note.text) for note in openai_recipe.notes if note.text],
        )
    
    async def _scan_with_anthropic(self, images: list[Path], translate_language: str | None = None) -> CreateRecipe | None:
        """Scan images using Anthropic Claude"""
        if not self._is_provider_enabled("anthropic"):
            raise Exception("Anthropic is not configured")
        
        # TODO: Implement Anthropic vision API integration
        raise NotImplementedError("Anthropic integration not yet implemented")
    
    async def _scan_with_gemini(self, images: list[Path], translate_language: str | None = None) -> CreateRecipe | None:
        """Scan images using Google Gemini"""
        if not self._is_provider_enabled("gemini"):
            raise Exception("Gemini is not configured")
        
        # TODO: Implement Gemini vision API integration
        raise NotImplementedError("Gemini integration not yet implemented")
    
    async def _scan_with_ollama(self, images: list[Path], translate_language: str | None = None) -> CreateRecipe | None:
        """Scan images using Ollama"""
        if not self._is_provider_enabled("ollama"):
            raise Exception("Ollama is not configured")
        
        # TODO: Implement Ollama vision API integration
        raise NotImplementedError("Ollama integration not yet implemented")
    
    async def _scan_with_ocr(self, images: list[Path]) -> CreateRecipe | None:
        """Scan images using traditional OCR"""
        try:
            from mealie.services.ocr import OCRService
        except ImportError:
            raise Exception("OCR services are not available. Please install pytesseract and pillow.")
        
        if not images:
            raise Exception("No images provided for OCR scanning")
        
        ocr_service = OCRService()
        # Use the first image for OCR scanning
        return ocr_service.process_image(images[0])
    
    def get_available_providers(self) -> dict[str, bool]:
        """Get a dict of available providers and their status"""
        return {
            "openai": self._is_provider_enabled("openai"),
            "anthropic": self._is_provider_enabled("anthropic"),
            "gemini": self._is_provider_enabled("gemini"),
            "ollama": self._is_provider_enabled("ollama"),
            "ocr": True,  # OCR is always available if tesseract is installed
        }
    
    def is_any_provider_configured(self) -> bool:
        """Check if any AI provider is configured"""
        return any([
            self._is_provider_enabled("openai"),
            self._is_provider_enabled("anthropic"),
            self._is_provider_enabled("gemini"),
            self._is_provider_enabled("ollama"),
        ])
    
    # ADMIN SETTINGS INTEGRATION: The following helper methods were added to support
    # admin panel configuration while maintaining backward compatibility with environment variables
    
    def _get_primary_provider(self) -> str:
        """Get the primary provider from admin settings or fallback to legacy settings"""
        if self._admin_settings:
            return self._admin_settings.image_scanning_primary_provider or "none"
        return self.settings.IMAGE_SCANNING_PRIMARY_PROVIDER
    
    def _get_secondary_provider(self) -> str:
        """Get the secondary provider from admin settings or fallback to legacy settings"""
        if self._admin_settings:
            return self._admin_settings.image_scanning_secondary_provider or "none"
        return self.settings.IMAGE_SCANNING_SECONDARY_PROVIDER
    
    def get_ocr_fallback_enabled(self) -> bool:
        """Get OCR fallback setting from admin settings or fallback to legacy settings
        PUBLIC METHOD: Used by recipe routes to check OCR availability
        """
        if self._admin_settings:
            return self._admin_settings.image_scanning_enable_ocr_fallback
        return self.settings.IMAGE_SCANNING_ENABLE_OCR_FALLBACK
    
    def _is_provider_enabled(self, provider: str) -> bool:
        """Check if a specific provider is enabled (has API key configured)
        CHANGED: Now checks admin settings first, then falls back to environment variables
        """
        # First check admin settings if available
        if self._admin_settings:
            if provider == "openai":
                return bool(self._admin_settings.openai_api_key_set)
            elif provider == "anthropic":
                return bool(self._admin_settings.anthropic_api_key_set)
            elif provider == "gemini":
                return bool(self._admin_settings.gemini_api_key_set)
            elif provider == "ollama":
                # Ollama doesn't need an API key, just check if base URL is configured
                return bool(self._admin_settings.ollama_base_url)
        
        # Fallback to legacy settings for backward compatibility
        if provider == "openai":
            return self.settings.OPENAI_ENABLED
        elif provider == "anthropic":
            return self.settings.ANTHROPIC_ENABLED
        elif provider == "gemini":
            return self.settings.GEMINI_ENABLED
        elif provider == "ollama":
            return self.settings.OLLAMA_ENABLED
        
        return False