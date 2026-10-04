"""
Fork: safehttp reads a body as the server sent it and never past a cap (fetch.py, DEFAULT_MAX_BYTES). curl used to
inflate gzip into memory faster than `_read_capped` counted it, and closing a response it stopped reading waited for
the rest of the body. These go over a real connection, through curl, to a server on 127.0.0.1 (allowed).
"""

import gzip
import http.server
import threading
import time
import tracemalloc
import zlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from mealie.pkgs.safehttp import fetch

MIB = 1024 * 1024


class LocalServer:
    """
    An HTTP/1.0 server answering with `status`, `headers` and `body` sent `repeat` times in 1 MiB blocks, and what it
    saw: each request's method and headers, and how many body bytes it got out before the client hung up
    """

    BLOCK = MIB

    def __init__(self) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body = b""
        self.repeat = 1
        self.sent = 0
        self.requests: list[tuple[str, dict[str, str]]] = []
        served = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # without a Content-Length the body ends when the connection closes

            def _answer(self, with_body: bool) -> None:
                served.requests.append((self.command, {k.lower(): v for k, v in self.headers.items()}))
                try:
                    self.send_response(served.status)
                    for name, value in served.headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    if not with_body:
                        return
                    for _ in range(served.repeat):
                        for start in range(0, len(served.body), served.BLOCK):
                            block = served.body[start : start + served.BLOCK]
                            self.wfile.write(block)
                            served.sent += len(block)
                except OSError:
                    pass  # the client hung up

            def do_GET(self) -> None:
                self._answer(True)

            def do_HEAD(self) -> None:
                self._answer(False)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/recipe"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalServer]:
    settings = SimpleNamespace(
        SCRAPER_PROXY_URL=None,
        SCRAPER_PROXY_MODE=fetch.ScraperProxyMode.always,
        SCRAPER_FLARESOLVERR_URL=None,
        SCRAPER_FLARESOLVERR_TIMEOUT=60,
        http_allow_list=["127.0.0.1"],
        http_disallow_list=[],
    )
    monkeypatch.setattr(fetch, "get_app_settings", lambda: settings)
    local = LocalServer()
    yield local
    local.close()


def gzip_bomb(inflated: int) -> bytes:
    """gzip of `inflated` zero bytes: about 1 KiB per MiB"""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    return b"".join(compressor.compress(b"\0" * MIB) for _ in range(inflated // MIB)) + compressor.flush()


async def fetch_measured(url: str, **kwargs: Any) -> tuple[Any, int]:
    """`resilient_fetch` (or the exception it raised), and the most memory Python objects took meanwhile"""
    tracemalloc.start()
    try:
        try:
            result: Any = await fetch.resilient_fetch(url, **kwargs)
        except Exception as e:
            result = e
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


def sent_after_hang_up(server: LocalServer) -> int:
    time.sleep(0.3)  # the server notices the closed connection
    return server.sent


@pytest.mark.asyncio
async def test_a_page_is_asked_for_uncompressed_and_read(server: LocalServer):
    server.headers = {"Content-Type": "text/html; charset=utf-8"}
    server.body = b"<html>pancakes</html>"

    result = await fetch.resilient_fetch(server.url)

    assert result is not None
    assert result.content == b"<html>pancakes</html>"
    [(method, headers)] = server.requests
    assert method == "GET"
    assert headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_a_page_compressed_anyway_is_inflated(server: LocalServer):
    page = b"<html>" + b"waffles " * 10_000 + b"</html>"
    server.headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
    server.body = gzip.compress(page)

    result = await fetch.resilient_fetch(server.url)

    assert result is not None
    assert result.content == page


@pytest.mark.asyncio
async def test_a_gzip_bomb_is_never_inflated_past_the_cap(server: LocalServer):
    # 128 KiB of gzip that inflates to 128 MiB, against a 1 MiB cap
    server.headers = {"Content-Type": "image/jpeg", "Content-Encoding": "gzip"}
    server.body = gzip_bomb(128 * MIB)

    result, peak = await fetch_measured(server.url, max_bytes=MIB)

    assert isinstance(result, fetch.ResponseTooLargeError)
    assert peak < 8 * MIB, f"{peak / MIB:.0f} MiB held while refusing a gzip body"


@pytest.mark.asyncio
async def test_a_gzip_bomb_without_a_cap_stops_at_the_default(server: LocalServer):
    """A page fetch has no `max_bytes`: 128 MiB of inflated page was returned whole"""
    server.headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
    server.body = gzip_bomb(128 * MIB)

    result, peak = await fetch_measured(server.url)

    assert isinstance(result, fetch.ResponseTooLargeError)
    assert peak < fetch.DEFAULT_MAX_BYTES + 16 * MIB, f"{peak / MIB:.0f} MiB held"


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [False, True])
async def test_a_body_over_the_cap_stops_the_transfer(server: LocalServer, declared: bool):
    # 128 MiB from a fast server, against a 1 MiB cap: the refusal stops the transfer instead of waiting for the rest
    server.body = b"\1" * LocalServer.BLOCK
    server.repeat = 128
    server.headers = {"Content-Type": "image/jpeg"}
    if declared:
        server.headers["Content-Length"] = str(128 * LocalServer.BLOCK)

    result, peak = await fetch_measured(server.url, max_bytes=MIB)

    assert isinstance(result, fetch.ResponseTooLargeError)
    assert peak < 16 * MIB, f"{peak / MIB:.0f} MiB held while refusing a body over the cap"
    assert sent_after_hang_up(server) < 32 * MIB, f"{server.sent / MIB:.0f} MiB sent"


@pytest.mark.asyncio
async def test_an_error_page_isnt_received(server: LocalServer):
    """An error status ends the fetch without reading the body, which closing the response used to receive whole"""
    server.status = 404
    server.body = b"\1" * LocalServer.BLOCK
    server.repeat = 128

    result, peak = await fetch_measured(server.url)

    assert result is None
    assert peak < 16 * MIB, f"{peak / MIB:.0f} MiB held for an error page"
    assert sent_after_hang_up(server) < 32 * MIB, f"{server.sent / MIB:.0f} MiB sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["br", "zstd", "compress"])
async def test_a_body_in_another_coding_fails_like_an_error(server: LocalServer, encoding: str):
    server.headers = {"Content-Type": "text/html", "Content-Encoding": encoding}
    server.body = b"\x8b\x00not really compressed"

    assert await fetch.resilient_fetch(server.url) is None
    assert len(server.requests) == 1  # a different fingerprint gets the same body


@pytest.mark.asyncio
async def test_a_corrupt_gzip_body_fails_like_an_error(server: LocalServer):
    server.headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
    server.body = b"\x1f\x8b\x08\x00 this isn't gzip"

    assert await fetch.resilient_fetch(server.url) is None


@pytest.mark.asyncio
async def test_head_still_reads_a_large_declared_length(server: LocalServer):
    """`largest_content_len` compares image sizes with HEAD: a size over the cap is reported, not refused"""
    server.headers = {"Content-Type": "image/jpeg", "Content-Length": str(4 * fetch.DEFAULT_MAX_BYTES)}

    result = await fetch.resilient_fetch(server.url, method="HEAD")

    assert result is not None
    assert result.headers["content-length"] == str(4 * fetch.DEFAULT_MAX_BYTES)


@pytest.mark.asyncio
async def test_a_page_at_the_default_cap_is_read(server: LocalServer):
    server.headers = {"Content-Type": "text/html"}
    server.body = b"\2" * fetch.DEFAULT_MAX_BYTES

    result = await fetch.resilient_fetch(server.url)

    assert result is not None
    assert len(result.content) == fetch.DEFAULT_MAX_BYTES
