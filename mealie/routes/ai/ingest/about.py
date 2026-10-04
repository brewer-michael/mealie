"""
`GET /api/ai/about` (docs/ai/PHASE2.md §14, plan §10): public. What this server's AI features accept, so an iOS
Shortcut, Home Assistant or a script can check before uploading. Nothing in it is about a group or a user.
"""

from fastapi import APIRouter

from mealie.core.settings.static import APP_VERSION
from mealie.schema.recipe_ingest import IngestAbout, IngestAboutFeature, IngestAboutFeatures
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.settings import get_ingest_settings, inbox_root

router = APIRouter(prefix="/ai/about", tags=["AI: Recipe Cards"])


@router.get("", response_model=IngestAbout)
def get_ai_about() -> IngestAbout:
    """The server's version and its AI features: whether recipe card scanning is on, and its upload limits"""
    settings = get_ingest_settings()
    return IngestAbout(
        version=APP_VERSION,
        features=IngestAboutFeatures(
            ingest=IngestAboutFeature(
                enabled=settings.ENABLED,
                max_upload_bytes=settings.max_upload_bytes,
                max_images_per_request=limits.MAX_IMAGES_PER_REQUEST,
                max_pages_per_card=limits.MAX_PAGES_PER_CARD,
                inbox=settings.ENABLED and inbox_root() is not None,
            ),
            mcp=True,
        ),
    )
