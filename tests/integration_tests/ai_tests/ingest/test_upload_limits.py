"""
`POST /api/ai/ingest`'s size limits (docs/ai/PHASE2.md §1.2, §1.5): 413 by `Content-Length` before any body byte is
read, 413 from the byte counter for chunked bodies, the 45 MiB JSON cap, and the per-file, per-request and per-card
counts.
"""

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest import upload as upload_service
from mealie.services.ai.ingest.settings import IngestSettings
from tests.integration_tests.ai_tests.ingest.test_upload_api import (
    INGEST,
    configure_card_reading,
    files,
    job_dirs,
    jpeg,
    no_ocr,  # noqa: F401  (the fixture)
    post_card,
)
from tests.utils.fixture_schemas import TestUser

MIB = 1024 * 1024


@pytest.fixture(scope="module")
def reader(unique_user: TestUser) -> TestUser:
    configure_card_reading(unique_user)
    return unique_user


@pytest.fixture()
def one_mib_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_service, "get_ingest_settings", lambda: IngestSettings(MAX_UPLOAD_MB=1, WORKER=False))


def _counted(chunks: int, size: int, consumed: list[int]) -> Iterator[bytes]:
    for _ in range(chunks):
        consumed.append(size)
        yield b"\0" * size


def test_413_by_content_length_before_the_body_is_read(api_client: TestClient, reader: TestUser, one_mib_uploads: None):
    consumed: list[int] = []
    response = api_client.post(
        INGEST,
        content=_counted(1, 1024, consumed),
        headers={**reader.token, "Content-Type": "image/jpeg", "Content-Length": str(2 * MIB)},
    )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail == {"code": "too_large", "message": "The upload is too large. The limit is 1 MB."}
    assert consumed == []


def test_413_mid_stream_for_a_chunked_body(api_client: TestClient, reader: TestUser, one_mib_uploads: None):
    before = job_dirs(reader)
    consumed: list[int] = []
    response = api_client.post(
        INGEST, content=_counted(3, MIB // 2, consumed), headers={**reader.token, "Content-Type": "image/jpeg"}
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "too_large"
    assert job_dirs(reader) == before


def test_413_mid_stream_for_a_multipart_body(api_client: TestClient, reader: TestUser, one_mib_uploads: None):
    head = (
        b'--cut\r\nContent-Disposition: form-data; name="files"; filename="a.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
    )

    def chunked() -> Iterator[bytes]:  # no Content-Length: only the counter can stop it
        yield head
        for _ in range(4):
            yield b"\xff" * (MIB // 2)
        yield b"\r\n--cut--\r\n"

    before = job_dirs(reader)
    response = api_client.post(
        INGEST, content=chunked(), headers={**reader.token, "Content-Type": "multipart/form-data; boundary=cut"}
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "too_large"
    assert job_dirs(reader) == before


def test_a_46_mib_json_body_is_413(api_client: TestClient, reader: TestUser):
    declared = api_client.post(
        INGEST,
        content=b"{}",
        headers={**reader.token, "Content-Type": "application/json", "Content-Length": str(46 * MIB)},
    )
    assert declared.status_code == 413
    assert declared.json()["detail"]["message"] == "The upload is too large. The limit is 45 MB."

    payload = b'{"images": [{"data": "' + b"A" * (46 * MIB) + b'"}]}'

    def chunked() -> Iterator[bytes]:
        for start in range(0, len(payload), 4 * MIB):
            yield payload[start : start + 4 * MIB]

    streamed = api_client.post(INGEST, content=chunked(), headers={**reader.token, "Content-Type": "application/json"})
    assert streamed.status_code == 413
    assert streamed.json()["detail"]["code"] == "too_large"


def test_a_photo_over_the_per_file_limit_is_rejected(
    api_client: TestClient, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 2048)
    big = jpeg((200, 200))
    assert len(big) > 2048

    for response in (
        post_card(api_client, reader, big),
        api_client.post(INGEST, content=big, headers={**reader.token, "Content-Type": "image/jpeg"}),
        api_client.post(INGEST, json={"images": [{"data": base64.b64encode(big).decode()}]}, headers=reader.token),
    ):
        assert response.status_code == 400
        assert response.json()["detail"]["rejected"][0]["reason"] == "too_large"


def test_more_than_twenty_images_in_a_request_is_400(api_client: TestClient, reader: TestUser):
    many = [jpeg((8, 8)) for _ in range(limits.MAX_IMAGES_PER_REQUEST + 1)]
    response = api_client.post(INGEST, files=files(*many), data={"split": "true"}, headers=reader.token)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_body"

    payload: dict[str, Any] = {"images": [base64.b64encode(data).decode() for data in many], "split": True}
    response = api_client.post(INGEST, json=payload, headers=reader.token)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_body"


def test_a_card_has_at_most_four_pages(api_client: TestClient, reader: TestUser):
    pages = [jpeg((8, 8)) for _ in range(limits.MAX_PAGES_PER_CARD + 1)]
    response = post_card(api_client, reader, *pages)
    assert response.status_code == 400
    assert response.json()["detail"]["rejected"] == [
        {"index": 4, "filename": "photo-4.jpg", "reason": "too_many_pages", "duplicateOf": None}
    ]

    # split, they're five cards
    response = post_card(api_client, reader, *pages, split=True)
    assert response.status_code == 202
    assert len(response.json()["jobs"]) == 5


def test_too_many_form_fields_is_400(api_client: TestClient, reader: TestUser):
    fields = {f"field{n}": "x" for n in range(limits.MAX_MULTIPART_FIELDS + 1)}
    response = api_client.post(INGEST, files=files(jpeg()), data=fields, headers=reader.token)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_body"
