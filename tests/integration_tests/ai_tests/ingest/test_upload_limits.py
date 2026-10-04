"""
`POST /api/ai/ingest`'s size limits (docs/ai/PHASE2.md §1.2, §1.5): 413 by `Content-Length` before any body byte is
read, 413 from the byte counter for chunked bodies, the 45 MiB JSON cap, and the per-file, per-request and per-card
counts.
"""

import base64
import io
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from tempfile import SpooledTemporaryFile
from typing import Any
from uuid import UUID

import anyio
import anyio.to_thread
import pytest
from fastapi.testclient import TestClient

from mealie.db.db_setup import session_context
from mealie.schema.recipe_ingest import IngestRejectReason, IngestSource
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest import upload as upload_service
from mealie.services.ai.ingest.intake import IntakeCard, IntakeOptions, IntakePage, IntakeRejected, IntakeService
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


class _Body:
    """A request that only has a body, for reading one without a server"""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def stream(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.data), MIB):
            yield self.data[start : start + MIB]


def test_json_bodies_are_decoded_two_at_a_time(monkeypatch: pytest.MonkeyPatch):
    # decoding a 45 MiB body holds about three times that in memory, so it takes one of the intake slots
    running = 0
    most = 0
    lock = threading.Lock()
    real_decode = upload_service._decode_json_images

    def decode(payload: Any, opened: list[Any]) -> Any:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.2)
        with lock:
            running -= 1
        return real_decode(payload, opened)

    monkeypatch.setattr(upload_service, "_decode_json_images", decode)
    payload = json.dumps({"images": [base64.b64encode(jpeg()).decode()]}).encode()
    bodies: list[Any] = []

    async def read_one() -> None:
        bodies.append(await upload_service._read_json(_Body(payload), limits.MAX_JSON_BODY_BYTES, {}))  # type: ignore[arg-type]

    async def main() -> None:
        async with anyio.create_task_group() as group:
            for _ in range(limits.INTAKE_CONCURRENCY + 3):
                group.start_soon(read_one)

    anyio.run(main)
    assert most == limits.INTAKE_CONCURRENCY
    assert all(len(body.images) == 1 and body.images[0].file is not None for body in bodies)
    for body in bodies:
        body.close()


def test_decoded_json_images_wait_on_disk(monkeypatch: pytest.MonkeyPatch):
    # a decoded image waits for its intake in a spooled file, not in memory: uploads queued for a slot hold only their
    # files, and the request's end closes them
    photo = os.urandom(upload_service.SPOOL_MAX_BYTES + 1)
    payload = json.dumps({"images": [{"data": base64.b64encode(photo).decode(), "filename": "front.jpg"}]}).encode()

    body = anyio.run(upload_service._read_json, _Body(payload), limits.MAX_JSON_BODY_BYTES, {})  # type: ignore[arg-type]
    (image,) = body.images
    assert isinstance(image.file, SpooledTemporaryFile)
    assert image.file._rolled  # type: ignore[attr-defined]
    assert (image.filename, image.index, image.file.read()) == ("front.jpg", 0, photo)

    body.close()
    assert image.file.closed


def test_a_bad_json_option_closes_the_decoded_images(monkeypatch: pytest.MonkeyPatch):
    opened: list[Any] = []
    real_spool = upload_service.SpooledTemporaryFile

    def spool(*args: Any, **kwargs: Any) -> Any:
        opened.append(real_spool(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(upload_service, "SpooledTemporaryFile", spool)
    payload = json.dumps({"images": [base64.b64encode(jpeg()).decode()], "position": "first"}).encode()
    with pytest.raises(upload_service.UploadRefused):
        anyio.run(upload_service._read_json, _Body(payload), limits.MAX_JSON_BODY_BYTES, {})  # type: ignore[arg-type]
    assert len(opened) == 2  # the body and the image
    assert all(file.closed for file in opened)


def test_json_decoding_and_the_inbox_share_the_intake_slots(monkeypatch: pytest.MonkeyPatch, reader: TestUser):
    # the inbox's scan calls `ingest` directly, without the event loop's limiter: a JSON body's decoding takes one of
    # the same slots, so together they hold at most INTAKE_CONCURRENCY photos in memory
    running = 0
    most = 0
    lock = threading.Lock()

    def busy() -> None:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.2)
        with lock:
            running -= 1

    real_decode = upload_service._decode_json_images

    def decode(payload: Any, opened: list[Any]) -> Any:
        busy()
        return real_decode(payload, opened)

    def normalize_and_insert(self: IntakeService, *args: Any) -> Any:
        busy()
        return IntakeRejected(0, None, IngestRejectReason.unreadable_image)

    monkeypatch.setattr(upload_service, "_decode_json_images", decode)
    monkeypatch.setattr(IntakeService, "_normalize_and_insert", normalize_and_insert)
    payload = json.dumps({"images": [base64.b64encode(jpeg()).decode()]}).encode()
    bodies: list[Any] = []

    def inbox_card() -> None:
        with session_context() as session:
            service = IntakeService(session, UUID(reader.group_id), UUID(reader.household_id))
            service.ingest(IntakeCard([IntakePage(io.BytesIO(b""))]), IntakeOptions(source=IngestSource.inbox))

    async def read_one() -> None:
        bodies.append(await upload_service._read_json(_Body(payload), limits.MAX_JSON_BODY_BYTES, {}))  # type: ignore[arg-type]

    async def main() -> None:
        async with anyio.create_task_group() as group:
            for _ in range(limits.INTAKE_CONCURRENCY):
                group.start_soon(read_one)
            for _ in range(limits.INTAKE_CONCURRENCY):
                group.start_soon(anyio.to_thread.run_sync, inbox_card)

    anyio.run(main)
    assert most == limits.INTAKE_CONCURRENCY
    for body in bodies:
        body.close()
