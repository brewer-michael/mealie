"""
Image URLs in the upload API (`fetch_url`, docs/ai/PHASE2.md §1.2): off by default, only public addresses unless
allowed, redirects checked hop by hop, the size cap from the header and the stream, the deadline, and nothing of the URL
but its host in the logs. No network: hostnames resolve through a patched `getaddrinfo`, and responses are served by an
httpx `MockTransport` behind safehttp's own address checks.
"""

import asyncio
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest

from mealie.pkgs.safehttp import AsyncSafeTransport
from mealie.schema.recipe_ingest import IngestRejectReason
from mealie.services.ai.ingest import fetch_url, limits
from mealie.services.ai.ingest.fetch_url import FetchedImage, fetch_image, url_filename
from mealie.services.ai.ingest.settings import IngestSettings

ADDRESSES = {
    "camera.example": "93.184.216.34",  # public
    "other.example": "93.184.216.35",
    "ha.local": "192.168.1.20",  # private
    "evil.example": "10.0.0.5",  # a public name that resolves to a private address
}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000


@pytest.fixture(autouse=True)
def addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list:
        if host not in ADDRESSES:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ADDRESSES[host], port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    configured = IngestSettings(**{"URL_FETCH": True, "WORKER": False, **values})
    monkeypatch.setattr(fetch_url, "get_ingest_settings", lambda: configured)


class Served:
    """What the fake server saw, and the transport serving it behind safehttp's checks"""

    def __init__(self, handler: Callable[[httpx.Request], Any]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def transport(self, allow_hosts: list[str], deny_hosts: list[str], timeout: int) -> httpx.AsyncBaseTransport:
        safe = AsyncSafeTransport(allow_hosts=allow_hosts, deny_hosts=deny_hosts, timeout=timeout)
        mock = httpx.MockTransport(self.serve)

        class Checked(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                safe._validate(request)  # the address checks the real transport makes before connecting
                return await mock.handle_async_request(request)

            async def aclose(self) -> None:
                await safe.aclose()

        return Checked()

    async def serve(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.handler(request)
        if asyncio.iscoroutine(response):
            response = await response
        return response


@pytest.fixture()
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[httpx.Request], Any]], Served]:
    def install(handler: Callable[[httpx.Request], Any]) -> Served:
        served = Served(handler)
        monkeypatch.setattr(fetch_url, "_transport", served.transport)
        return served

    return install


def fetch(url: str, **kwargs: Any) -> FetchedImage | IngestRejectReason:
    return asyncio.run(fetch_image(url, **kwargs))


def body_of(result: FetchedImage | IngestRejectReason) -> bytes:
    assert isinstance(result, FetchedImage), result
    try:
        return result.file.read()
    finally:
        result.file.close()


# ==========================================
# Off by default


def test_urls_are_refused_while_fetching_is_off(serve, monkeypatch: pytest.MonkeyPatch):
    served = serve(lambda request: httpx.Response(200, content=JPEG))
    monkeypatch.setattr(fetch_url, "get_ingest_settings", lambda: IngestSettings(WORKER=False))
    assert fetch("http://camera.example/card.jpg") == IngestRejectReason.url_not_allowed
    assert served.requests == []  # nothing was fetched


# ==========================================
# Where a URL may lead


def test_a_public_image_is_fetched(serve, monkeypatch: pytest.MonkeyPatch):
    settings(monkeypatch)
    served = serve(lambda request: httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"}))
    result = fetch("https://camera.example/snapshots/card.jpg?token=secret")
    assert isinstance(result, FetchedImage) and result.size == len(JPEG)
    assert body_of(result) == JPEG
    [request] = served.requests
    assert request.headers["accept"].startswith("image/")
    assert "cookie" not in request.headers and "authorization" not in request.headers


@pytest.mark.parametrize(
    "url",
    [
        "http://ha.local:8123/local/card.jpg",
        "http://evil.example/card.jpg",
        "http://127.0.0.1/card.jpg",
        "http://[::1]/card.jpg",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_a_private_address_is_refused_unless_allowed(serve, monkeypatch: pytest.MonkeyPatch, url: str):
    settings(monkeypatch)
    served = serve(lambda request: httpx.Response(200, content=JPEG))
    assert fetch(url) == IngestRejectReason.url_not_allowed
    assert served.requests == []


@pytest.mark.parametrize("allowed", ["ha.local", "192.168.1.0/24", "192.168.1.20"])
def test_home_assistants_address_can_be_allowed(serve, monkeypatch: pytest.MonkeyPatch, allowed: str):
    settings(monkeypatch, URL_ALLOW_HOSTS=f"other.lan, {allowed}")
    serve(lambda request: httpx.Response(200, content=JPEG))
    assert body_of(fetch("http://ha.local:8123/local/card.jpg")) == JPEG


def test_the_global_allow_and_disallow_lists_apply(serve, monkeypatch: pytest.MonkeyPatch):
    from mealie.core.config import get_app_settings

    app_settings = get_app_settings()
    settings(monkeypatch)
    serve(lambda request: httpx.Response(200, content=JPEG))
    monkeypatch.setattr(app_settings, "HTTP_ALLOW_LIST", "ha.local")
    assert body_of(fetch("http://ha.local/card.jpg")) == JPEG

    monkeypatch.setattr(app_settings, "HTTP_DISALLOW_LIST", "camera.example")
    assert fetch("http://camera.example/card.jpg") == IngestRejectReason.url_not_allowed


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://camera.example/card.jpg",
        "gopher://camera.example/card.jpg",
        "http://user:password@camera.example/card.jpg",
        "http://:secret@camera.example/card.jpg",
        "not a url at all",
        "",
    ],
)
def test_only_plain_http_urls_are_fetched(serve, monkeypatch: pytest.MonkeyPatch, url: str):
    settings(monkeypatch)
    served = serve(lambda request: httpx.Response(200, content=JPEG))
    assert fetch(url) == IngestRejectReason.url_not_allowed
    assert served.requests == []


# ==========================================
# Redirects


def redirecting(*locations: str, set_cookie: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """A server that answers each request in turn with a redirect to the next location, then the image"""
    remaining = list(locations)

    def handler(request: httpx.Request) -> httpx.Response:
        if remaining:
            headers = {"Location": remaining.pop(0)}
            if set_cookie:
                headers["Set-Cookie"] = set_cookie
            return httpx.Response(302, headers=headers)
        return httpx.Response(200, content=JPEG)

    return handler


def test_redirects_are_followed_and_checked_hop_by_hop(serve, monkeypatch: pytest.MonkeyPatch):
    settings(monkeypatch)
    served = serve(redirecting("https://other.example/b.jpg", "/c.jpg", set_cookie="session=abc; Path=/"))
    assert body_of(fetch("https://camera.example/a.jpg")) == JPEG
    assert [str(request.url) for request in served.requests] == [
        "https://camera.example/a.jpg",
        "https://other.example/b.jpg",
        "https://other.example/c.jpg",
    ]
    assert all("cookie" not in request.headers for request in served.requests)  # a hop's cookie isn't sent back


@pytest.mark.parametrize(
    "location",
    ["http://ha.local/card.jpg", "http://evil.example/card.jpg", "http://127.0.0.1:9000/api/admin"],
)
def test_a_redirect_to_a_private_address_is_refused(serve, monkeypatch: pytest.MonkeyPatch, location: str):
    settings(monkeypatch)
    served = serve(redirecting(location))
    assert fetch("http://camera.example/card.jpg") == IngestRejectReason.url_not_allowed
    assert len(served.requests) == 1  # the private address was never asked


@pytest.mark.parametrize(
    "start, location",
    [
        ("http://camera.example/a.jpg", "file:///etc/passwd"),
        ("http://camera.example/a.jpg", "ftp://camera.example/a.jpg"),
        ("https://camera.example/a.jpg", "http://camera.example/a.jpg"),  # a downgrade
    ],
)
def test_a_redirect_off_https_or_off_http_is_refused(serve, monkeypatch: pytest.MonkeyPatch, start: str, location: str):
    settings(monkeypatch)
    served = serve(redirecting(location))
    assert fetch(start) == IngestRejectReason.url_not_allowed
    assert len(served.requests) == 1


def test_more_than_three_redirects_fail(serve, monkeypatch: pytest.MonkeyPatch):
    settings(monkeypatch)
    serve(redirecting("/1", "/2", "/3", "/4"))
    assert fetch("http://camera.example/0") == IngestRejectReason.url_fetch_failed

    serve(redirecting("/1", "/2", "/3"))
    assert body_of(fetch("http://camera.example/0")) == JPEG


# ==========================================
# The body


def test_a_declared_length_over_the_cap_is_refused_unread(serve, monkeypatch: pytest.MonkeyPatch):
    settings(monkeypatch)
    pulled: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for _ in range(100):
            pulled.append(1)
            yield b"\0" * 1024

    serve(lambda request: httpx.Response(200, content=body(), headers={"Content-Length": str(100 * 1024)}))
    assert fetch("http://camera.example/huge.jpg", max_bytes=10 * 1024) == IngestRejectReason.too_large
    assert pulled == []


def test_a_streamed_body_over_the_cap_stops_being_read(serve, monkeypatch: pytest.MonkeyPatch):
    settings(monkeypatch)
    pulled: list[int] = []

    async def endless() -> AsyncIterator[bytes]:
        while True:
            pulled.append(1)
            yield b"\0" * 1024

    serve(lambda request: httpx.Response(200, content=endless()))  # no Content-Length
    assert fetch("http://camera.example/stream.jpg", max_bytes=10 * 1024) == IngestRejectReason.too_large
    assert len(pulled) <= 12


def test_the_default_cap_is_a_files(monkeypatch: pytest.MonkeyPatch, serve):
    settings(monkeypatch)
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 1000)
    serve(lambda request: httpx.Response(200, content=JPEG))
    assert asyncio.run(fetch_image("http://camera.example/a.jpg", max_bytes=limits.MAX_FILE_BYTES)) == (
        IngestRejectReason.too_large
    )


@pytest.mark.parametrize("status", [404, 403, 500, 503, 304])
def test_an_http_error_fails(serve, monkeypatch: pytest.MonkeyPatch, status: int):
    settings(monkeypatch)
    serve(lambda request: httpx.Response(status, content=b"nope"))
    assert fetch("http://camera.example/card.jpg") == IngestRejectReason.url_fetch_failed


def test_a_slow_server_fails_at_the_deadline(serve, monkeypatch: pytest.MonkeyPatch):
    settings(monkeypatch, URL_TIMEOUT=1)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, content=JPEG)

    serve(slow)
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        assert loop.run_until_complete(fetch_image("http://camera.example/card.jpg")) == (
            IngestRejectReason.url_fetch_failed
        )
        assert loop.time() - started < 5
    finally:
        loop.close()


def test_a_network_error_fails(serve, monkeypatch: pytest.MonkeyPatch):
    settings(monkeypatch)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    serve(unreachable)
    assert fetch("http://camera.example/card.jpg") == IngestRejectReason.url_fetch_failed
    assert fetch("http://nowhere.example/card.jpg") == IngestRejectReason.url_not_allowed  # doesn't resolve


# ==========================================
# Names and logs


@pytest.mark.parametrize(
    "url, name",
    [
        ("http://ha.local:8123/api/camera_proxy/camera.kitchen?token=abc123", "camera.kitchen"),
        ("http://ha.local/local/card%20one.jpg#frag", "card one.jpg"),
        ("http://ha.local/local/cards/", "cards"),
        ("http://ha.local/", None),
        ("http://ha.local", None),
        ("::not a url::", None),
    ],
)
def test_the_name_is_the_last_path_segment(url: str, name: str | None):
    assert url_filename(url) == name


@pytest.fixture()
def logged(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    import logging

    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = Collect(level=logging.DEBUG)
    loggers = [logging.getLogger("httpx"), logging.getLogger("httpcore"), fetch_url.logger]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    yield lines
    for logger, level in zip(loggers, levels, strict=True):
        logger.removeHandler(handler)
        logger.setLevel(level)


def test_logs_name_the_host_only(serve, monkeypatch: pytest.MonkeyPatch, logged: list[str]):
    settings(monkeypatch)
    secret = "token=s3cr3t-camera-token"
    serve(lambda request: httpx.Response(200, content=JPEG))
    body_of(fetch(f"https://camera.example/api/camera_proxy/camera.kitchen?{secret}"))
    serve(lambda request: httpx.Response(404))
    fetch(f"https://camera.example/api/camera_proxy/camera.kitchen?{secret}")
    fetch(f"http://ha.local/api/camera_proxy/camera.kitchen?{secret}")
    serve(redirecting(f"http://ha.local/x?{secret}"))
    fetch(f"https://camera.example/start?{secret}")

    assert any("camera.example" in line for line in logged)
    assert not any("s3cr3t" in line or "camera_proxy" in line for line in logged), logged

    # httpx itself still logs other requests
    httpx_logger = __import__("logging").getLogger("httpx")
    httpx_logger.info("HTTP Request: GET http://elsewhere/")
    assert logged[-1] == "HTTP Request: GET http://elsewhere/"
