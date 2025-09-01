from fastapi import APIRouter, Depends
from recipe_scrapers import __version__ as recipe_scraper_version
from sqlalchemy.orm import Session

from mealie.core.release_checker import get_latest_version
from mealie.core.settings.static import APP_VERSION
from mealie.db.db_setup import generate_session
from mealie.routes._base import BaseAdminController, controller
from mealie.schema.admin.about import AdminAboutInfo, AppStatistics, CheckAppConfig

router = APIRouter(prefix="/about")


@controller(router)
class AdminAboutController(BaseAdminController):
    @router.get("", response_model=AdminAboutInfo)
    def get_app_info(self):
        """Get general application information"""

        settings = self.settings

        return AdminAboutInfo(
            production=settings.PRODUCTION,
            version=APP_VERSION,
            versionLatest=get_latest_version(),
            demo_status=settings.IS_DEMO,
            api_port=settings.API_PORT,
            api_docs=settings.API_DOCS,
            db_type=settings.DB_ENGINE,
            db_url=settings.DB_URL_PUBLIC,
            default_group=settings.DEFAULT_GROUP,
            default_household=settings.DEFAULT_HOUSEHOLD,
            allow_signup=settings.ALLOW_SIGNUP,
            allow_password_login=settings.ALLOW_PASSWORD_LOGIN,
            build_id=settings.GIT_COMMIT_HASH,
            recipe_scraper_version=recipe_scraper_version.__version__,
            enable_oidc=settings.OIDC_AUTH_ENABLED,
            oidc_redirect=settings.OIDC_AUTO_REDIRECT,
            oidc_provider_name=settings.OIDC_PROVIDER_NAME,
            enable_openai=settings.OPENAI_ENABLED,
            enable_openai_image_services=settings.OPENAI_ENABLED and settings.OPENAI_ENABLE_IMAGE_SERVICES,
        )

    @router.get("/statistics", response_model=AppStatistics)
    def get_app_statistics(self):
        return AppStatistics(
            total_recipes=self.repos.recipes.count_all(),
            uncategorized_recipes=self.repos.recipes.count_uncategorized(),  # type: ignore
            untagged_recipes=self.repos.recipes.count_untagged(),  # type: ignore
            total_users=self.repos.users.count_all(),
            total_households=self.repos.households.count_all(),
            total_groups=self.repos.groups.count_all(),
        )

    @router.get("/check", response_model=CheckAppConfig)
    def check_app_config(self, session: Session = Depends(generate_session)):
        settings = self.settings

        # ADMIN SETTINGS INTEGRATION: Check if any AI provider is configured 
        # This matches the logic from app_about.py to provide consistent status
        ai_image_services_enabled = False
        try:
            from mealie.repos.repository_admin_settings import RepositoryAdminSettings
            admin_settings_repo = RepositoryAdminSettings(session)
            admin_settings = admin_settings_repo.get_settings()
            
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
                    ai_image_services_enabled = True
        except Exception:
            # Fallback to legacy settings if admin settings fail to load
            ai_image_services_enabled = settings.OPENAI_ENABLED and settings.OPENAI_ENABLE_IMAGE_SERVICES

        return CheckAppConfig(
            email_ready=settings.SMTP_ENABLE,
            ldap_ready=settings.LDAP_ENABLED,
            base_url_set=settings.BASE_URL != "http://localhost:8080",
            is_up_to_date=APP_VERSION == "develop" or APP_VERSION == "nightly" or get_latest_version() == APP_VERSION,
            oidc_ready=settings.OIDC_READY,
            enable_openai=settings.OPENAI_ENABLED,  # Legacy field
            ai_image_services_enabled=ai_image_services_enabled,  # NEW: Unified AI provider status
        )
