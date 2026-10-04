"""
A backup restore that recipe card ingestion keeps busy (docs/ai/PHASE2.md §3.9): when in-flight writes still hold the
ingest write lock after the wait, the restore gives up before changing anything, and the admin is told to try again
(503 with a message) rather than shown a bare server error.
"""

import fcntl
import os

import pytest
from fastapi.testclient import TestClient

from mealie.services.ai.ingest import limits, storage
from tests.utils import api_routes


@pytest.mark.parametrize("locale", ["en-US", "de-DE"])
def test_a_restore_refused_while_ingestion_writes_says_to_try_again(
    api_client: TestClient, admin_token: dict, monkeypatch: pytest.MonkeyPatch, locale: str
):
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 0.2)
    monkeypatch.setattr(limits, "RESTORE_LOCK_POLL", 0.05)

    fd = os.open(storage.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # an ingest write in progress
        response = api_client.post(
            api_routes.admin_backups_file_name_restore("never-restored.zip"),
            headers={**admin_token, "Accept-Language": locale},
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert response.status_code == 503, response.text
    message = response.json()["detail"]["message"]
    assert message == "Recipe card ingestion is busy writing files. Try the restore again in a minute."
    assert not storage.pause_marker_path().exists()  # ingestion isn't left paused
