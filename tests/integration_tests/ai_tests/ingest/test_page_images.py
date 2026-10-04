"""
A card's page images (docs/ai/PHASE2.md §9, §14): served only to the job's household through the fork route, private
and `nosniff`, with an ETag that changes when the page is turned and immutable caching for versioned URLs. Runs on
SQLite and PostgreSQL.
"""

import io
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_jobs_api import assert_code, job_url, seed_job, set_columns, use_fake_flags

from mealie.schema.recipe_ingest import IngestStatus
from mealie.services.ai.ingest import storage
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


@pytest.mark.parametrize(
    ("kind", "media_type", "pil_format", "longest"),
    [("page", "image/jpeg", "JPEG", 640), ("view", "image/jpeg", "JPEG", 640), ("thumb", "image/webp", "WEBP", 480)],
)
def test_each_image(
    kind: str, media_type: str, pil_format: str, longest: int, api_client: TestClient, unique_user: TestUser
):
    job_id = seed_job(unique_user)
    page = api_client.get(job_url(job_id), headers=unique_user.token).json()["pages"][0]
    url = page[f"{kind}Url"]

    response = api_client.get(url, headers=unique_user.token)
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["etag"].endswith(f'-r0-{kind}"')
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.format == pil_format
        assert max(image.size) == longest
        assert not image.getexif()

    # without the version (or with an old one) the browser checks back every time
    unversioned = api_client.get(job_url(job_id, "pages", 0, kind), headers=unique_user.token)
    assert unversioned.headers["cache-control"] == "private, no-cache"


def test_the_etag_follows_the_rotation(api_client: TestClient, unique_user: TestUser):
    job_id = seed_job(unique_user)
    before = api_client.get(job_url(job_id), headers=unique_user.token).json()["pages"][0]
    url = job_url(job_id, "pages", 0, "view")
    first = api_client.get(url, headers=unique_user.token)
    etag = first.headers["etag"]

    cached = api_client.get(url, headers={**unique_user.token, "If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == etag
    assert api_client.get(url, headers={**unique_user.token, "If-None-Match": '"other", ' + etag}).status_code == 304

    rotated = api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 270}, headers=unique_user.token)
    assert rotated.status_code == 200
    after = api_client.get(url, headers={**unique_user.token, "If-None-Match": etag})
    assert after.status_code == 200
    assert after.headers["etag"] != etag
    assert after.headers["etag"].endswith('-r270-view"')
    with Image.open(io.BytesIO(after.content)) as image:
        assert image.size == (640, 480)
    assert rotated.json()["viewUrl"] != before["viewUrl"]  # a new version: never shown from the cache


def test_missing_pages_and_files(api_client: TestClient, unique_user: TestUser):
    job_id = seed_job(unique_user, page_count=2)
    assert api_client.get(job_url(job_id, "pages", 1, "thumb"), headers=unique_user.token).status_code == 200
    assert_code(api_client.get(job_url(job_id, "pages", 2, "thumb"), headers=unique_user.token), 404, "not_found")
    assert api_client.get(job_url(job_id, "pages", 0, "original"), headers=unique_user.token).status_code == 422

    (storage.page_dir(UUID(unique_user.group_id), job_id, 1) / "thumb.webp").unlink()
    assert_code(api_client.get(job_url(job_id, "pages", 1, "thumb"), headers=unique_user.token), 404, "not_found")


def test_purged_cards_have_no_images(api_client: TestClient, unique_user: TestUser):
    job_id = seed_job(unique_user, status=IngestStatus.committed)
    set_columns(job_id, draft=None, flags=None, transcription=None)  # what the retention purge leaves
    assert_code(api_client.get(job_url(job_id, "pages", 0, "view"), headers=unique_user.token), 404, "not_found")
    job = api_client.get(job_url(job_id), headers=unique_user.token).json()
    assert job["pages"] == []
    assert job["thumbUrl"] is None
    assert job["pageCount"] == 1


def test_images_need_a_user(api_client: TestClient, unique_user: TestUser):
    job_id = seed_job(unique_user)
    assert api_client.get(job_url(job_id, "pages", 0, "view")).status_code == 401
