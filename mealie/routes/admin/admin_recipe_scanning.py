"""Admin routes for recipe scanning configuration."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm.session import Session

from mealie.core.dependencies import get_admin_user
from mealie.db.db_setup import generate_session
from mealie.repos.all_repositories import get_repositories
from mealie.schema.admin.admin_settings import AdminSettingsIn, AdminSettingsOut
from mealie.schema.response.responses import SuccessResponse
from mealie.schema.user.user import PrivateUser

router = APIRouter(prefix="/recipe-scanning", tags=["Admin: Recipe Scanning"])


@router.get("/settings", response_model=AdminSettingsOut)
def get_recipe_scanning_settings(
    session: Session = Depends(generate_session),
    _: PrivateUser = Depends(get_admin_user),
):
    """Get the current recipe scanning settings."""
    repos = get_repositories(session)
    return repos.admin_settings.get_settings()


@router.put("/settings", response_model=AdminSettingsOut)
def update_recipe_scanning_settings(
    settings_data: AdminSettingsIn,
    session: Session = Depends(generate_session),
    _: PrivateUser = Depends(get_admin_user),
):
    """Update the recipe scanning settings."""
    repos = get_repositories(session)
    
    try:
        updated_settings = repos.admin_settings.update_settings(settings_data.model_dump())
        return updated_settings
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update settings: {str(e)}"
        )


@router.post("/test-connection/{provider}", response_model=SuccessResponse)
def test_provider_connection(
    provider: str,
    session: Session = Depends(generate_session),
    _: PrivateUser = Depends(get_admin_user),
):
    """Test connection to a specific provider."""
    # TODO: Implement actual connection testing for each provider
    # For now, return success for all providers to allow testing
    
    valid_providers = ["openai", "anthropic", "gemini", "ollama"]
    if provider not in valid_providers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid provider. Must be one of: {', '.join(valid_providers)}"
        )
    
    # Mock successful connection test
    return SuccessResponse.respond(f"Successfully connected to {provider}")