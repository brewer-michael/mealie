"""`GET /api/ai/about` (docs/ai/PHASE2.md §14): public, and what it says about recipe card uploads"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mealie.core.settings.static import APP_VERSION
from mealie.routes.ai.ingest import about
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.settings import IngestSettings

ABOUT = "/api/ai/about"


def test_about_is_public_and_lists_the_upload_limits(api_client: TestClient):
    response = api_client.get(ABOUT)
    assert response.status_code == 200
    assert response.json() == {
        "version": APP_VERSION,
        "features": {
            "ingest": {
                "enabled": True,
                "maxUploadBytes": 100 * limits.MIB,
                "maxImagesPerRequest": limits.MAX_IMAGES_PER_REQUEST,
                "maxPagesPerCard": limits.MAX_PAGES_PER_CARD,
                "inbox": False,
                "worker": False,
            },
            "mcp": True,
        },
    }


def test_about_follows_the_settings(api_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(about, "get_ingest_settings", lambda: IngestSettings(MAX_UPLOAD_MB=20, WORKER=False))
    monkeypatch.setattr(about, "inbox_root", lambda: tmp_path)
    ingest = api_client.get(ABOUT).json()["features"]["ingest"]
    assert ingest["maxUploadBytes"] == 20 * limits.MIB
    assert ingest["inbox"] is True

    monkeypatch.setattr(about, "get_ingest_settings", lambda: IngestSettings(ENABLED=False, WORKER=False))
    ingest = api_client.get(ABOUT).json()["features"]["ingest"]
    assert ingest["enabled"] is False
    assert ingest["inbox"] is False
