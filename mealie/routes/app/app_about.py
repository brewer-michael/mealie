from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm.session import Session

from mealie.core.config import get_app_settings
from mealie.core.settings.static import APP_VERSION
from mealie.db.db_setup import generate_session
from mealie.db.models.users.users import User
from mealie.repos.all_repositories import get_repositories
# Added for admin settings integration - allows checking AI provider configuration via admin panel
from mealie.repos.repository_admin_settings import RepositoryAdminSettings
from mealie.schema.admin.about import AppInfo, AppStartupInfo, AppTheme

router = APIRouter(prefix="/about")


@router.get("", response_model=AppInfo)
def get_app_info(session: Session = Depends(generate_session)):
    """Get general application information"""
    settings = get_app_settings()

    public_repos = get_repositories(session, group_id=None, household_id=None)

    default_group_slug: str | None = None
    default_household_slug: str | None = None

    default_group = public_repos.groups.get_by_name(settings.DEFAULT_GROUP)
    if default_group and default_group.preferences and not default_group.preferences.private_group:
        default_group_slug = default_group.slug

    if default_group and default_group_slug:
        group_repos = get_repositories(session, group_id=default_group.id, household_id=None)
        default_household = group_repos.households.get_by_name(settings.DEFAULT_HOUSEHOLD)
        if default_household and default_household.preferences and not default_household.preferences.private_household:
            default_household_slug = default_household.slug

    # ADMIN SETTINGS INTEGRATION: Check admin settings for AI provider configuration
    # This replaces the legacy OPENAI_ENABLE_IMAGE_SERVICES check to support multiple AI providers
    # configured via the admin panel instead of environment variables only
    ai_image_services_enabled = False
    try:
        admin_settings_repo = RepositoryAdminSettings(session)
        admin_settings = admin_settings_repo.get_settings()
        
        # DEBUG: Check what we actually have in admin settings
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"DEBUG: admin_settings exists: {admin_settings is not None}")
        if admin_settings:
            logger.info(f"DEBUG: image_scanning_primary_provider: {admin_settings.image_scanning_primary_provider}")
            logger.info(f"DEBUG: openai_api_key_set: {admin_settings.openai_api_key_set}")
            logger.info(f"DEBUG: gemini_api_key_set: {admin_settings.gemini_api_key_set}")
            logger.info(f"DEBUG: anthropic_api_key_set: {admin_settings.anthropic_api_key_set}")
            logger.info(f"DEBUG: ollama_base_url: {admin_settings.ollama_base_url}")
        
        # AI image services are enabled if a provider is configured with a valid API key
        # This allows the frontend to show/hide image scanning features based on admin configuration
        if admin_settings and admin_settings.image_scanning_primary_provider and admin_settings.image_scanning_primary_provider != 'none':
            provider = admin_settings.image_scanning_primary_provider
            # Check if the selected provider has an API key/URL configured
            if provider == 'openai' and admin_settings.openai_api_key_set:
                ai_image_services_enabled = True
            elif provider == 'gemini' and admin_settings.gemini_api_key_set:
                ai_image_services_enabled = True
            elif provider == 'anthropic' and admin_settings.anthropic_api_key_set:
                ai_image_services_enabled = True
            elif provider == 'ollama' and admin_settings.ollama_base_url and admin_settings.ollama_base_url != "http://localhost:11434":
                # Only enable if Ollama URL is customized (not the default)
                ai_image_services_enabled = True
        
        logger.info(f"DEBUG: ai_image_services_enabled final value: {ai_image_services_enabled}")
        
    except Exception as e:
        # Fallback to legacy settings if admin settings fail to load
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"DEBUG: Exception loading admin settings: {str(e)}")
        ai_image_services_enabled = settings.OPENAI_ENABLED and settings.OPENAI_ENABLE_IMAGE_SERVICES

    return AppInfo(
        version=APP_VERSION,
        demo_status=settings.IS_DEMO,
        production=settings.PRODUCTION,
        allow_signup=settings.ALLOW_SIGNUP,
        default_group_slug=default_group_slug,
        default_household_slug=default_household_slug,
        enable_oidc=settings.OIDC_READY,
        oidc_redirect=settings.OIDC_AUTO_REDIRECT,
        oidc_provider_name=settings.OIDC_PROVIDER_NAME,
        enable_openai=settings.OPENAI_ENABLED,  # Keep legacy setting for backward compatibility
        ai_image_services_enabled=ai_image_services_enabled,  # CHANGED: Now uses admin settings for any AI provider instead of legacy OPENAI_ENABLE_IMAGE_SERVICES
        allow_password_login=settings.ALLOW_PASSWORD_LOGIN,
    )


@router.get("/startup-info", response_model=AppStartupInfo)
def get_startup_info(session: Session = Depends(generate_session)):
    """returns helpful startup information"""
    settings = get_app_settings()

    is_first_login = False
    with session as db:
        if db.query(User).filter_by(email=settings._DEFAULT_EMAIL).count() > 0:
            is_first_login = True

    return AppStartupInfo(
        is_first_login=is_first_login,
        is_demo=settings.IS_DEMO,
    )


@router.get("/theme", response_model=AppTheme)
def get_app_theme(resp: Response):
    """Get's the current theme settings"""
    settings = get_app_settings()

    resp.headers["Cache-Control"] = "public, max-age=604800"
    return AppTheme(**settings.theme.model_dump())
