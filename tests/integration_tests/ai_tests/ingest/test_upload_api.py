"""
`POST /api/ai/ingest` (docs/ai/PHASE2.md §1.2, §2, §14): the checks before the body, the three body shapes, the 202
and its summary, rejections and duplicates, and the pause. Runs on SQLite and PostgreSQL.

The helpers at the top are shared by the other upload and batch API tests.
"""

import asyncio
import fcntl
import io
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from PIL import Image

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate
from mealie.schema.recipe_ingest import (
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
    RecipeIngestionSettingsUpdate,
)
from mealie.services import ocr
from mealie.services.ai.ingest import images, intake, limits, storage
from mealie.services.ai.ingest import upload as upload_service
from mealie.services.ai.ingest.settings import IngestSettings
from tests.utils.fixture_schemas import TestUser

INGEST = "/api/ai/ingest"
ORIENTATION = 0x0112
GPS_IFD = 0x8825

# ==================================================================================================================
# Shared helpers


def jpeg(size: tuple[int, int] = (96, 64), *, gps: bool = False, orientation: int | None = None) -> bytes:
    """A small JPEG that's different every time (so it's never a duplicate of another test's card)"""
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    exif = Image.Exif()
    if orientation:
        exif[ORIENTATION] = orientation
    if gps:
        exif[GPS_IFD] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80, exif=exif.tobytes())
    return buffer.getvalue()


def configure_card_reading(user: TestUser, *, image: bool = True) -> None:
    """Gives the user's group a default provider (and an image provider), so it can read cards"""
    repos = user.repos
    default = repos.group_ai_providers.create(AIProviderCreate(name="Text", model="m", api_key="k"))
    image_provider = (
        repos.group_ai_providers.create(AIProviderCreate(name="Vision", model="m", api_key="k")) if image else None
    )
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(
            default_provider_id=default.id,
            image_provider_id=image_provider.id if image_provider else None,
            audio_provider_id=None,
        ),
    )


def files(*images_: bytes, names: list[str] | None = None) -> list[tuple[str, tuple[str, bytes, str]]]:
    names = names or [f"photo-{n}.jpg" for n in range(len(images_))]
    return [("files", (name, data, "image/jpeg")) for name, data in zip(names, images_, strict=True)]


def post_card(
    api_client: TestClient, user: TestUser, *images_: bytes, headers: dict | None = None, **fields: Any
) -> Any:
    data = {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in fields.items()}
    return api_client.post(INGEST, files=files(*images_), data=data, headers={**user.token, **(headers or {})})


def job_row(job_id: str | UUID) -> RecipeIngestionJob:
    with session_context() as session:
        job = session.get(RecipeIngestionJob, UUID(str(job_id)))
        assert job is not None
        session.expunge(job)
        return job


def batch_row(batch_id: str | UUID) -> RecipeIngestionBatch:
    with session_context() as session:
        batch = session.get(RecipeIngestionBatch, UUID(str(batch_id)))
        assert batch is not None
        session.expunge(batch)
        return batch


def job_dirs(user: TestUser) -> set[str]:
    root = storage.ingest_root(UUID(user.group_id))
    return {path.name for path in root.iterdir()} if root.exists() else set()


def job_count(user: TestUser) -> int:
    with session_context() as session:
        return session.execute(
            sa.select(sa.func.count())
            .select_from(RecipeIngestionJob)
            .where(RecipeIngestionJob.household_id == UUID(user.household_id))
        ).scalar_one()


def assert_no_message_anywhere(body: Any) -> None:
    """The frontend's axios interceptor toasts any `message`; a 202 must have none, at any depth"""
    if isinstance(body, dict):
        assert "message" not in body
        for value in body.values():
            assert_no_message_anywhere(value)
    elif isinstance(body, list):
        for value in body:
            assert_no_message_anywhere(value)


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether a group can read cards mustn't depend on this machine having Tesseract"""
    monkeypatch.setattr(ocr, "is_available", lambda: False)


@pytest.fixture(scope="module")
def reader(unique_user: TestUser) -> TestUser:
    """`unique_user`, whose group can read cards"""
    configure_card_reading(unique_user)
    return unique_user


@pytest.fixture()
def paused(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    marker = storage.pause_marker_path()
    marker.write_text(str(time.time()))
    yield marker
    marker.unlink(missing_ok=True)


# ==================================================================================================================
# Before the body


def test_an_unauthenticated_upload_is_401_with_no_body_read(api_client: TestClient):
    consumed: list[int] = []

    def body() -> Iterator[bytes]:
        for _ in range(5):
            consumed.append(1)
            yield b"\0" * (1024 * 1024)

    response = api_client.post(INGEST, content=body(), headers={"Content-Type": "image/jpeg"})
    assert response.status_code == 401
    assert consumed == []


def test_a_session_cookie_alone_is_401(api_client: TestClient, reader: TestUser):
    consumed: list[int] = []

    def body() -> Iterator[bytes]:
        consumed.append(1)
        yield jpeg()

    token = reader.token["Authorization"].removeprefix("Bearer ")
    api_client.cookies.set("mealie.access_token", token)
    response = api_client.post(INGEST, content=body(), headers={"Content-Type": "image/jpeg"})
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["code"] == "authorization_required"
    assert detail["message"] == "Send your API token in the Authorization header to upload recipe cards."
    assert consumed == []


def test_503_while_paused_with_no_body_read(api_client: TestClient, reader: TestUser, paused: Path):
    consumed: list[int] = []

    def body() -> Iterator[bytes]:
        consumed.append(1)
        yield jpeg()

    response = api_client.post(INGEST, content=body(), headers={**reader.token, "Content-Type": "image/jpeg"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(limits.PAUSED_RETRY_AFTER)
    assert response.json()["detail"]["code"] == "paused_for_restore"
    assert response.json()["detail"]["message"]
    assert consumed == []


def test_503_when_ingestion_is_off(api_client: TestClient, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(upload_service, "get_ingest_settings", lambda: IngestSettings(ENABLED=False, WORKER=False))
    response = post_card(api_client, reader, jpeg())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ingest_disabled"
    assert response.json()["detail"]["message"]


def test_400_when_the_group_cant_read_cards(api_client: TestClient, unique_user_fn_scoped: TestUser):
    response = post_card(api_client, unique_user_fn_scoped, jpeg())
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ai_not_enabled"
    assert response.json()["detail"]["message"]

    # a default provider alone reads cards only with OCR
    configure_card_reading(unique_user_fn_scoped, image=False)
    assert post_card(api_client, unique_user_fn_scoped, jpeg()).json()["detail"]["code"] == "ai_not_enabled"


def test_a_default_provider_with_ocr_can_read_cards(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    configure_card_reading(unique_user_fn_scoped, image=False)
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    assert post_card(api_client, unique_user_fn_scoped, jpeg()).status_code == 202


def test_400_when_cards_must_stay_local_and_no_local_provider_can_read_them(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    configure_card_reading(user)

    # the upload's own request
    response = post_card(api_client, user, jpeg(), localOnly=True)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "local_only_unavailable"
    assert response.json()["detail"]["message"]
    assert job_count(user) == 0

    # the group's setting
    with session_context() as session:
        IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).settings.upsert(
            RecipeIngestionSettingsUpdate(local_only=True)
        )
    response = post_card(api_client, user, jpeg())
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "local_only_unavailable"


def test_429_at_the_groups_processing_quota(api_client: TestClient, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    assert post_card(api_client, reader, jpeg()).status_code == 202
    with session_context() as session:
        processing = IngestRepos(session, UUID(reader.group_id), None).processing_jobs_in_group()

    monkeypatch.setattr(limits, "MAX_PROCESSING_JOBS_PER_GROUP", processing)
    response = post_card(api_client, reader, jpeg())
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(limits.QUOTA_RETRY_AFTER)
    assert response.json()["detail"]["code"] == "too_many_jobs"
    assert response.json()["detail"]["message"]


def test_the_checks_before_the_body_run_in_one_worker_thread_call(
    api_client: TestClient, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    calls: list[bool] = []
    real = upload_service.reading_readiness

    def readiness(*args: Any, **kwargs: Any) -> intake.ReadingReadiness:
        try:
            asyncio.get_running_loop()
            on_event_loop = True
        except RuntimeError:
            on_event_loop = False
        calls.append(on_event_loop)
        return real(*args, **kwargs)

    monkeypatch.setattr(upload_service, "reading_readiness", readiness)
    assert post_card(api_client, reader, jpeg()).status_code == 202
    assert calls == [False]


def test_415_for_anything_but_images_forms_and_json(api_client: TestClient, reader: TestUser):
    response = api_client.post(INGEST, content=b"hello", headers={**reader.token, "Content-Type": "text/plain"})
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_media_type"
    assert response.json()["detail"]["message"]


# ==================================================================================================================
# The three shapes


def test_a_multipart_card_with_front_and_back(api_client: TestClient, reader: TestUser):
    front, back = jpeg(gps=True), jpeg((64, 96), gps=True)
    response = api_client.post(
        INGEST, files=files(front, back, names=["IMG_0007.jpg", "IMG_0008.jpg"]), headers=reader.token
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert_no_message_anywhere(body)
    assert body["summary"] == "1 recipe card queued. You'll be notified when it's ready."
    assert body["rejected"] == []
    [item] = body["jobs"]
    assert item["status"] == "processing"
    assert item["pageCount"] == 2
    group_slug = api_client.get("/api/users/self", headers=reader.token).json()["groupSlug"]
    assert item["reviewPath"] == f"/g/{group_slug}/recipes/cards/{item['id']}"

    job = job_row(item["id"])
    assert str(job.batch_id) == body["batchId"]
    assert job.status == IngestStatus.processing.value
    assert (job.task_kind, job.task_state) == (IngestTaskKind.extract.value, IngestTaskState.queued.value)
    assert job.task_priority == limits.PRIORITY_EXTRACT
    assert job.source == IngestSource.api.value
    assert job.source_name == "upload/IMG_0007.jpg"
    assert job.created_by == reader.user_id
    assert job.locale == "en-US"
    assert job.local_only is False
    assert job.integration_id
    pages = [PageMeta.model_validate(page) for page in job.pages]
    assert [page.index for page in pages] == [0, 1]
    assert [page.original_filename for page in pages] == ["IMG_0007.jpg", "IMG_0008.jpg"]
    assert (pages[1].width, pages[1].height) == (64, 96)

    # pages on disk, with no metadata
    job_dir = storage.job_dir(UUID(reader.group_id), job.id)
    for index in (0, 1):
        for name in (images.PAGE_FILE, images.VIEW_FILE, images.THUMB_FILE):
            data = (job_dir / "pages" / str(index) / name).read_bytes()
            assert b"GPS" not in data and b"Exif" not in data

    batch = batch_row(body["batchId"])
    assert batch.source == IngestSource.api.value
    assert batch.created_by == reader.user_id
    assert batch.sealed_at is None


def test_a_raw_image_body_with_options_in_the_query(api_client: TestClient, reader: TestUser):
    response = api_client.post(
        f"{INGEST}?position=7&filename=card.jpg&batchId=new",
        content=jpeg(),
        headers={**reader.token, "Content-Type": "image/jpeg"},
    )
    assert response.status_code == 202, response.text
    job = job_row(response.json()["jobs"][0]["id"])
    assert job.position == 7
    assert job.source_name == "upload/card.jpg"


def test_an_octet_stream_body_is_an_image_too(api_client: TestClient, reader: TestUser):
    response = api_client.post(
        INGEST, content=jpeg(), headers={**reader.token, "Content-Type": "application/octet-stream"}
    )
    assert response.status_code == 202, response.text
    assert job_row(response.json()["jobs"][0]["id"]).source_name is None


def test_json_with_base64_as_shortcuts_send_it(api_client: TestClient, reader: TestUser):
    import base64

    front = base64.b64encode(jpeg()).decode()
    wrapped = "\n".join(front[i : i + 76] for i in range(0, len(front), 76))  # Shortcuts wraps lines
    back = "data:image/jpeg;base64," + base64.b64encode(jpeg()).decode().rstrip("=")
    payload = {"images": [{"data": wrapped, "filename": "front.jpg"}, {"data": back}], "split": False}

    response = api_client.post(INGEST, json=payload, headers=reader.token)
    assert response.status_code == 202, response.text
    [item] = response.json()["jobs"]
    assert item["pageCount"] == 2
    assert job_row(item["id"]).source_name == "upload/front.jpg"


def test_split_makes_each_image_a_card_and_partial_success_is_202(api_client: TestClient, reader: TestUser):
    response = api_client.post(
        INGEST,
        files=[
            ("files", ("a.jpg", jpeg(), "image/jpeg")),
            ("files", ("b.pdf", b"%PDF-1.7\n...", "application/pdf")),
            ("other-name", ("c.jpg", jpeg(), "image/jpeg")),  # any file field is an image
        ],
        data={"split": "true", "batchId": "new"},
        headers=reader.token,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert len(body["jobs"]) == 2
    assert body["rejected"] == [{"index": 1, "filename": "b.pdf", "reason": "pdf_not_supported", "duplicateOf": None}]
    assert body["summary"] == (
        "2 recipe cards queued. You'll be notified when they're ready. 1 photo couldn't be used."
    )
    jobs = [job_row(item["id"]) for item in body["jobs"]]
    assert {str(job.batch_id) for job in jobs} == {body["batchId"]}
    assert [job.position for job in jobs] == [0, 1]


def test_nothing_accepted_is_400_with_the_same_body(api_client: TestClient, reader: TestUser):
    before = job_dirs(reader)
    response = post_card(api_client, reader, b"\xff\xd8\xff\xe0 not really a jpeg")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "nothing_accepted"
    assert "message" not in detail
    assert detail["jobs"] == []
    assert detail["batchId"] is None
    assert detail["rejected"] == [
        {"index": 0, "filename": "photo-0.jpg", "reason": "unreadable_image", "duplicateOf": None}
    ]
    assert detail["summary"] == "No recipe cards were queued. 1 photo couldn't be used."
    assert job_dirs(reader) == before  # nothing left in DATA_DIR


def test_an_empty_form_accepts_nothing(api_client: TestClient, reader: TestUser):
    response = api_client.post(
        INGEST, data={"split": "true"}, files={"x": ("", b"", "image/jpeg")}, headers=reader.token
    )
    assert response.status_code == 400
    assert response.json()["detail"]["summary"] == "No recipe cards were queued."


def test_malformed_bodies_are_400(api_client: TestClient, reader: TestUser):
    response = api_client.post(
        INGEST, content=b"{not json", headers={**reader.token, "Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_body"

    response = api_client.post(INGEST, json={"pictures": []}, headers=reader.token)
    assert response.json()["detail"]["code"] == "invalid_body"

    response = post_card(api_client, reader, jpeg(), position="first")
    assert response.json()["detail"]["code"] == "invalid_body"


def test_undecodable_base64_is_a_rejected_image(api_client: TestClient, reader: TestUser):
    response = api_client.post(INGEST, json={"images": [{"data": "%%%"}]}, headers=reader.token)
    assert response.status_code == 400
    assert response.json()["detail"]["rejected"][0]["reason"] == "unreadable_image"


# ==================================================================================================================
# Duplicates


def test_duplicates_are_found_by_the_ordered_page_hashes(api_client: TestClient, reader: TestUser):
    front, back = jpeg(), jpeg()
    first = post_card(api_client, reader, front)
    assert first.status_code == 202
    first_id = first.json()["jobs"][0]["id"]

    again = post_card(api_client, reader, front)
    assert again.status_code == 400
    assert again.json()["detail"]["rejected"] == [
        {"index": 0, "filename": "photo-0.jpg", "reason": "duplicate", "duplicateOf": first_id}
    ]

    # the same front sent again with its back is a new card, and so is the other way round
    assert post_card(api_client, reader, front, back).status_code == 202
    assert post_card(api_client, reader, back, front).status_code == 202
    # unless asked, a committed card counts too
    with session_context() as session:
        session.execute(
            sa.update(RecipeIngestionJob)
            .where(RecipeIngestionJob.id == UUID(first_id))
            .values(status=IngestStatus.committed.value)
        )
        session.commit()
    assert post_card(api_client, reader, front).status_code == 400
    assert post_card(api_client, reader, front, allowDuplicate=True).status_code == 202


# ==================================================================================================================
# Language, privacy, batches


def test_the_summary_is_in_the_requests_language(
    api_client: TestClient, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    class Translator:
        def __init__(self, locale: str) -> None:
            self.locale = locale

        def t(self, key: str, default: Any = None, **kwargs: Any) -> str:
            return f"[{self.locale}] {key} {kwargs.get('count', '')}".strip()

    monkeypatch.setattr(upload_service, "get_locale_provider", Translator)
    response = post_card(api_client, reader, jpeg(), headers={"Accept-Language": "fr-CA;q=0.4, de-DE,de;q=0.9"})
    assert response.status_code == 202
    assert response.json()["summary"] == "[de-DE] recipe-ingest.upload-summary 1"
    assert job_row(response.json()["jobs"][0]["id"]).locale == "de-DE"


def test_a_language_without_the_text_falls_back_to_english(api_client: TestClient, reader: TestUser):
    response = post_card(api_client, reader, jpeg(), headers={"Accept-Language": "de-DE"})
    assert response.json()["summary"] == "1 recipe card queued. You'll be notified when it's ready."

    response = api_client.post(
        INGEST, content=b"x", headers={**reader.token, "Content-Type": "text/plain", "Accept-Language": "de-DE"}
    )
    assert not response.json()["detail"]["message"].startswith("recipe-ingest.")


@pytest.mark.parametrize(
    "header, locale",
    [
        (None, "en-US"),
        ("de-DE,de;q=0.9", "de-DE"),
        ("de", "de-DE"),
        ("en", "en-US"),
        ("en-gb", "en-GB"),
        ("fr;q=0.5, nl;q=0.8", "nl-NL"),
        ("*", "en-US"),
        ("xx-YY", "en-US"),
        ("pt_BR", "pt-BR"),
    ],
)
def test_accept_language_resolves_to_a_supported_locale(header: str | None, locale: str):
    assert upload_service.resolve_locale(header) == locale


def test_a_card_asked_to_stay_local_is_a_local_only_job(
    api_client: TestClient, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    real = upload_service.reading_readiness

    def local_ready(*args: Any, **kwargs: Any) -> intake.ReadingReadiness:
        readiness = real(*args, **kwargs)
        return intake.ReadingReadiness(
            can_read=True, local_ready=True, group_local_only=False, processing=readiness.processing
        )

    monkeypatch.setattr(upload_service, "reading_readiness", local_ready)
    response = post_card(api_client, reader, jpeg(), localOnly=True)
    assert response.status_code == 202
    assert job_row(response.json()["jobs"][0]["id"]).local_only is True


def test_an_unknown_or_foreign_batch_is_404(api_client: TestClient, reader: TestUser, h2_user: TestUser):
    with session_context() as session:
        theirs = IngestRepos(session, UUID(h2_user.group_id), UUID(h2_user.household_id)).batches.create(
            source=IngestSource.app, created_by=h2_user.user_id
        )
    for batch_id in (str(theirs), "4f9c6b8e-0000-4000-8000-000000000000", "not-a-batch"):
        response = post_card(api_client, reader, jpeg(), batchId=batch_id)
        assert response.status_code == 404, batch_id
        assert response.json()["detail"] == {"code": "not_found"}


# ==================================================================================================================
# The pause after the body


def test_a_pause_that_starts_after_the_body_was_read_is_503_and_writes_nothing(
    api_client: TestClient, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    real = upload_service.UploadHandler._read_body

    async def read_then_pause(self: upload_service.UploadHandler, *args: Any) -> Any:
        body = await real(self, *args)
        storage.pause_marker_path().write_text(str(time.time()))
        return body

    monkeypatch.setattr(upload_service.UploadHandler, "_read_body", read_then_pause)
    before_dirs, before_jobs = job_dirs(reader), job_count(reader)
    try:
        response = post_card(api_client, reader, jpeg())
    finally:
        storage.pause_marker_path().unlink(missing_ok=True)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "paused_for_restore"
    assert response.headers["Retry-After"] == "60"
    assert job_dirs(reader) == before_dirs
    assert job_count(reader) == before_jobs


def test_a_restore_holding_the_write_lock_is_503(api_client: TestClient, reader: TestUser):
    held, release = threading.Event(), threading.Event()

    def restore() -> None:
        fd = os.open(storage.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.set()
            release.wait(10)
        finally:
            os.close(fd)

    thread = threading.Thread(target=restore, daemon=True)
    thread.start()
    assert held.wait(5)
    before = job_dirs(reader)
    try:
        response = post_card(api_client, reader, jpeg())
    finally:
        release.set()
        thread.join(5)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "paused_for_restore"
    assert job_dirs(reader) == before


def test_error_bodies_carry_a_code_and_a_message_only_where_the_page_doesnt_handle_them(
    api_client: TestClient, reader: TestUser
):
    shown = api_client.post(INGEST, content=b"x", headers={**reader.token, "Content-Type": "text/plain"})
    assert set(shown.json()["detail"]) == {"code", "message"}

    handled = post_card(api_client, reader, jpeg(), batchId="4f9c6b8e-0000-4000-8000-000000000001")
    assert set(handled.json()["detail"]) == {"code"}

    assert json.dumps(post_card(api_client, reader, jpeg()).json()).count('"message"') == 0
