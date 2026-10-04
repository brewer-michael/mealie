"""
`GET /api/ai/about` (docs/ai/PHASE2.md §14, plan §10): public. What this server's AI features accept, so an iOS
Shortcut, Home Assistant or a script can check before uploading. Nothing in it is about a group or a user.

`features.ingest.worker` says whether a card reader runs: some process's dispatcher marked itself running within the
last `READER_SEEN_WITHIN` seconds (`storage.dispatcher_seen_at`). With `AI_INGEST_WORKER=false` everywhere, uploads
are still accepted but nothing reads them.
"""

import time

from fastapi import APIRouter

from mealie.core.settings.static import APP_VERSION
from mealie.schema.recipe_ingest import IngestAbout, IngestAboutFeature, IngestAboutFeatures
from mealie.services.ai.ingest import limits, storage
from mealie.services.ai.ingest.settings import get_ingest_settings, inbox_root

router = APIRouter(prefix="/ai/about", tags=["AI: Recipe Cards"])

READER_SEEN_WITHIN = 3 * limits.DISPATCHER_SEEN_INTERVAL
"""A running dispatcher marks itself every `DISPATCHER_SEEN_INTERVAL`: three missed marks and no reader is running"""


def reader_running() -> bool:
    """Whether a card reader (a dispatcher, in any process) marked itself running in the last `READER_SEEN_WITHIN`"""
    seen = storage.dispatcher_seen_at()
    return seen is not None and time.time() - seen <= READER_SEEN_WITHIN


@router.get("", response_model=IngestAbout)
def get_ai_about() -> IngestAbout:
    """
    The server's version and its AI features: whether recipe card scanning is on, its upload limits, and whether a
    card reader is running
    """
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
                worker=settings.ENABLED and reader_running(),
            ),
            mcp=True,
        ),
    )
