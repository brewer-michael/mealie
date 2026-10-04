"""
Image URLs in `POST /api/ai/ingest`'s JSON (docs/ai/PHASE2.md §1.2, `fetch_url`): off by default, fetched only after
every check, through the real safehttp transport, from a server on 127.0.0.1 allowed with `AI_INGEST_URL_ALLOW_HOSTS`.
The finer rules (redirects, caps, deadlines, logs) are in `test_fetch_url.py`.
"""

import base64
import io
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from mealie.schema.recipe_ingest import PageMeta
from mealie.services import ocr
from mealie.services.ai.ingest import fetch_url, images, storage
from mealie.services.ai.ingest import upload as upload_service
from mealie.services.ai.ingest.settings import IngestSettings
from tests.integration_tests.ai_tests.ingest.test_upload_api import (
    INGEST,
    configure_card_reading,
    job_count,
    job_row,
    jpeg,
)
from tests.utils.fixture_schemas import TestUser

GPS_IFD = 0x8825
SECRET = "s3cr3t-camera-token"


def photo(size: tuple[int, int]) -> bytes:
    """A JPEG with GPS in its EXIF, as a camera snapshot might have"""
    exif = Image.Exif()
    exif[GPS_IFD] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue()


class CardServer:
    """A small web server on 127.0.0.1 standing in for Home Assistant: what it serves, and every request it got"""

    def __init__(self) -> None:
        self.files: dict[str, tuple[int, bytes, dict[str, str]]] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.delay = 0.0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.requests.append((self.path, dict(self.headers.items())))
                path = self.path.split("?", 1)[0]
                if path == "/slow":
                    time.sleep(server.delay)
                status, body, headers = server.files.get(path, (404, b"not found", {}))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def serve(self, path: str, body: bytes, status: int = 200, **headers: str) -> str:
        self.files[path] = (status, body, {"Content-Type": "image/jpeg", **headers})
        return self.base + path

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def ha() -> Iterator[CardServer]:
    server = CardServer()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def fresh(ha: CardServer, monkeypatch: pytest.MonkeyPatch) -> None:
    ha.requests.clear()
    ha.files.clear()
    ha.delay = 0.0
    monkeypatch.setattr(ocr, "is_available", lambda: False)


def fetching(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    settings = IngestSettings(**{"URL_FETCH": True, "URL_ALLOW_HOSTS": "127.0.0.1", "WORKER": False, **values})
    monkeypatch.setattr(fetch_url, "get_ingest_settings", lambda: settings)


@pytest.fixture(scope="module")
def reader(unique_user: TestUser) -> TestUser:
    configure_card_reading(unique_user)
    return unique_user


def post(api_client: TestClient, user: TestUser, *images_: dict | str, **options: Any) -> Any:
    return api_client.post(INGEST, json={"images": list(images_), **options}, headers=user.token)


def b64(data: bytes) -> dict:
    return {"data": base64.b64encode(data).decode()}


# ==========================================
# Off by default, and only after every check


def test_urls_are_refused_and_never_fetched_by_default(api_client: TestClient, reader: TestUser, ha: CardServer):
    url = ha.serve("/card.jpg", photo((80, 60)))
    response = post(api_client, reader, {"url": url})
    assert response.status_code == 400
    assert response.json()["detail"]["rejected"] == [
        {"index": 0, "filename": "card.jpg", "reason": "url_not_allowed", "duplicateOf": None}
    ]
    assert ha.requests == []


def test_a_private_address_isnt_fetched_without_the_allow_list(
    api_client: TestClient, reader: TestUser, ha: CardServer, monkeypatch: pytest.MonkeyPatch
):
    fetching(monkeypatch, URL_ALLOW_HOSTS="")
    response = post(api_client, reader, {"url": ha.serve("/card.jpg", photo((80, 60)))})
    assert response.json()["detail"]["rejected"][0]["reason"] == "url_not_allowed"
    assert ha.requests == []


def test_nothing_is_fetched_before_the_checks_pass(
    api_client: TestClient, reader: TestUser, unique_user_fn_scoped: TestUser, ha: CardServer, monkeypatch
):
    fetching(monkeypatch)
    url = ha.serve("/card.jpg", photo((80, 60)))

    # a cookie alone isn't enough (F18)
    token = reader.token["Authorization"].removeprefix("Bearer ")
    api_client.cookies.set("mealie.access_token", token)
    try:
        assert api_client.post(INGEST, json={"images": [{"url": url}]}).status_code == 401
    finally:
        api_client.cookies.clear()

    # a group that can't read cards
    assert post(api_client, unique_user_fn_scoped, {"url": url}).json()["detail"]["code"] == "ai_not_enabled"

    # a restore's pause
    marker = storage.pause_marker_path()
    marker.write_text(str(time.time()))
    try:
        assert post(api_client, reader, {"url": url}).status_code == 503
    finally:
        marker.unlink(missing_ok=True)

    # an unknown batch
    assert post(api_client, reader, {"url": url}, batchId=str(os.urandom(16).hex())).status_code == 404
    assert ha.requests == []


# ==========================================
# Fetched


def test_an_allowed_url_becomes_a_card_without_metadata(
    api_client: TestClient, reader: TestUser, ha: CardServer, monkeypatch: pytest.MonkeyPatch
):
    fetching(monkeypatch)
    url = ha.serve("/api/camera_proxy/camera.kitchen", photo((96, 64)))
    response = post(api_client, reader, {"url": f"{url}?token={SECRET}"})
    assert response.status_code == 202, response.text
    [job] = response.json()["jobs"]
    row = job_row(job["id"])
    assert row.source_name == "upload/camera.kitchen"  # never the query, which holds the camera's token
    [page] = [PageMeta.model_validate(item) for item in row.pages]
    assert (page.width, page.height, page.format, page.original_filename) == (96, 64, "jpeg", "camera.kitchen")

    page_jpg = storage.page_dir(row.group_id, row.id, 0) / images.PAGE_FILE
    data = page_jpg.read_bytes()
    assert b"Exif" not in data and b"GPS" not in data
    [(path, headers)] = ha.requests
    assert path == f"/api/camera_proxy/camera.kitchen?token={SECRET}"
    assert not {"Cookie", "Authorization"} & set(headers)


def test_base64_and_url_images_keep_their_order(
    api_client: TestClient, reader: TestUser, ha: CardServer, monkeypatch: pytest.MonkeyPatch
):
    fetching(monkeypatch)
    front = ha.serve("/front.jpg", photo((90, 60)))
    response = post(api_client, reader, b64(photo((60, 90))), {"url": front, "filename": "front side.jpg"})
    assert response.status_code == 202, response.text
    pages = [PageMeta.model_validate(item) for item in job_row(response.json()["jobs"][0]["id"]).pages]
    assert [(page.width, page.original_filename) for page in pages] == [(60, None), (90, "front side.jpg")]

    back = ha.serve("/back.jpg", photo((70, 50)))
    response = post(api_client, reader, {"url": back}, b64(photo((50, 70))), split=True)
    jobs = [job_row(item["id"]) for item in response.json()["jobs"]]
    assert [PageMeta.model_validate(job.pages[0]).width for job in jobs] == [70, 50]
    assert jobs[0].position + 1 == jobs[1].position


def test_a_redirects_cookie_isnt_sent_on(
    api_client: TestClient, reader: TestUser, ha: CardServer, monkeypatch: pytest.MonkeyPatch
):
    fetching(monkeypatch)
    ha.serve("/card.jpg", photo((80, 60)))
    start = ha.serve("/start", b"", status=302, Location="/card.jpg", **{"Set-Cookie": "session=abc; Path=/"})
    response = post(api_client, reader, {"url": start})
    assert response.status_code == 202, response.text
    assert [path for path, _ in ha.requests] == ["/start", "/card.jpg"]
    assert "Cookie" not in ha.requests[1][1]


@pytest.mark.parametrize(
    "path, body, status, reason",
    [
        ("/missing.jpg", b"", 404, "url_fetch_failed"),
        ("/error.jpg", b"oops", 500, "url_fetch_failed"),
        ("/page.html", b"<!doctype html><html><body>Sign in</body></html>", 200, "unsupported_format"),
    ],
)
def test_a_url_that_isnt_an_image_is_refused(
    api_client: TestClient,
    reader: TestUser,
    ha: CardServer,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: bytes,
    status: int,
    reason: str,
):
    fetching(monkeypatch)
    url = ha.serve(path, body, status=status) if status != 404 else ha.base + path
    response = post(api_client, reader, {"url": url})
    assert response.status_code == 400
    assert response.json()["detail"]["rejected"][0]["reason"] == reason


def test_a_slow_url_fails_at_its_deadline(
    api_client: TestClient, reader: TestUser, ha: CardServer, monkeypatch: pytest.MonkeyPatch
):
    fetching(monkeypatch, URL_TIMEOUT=1)
    ha.delay = 4
    ha.serve("/slow", photo((80, 60)))
    started = time.monotonic()
    response = post(api_client, reader, {"url": ha.base + "/slow"}, b64(jpeg()), split=True)
    assert time.monotonic() - started < 3.5
    assert response.status_code == 202
    assert [item["reason"] for item in response.json()["rejected"]] == ["url_fetch_failed"]
    assert len(response.json()["jobs"]) == 1


def test_fetched_images_share_the_requests_size_cap(
    api_client: TestClient, reader: TestUser, ha: CardServer, monkeypatch: pytest.MonkeyPatch
):
    fetching(monkeypatch)
    monkeypatch.setattr(upload_service, "get_ingest_settings", lambda: IngestSettings(MAX_UPLOAD_MB=1, WORKER=False))
    big = photo((1400, 1000))  # noise: over half a megabyte, under one
    assert 512 * 1024 < len(big) < 1024 * 1024
    first, second = ha.serve("/a.jpg", big), ha.serve("/b.jpg", big)
    response = post(api_client, reader, {"url": first}, {"url": second}, split=True)
    assert response.status_code == 202, response.text
    assert len(response.json()["jobs"]) == 1
    assert [(item["index"], item["reason"]) for item in response.json()["rejected"]] == [(1, "too_large")]


# ==========================================
# The body's shape


def test_an_image_is_either_a_url_or_data(api_client: TestClient, reader: TestUser, monkeypatch):
    fetching(monkeypatch)
    for item in ({"url": "http://127.0.0.1/a.jpg", "data": "AAAA"}, {"url": 42}, {"url": None}):
        response = post(api_client, reader, item)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_body"


def test_a_bare_string_is_always_base64(api_client: TestClient, reader: TestUser, ha: CardServer, monkeypatch):
    fetching(monkeypatch)
    url = ha.serve("/card.jpg", photo((80, 60)))
    response = post(api_client, reader, url)
    assert response.json()["detail"]["rejected"][0]["reason"] == "unreadable_image"
    assert ha.requests == []


def test_url_images_count_toward_the_images_per_request(api_client: TestClient, reader: TestUser, ha, monkeypatch):
    fetching(monkeypatch)
    before = job_count(reader)
    response = post(api_client, reader, *[{"url": f"{ha.base}/{n}.jpg"} for n in range(21)], split=True)
    assert response.json()["detail"]["code"] == "invalid_body"
    assert ha.requests == []
    assert job_count(reader) == before


def test_the_logs_never_hold_the_url(
    api_client: TestClient,
    reader: TestUser,
    ha: CardServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    fetching(monkeypatch)
    caplog.set_level("DEBUG")
    url = ha.serve("/api/camera_proxy/camera.kitchen", photo((80, 60)))
    post(api_client, reader, {"url": f"{url}?token={SECRET}"})
    post(api_client, reader, {"url": f"{ha.base}/missing?token={SECRET}"})
    fetching(monkeypatch, URL_ALLOW_HOSTS="")
    post(api_client, reader, {"url": f"{url}?token={SECRET}"})

    assert "127.0.0.1" in caplog.text  # the host is named
    assert SECRET not in caplog.text
    assert "camera_proxy" not in caplog.text
