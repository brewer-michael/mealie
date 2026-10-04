"""`GET /api/ai/about` (docs/ai/PHASE2.md §14): public, and what it says about recipe card uploads"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mealie.core.settings.static import APP_VERSION
from mealie.routes.ai.ingest import about
from mealie.services.ai.ingest import limits, storage
from mealie.services.ai.ingest.settings import IngestSettings

ABOUT = "/api/ai/about"


@pytest.fixture(autouse=True)
def no_reader_seen() -> Iterator[Path]:
    """No dispatcher presence file, as on a server with no reader; what another test's dispatcher left is put back"""
    path = storage.dispatcher_seen_path()
    seen = path.stat().st_mtime if path.exists() else None
    path.unlink(missing_ok=True)
    yield path
    path.unlink(missing_ok=True)
    if seen is not None:
        path.touch()
        os.utime(path, (seen, seen))


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


def test_about_says_whether_a_card_reader_is_running(api_client: TestClient, no_reader_seen: Path):
    def worker() -> bool:
        return api_client.get(ABOUT).json()["features"]["ingest"]["worker"]

    assert worker() is False  # no dispatcher ever ran

    storage.mark_dispatcher_seen()  # what a running dispatcher does every minute
    assert worker() is True

    stale = time.time() - about.READER_SEEN_WITHIN - 5  # three marks missed: the reader stopped
    os.utime(no_reader_seen, (stale, stale))
    assert worker() is False

    recent = time.time() - about.READER_SEEN_WITHIN + 30
    os.utime(no_reader_seen, (recent, recent))
    assert worker() is True


def test_no_reader_runs_while_ingestion_is_off(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch, no_reader_seen: Path
):
    storage.mark_dispatcher_seen()
    monkeypatch.setattr(about, "get_ingest_settings", lambda: IngestSettings(ENABLED=False, WORKER=False))
    assert api_client.get(ABOUT).json()["features"]["ingest"]["worker"] is False
