"""
Image URLs in the upload API (`fetch_url`, docs/ai/PHASE2.md §1.2): off by default, only public addresses unless
allowed, redirects checked hop by hop, the size cap from the header and the stream, no compressed bodies, the deadline
(the host's lookup included, which never holds up the event loop), and nothing of the URL but its host in the logs. No
network: hostnames resolve through a patched `getaddrinfo`, and responses are served by an httpx `MockTransport`
behind safehttp's own address checks, or (for what curl itself does with a body) by a local server on 127.0.0.1.
"""

import asyncio
import http.server
import itertools
import socket
import threading
import time
import tracemalloc
import zlib
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

    def transport(
        self, allow_hosts: list[str], deny_hosts: list[str], timeout: int, max_bytes: int
    ) -> httpx.AsyncBaseTransport:
        safe = fetch_url._OffLoopTransport(allow_hosts=allow_hosts, deny_hosts=deny_hosts, timeout=timeout)
        assert isinstance(safe, AsyncSafeTransport)
        mock = httpx.MockTransport(self.serve)

        class Checked(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                await safe.validate(request)  # the address checks the real transport makes before connecting
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


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
def test_an_encoded_body_is_refused_unread(serve, monkeypatch: pytest.MonkeyPatch, encoding: str):
    settings(monkeypatch)
    pulled: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        pulled.append(1)
        yield b"\x1f\x8b" + b"\0" * 100

    served = serve(lambda request: httpx.Response(200, content=body(), headers={"Content-Encoding": encoding}))
    assert fetch("http://camera.example/card.jpg") == IngestRejectReason.url_fetch_failed
    assert pulled == []
    [request] = served.requests
    assert request.headers["accept-encoding"] == "identity"  # it was never asked for


def test_an_identity_encoding_is_read(serve, monkeypatch: pytest.MonkeyPatch):
    settings(monkeypatch)
    serve(lambda request: httpx.Response(200, content=JPEG, headers={"Content-Encoding": "Identity"}))
    assert body_of(fetch("http://camera.example/card.jpg")) == JPEG


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


def test_a_host_that_doesnt_resolve_fails(serve, monkeypatch: pytest.MonkeyPatch):
    # a lookup that fails is a failed fetch (as a network error is), not an address that isn't allowed
    settings(monkeypatch)
    real_transport = fetch_url._transport
    serve(lambda request: httpx.Response(200, content=JPEG))
    assert fetch("http://nowhere.example/card.jpg") == IngestRejectReason.url_fetch_failed
    serve(redirecting("http://nowhere.example/card.jpg"))
    assert fetch("http://camera.example/start") == IngestRejectReason.url_fetch_failed

    monkeypatch.setattr(fetch_url, "_transport", real_transport)  # nothing is sent: the lookup fails first
    assert fetch("http://nowhere.example/card.jpg") == IngestRejectReason.url_fetch_failed


def test_a_slow_lookup_never_holds_up_the_event_loop(monkeypatch: pytest.MonkeyPatch):
    # a DNS server that answers late (glibc waits up to 30 s for one): the lookup runs in a worker thread, the event
    # loop (every other request) goes on meanwhile, and the URL's deadline covers the lookup too
    settings(monkeypatch, URL_TIMEOUT=1)
    release = threading.Event()
    resolved_on: list[str] = []

    def slow_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list:
        resolved_on.append(threading.current_thread().name)
        release.wait(10)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)

    async def main() -> tuple[FetchedImage | IngestRejectReason, float, float]:
        ticks: list[float] = []

        async def tick() -> None:
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        ticker = asyncio.create_task(tick())
        await asyncio.sleep(0.2)
        started = time.monotonic()
        result = await fetch_image("http://slow-dns.example/card.jpg")
        ended = time.monotonic()
        ticker.cancel()
        stall = max(later - earlier for earlier, later in itertools.pairwise([*ticks, ended]))
        return result, ended - started, stall

    try:
        result, elapsed, stall = asyncio.run(main())
    finally:
        release.set()
    assert result == IngestRejectReason.url_fetch_failed
    assert elapsed < 3, f"the lookup took {elapsed:.1f} s against a 1 s deadline"
    assert stall < 0.5, f"the event loop stood still for {stall:.1f} s"
    assert resolved_on and resolved_on[0] != threading.main_thread().name


# ==========================================
# The body over a real connection: curl's own transport, from a server on 127.0.0.1 (allowed)


class LocalServer:
    """
    A local HTTP/1.0 server answering every GET with `body` (sent in 1 MiB blocks), and what it saw: each request's
    headers and how many body bytes it got out before the client hung up
    """

    BLOCK = 1024 * 1024

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.body = b""
        self.repeat = 1
        self.sent = 0
        self.requests: list[dict[str, str]] = []
        served = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # no Content-Length: the body ends when the connection closes

            def do_GET(self) -> None:
                served.requests.append({name.lower(): value for name, value in self.headers.items()})
                try:
                    self.send_response(200)
                    for name, value in served.headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    for _ in range(served.repeat):
                        for start in range(0, len(served.body), served.BLOCK):
                            block = served.body[start : start + served.BLOCK]
                            self.wfile.write(block)
                            served.sent += len(block)
                except OSError:
                    pass  # the client hung up

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/card.jpg"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture()
def local_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalServer]:
    settings(monkeypatch, URL_ALLOW_HOSTS="127.0.0.1")
    server = LocalServer()
    yield server
    server.close()


def fetch_measured(url: str, max_bytes: int) -> tuple[FetchedImage | IngestRejectReason, int]:
    """`fetch`, and the most memory Python objects took meanwhile (every chunk curl hands over is one)"""
    tracemalloc.start()
    try:
        result = fetch(url, max_bytes=max_bytes)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


def test_an_image_is_fetched_over_a_real_connection(local_server: LocalServer):
    local_server.headers = {"Content-Type": "image/jpeg", "Content-Length": str(len(JPEG))}
    local_server.body = JPEG
    assert body_of(fetch(local_server.url)) == JPEG
    [request] = local_server.requests
    assert request["accept-encoding"] == "identity"


def test_a_compressed_body_is_never_inflated(local_server: LocalServer):
    # 128 KiB of gzip that would inflate to 128 MiB, against a 1 MiB cap
    inflated = 128 * limits.MIB
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    local_server.body = b"".join(compressor.compress(b"\0" * limits.MIB) for _ in range(inflated // limits.MIB))
    local_server.body += compressor.flush()
    local_server.headers = {"Content-Type": "image/jpeg", "Content-Encoding": "gzip"}

    result, peak = fetch_measured(local_server.url, limits.MIB)
    assert result == IngestRejectReason.url_fetch_failed  # an encoding it didn't ask for
    assert peak < 8 * limits.MIB, f"{peak / limits.MIB:.0f} MiB held while refusing a gzip body"


@pytest.mark.parametrize("declared", [False, True])
def test_a_body_over_the_cap_stops_the_transfer(local_server: LocalServer, declared: bool):
    # 128 MiB from a fast server, against a 1 MiB cap: curl stops receiving at the cap, and the refusal stops the
    # transfer rather than waiting for the rest of the body
    local_server.body = b"\1" * LocalServer.BLOCK
    local_server.repeat = 128
    local_server.headers = {"Content-Type": "image/jpeg"}
    if declared:
        local_server.headers["Content-Length"] = str(128 * LocalServer.BLOCK)

    result, peak = fetch_measured(local_server.url, limits.MIB)
    assert result == IngestRejectReason.too_large
    assert peak < 16 * limits.MIB, f"{peak / limits.MIB:.0f} MiB held while refusing a body over the cap"
    time.sleep(0.2)  # the server notices the closed connection
    assert local_server.sent < 32 * limits.MIB, f"{local_server.sent / limits.MIB:.0f} MiB sent"


def test_a_body_at_the_cap_is_read(local_server: LocalServer):
    data = b"\xff\xd8\xff\xe0" + b"\2" * (limits.MIB - 4)
    local_server.body = data
    assert body_of(fetch(local_server.url, max_bytes=limits.MIB)) == data


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
