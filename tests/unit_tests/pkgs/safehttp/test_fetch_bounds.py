"""
Fork: safehttp reads a body as the server sent it and never past a cap (decoding.py, DEFAULT_MAX_BYTES). curl used to
inflate gzip into memory faster than `_read_capped` counted it, and closing a response it stopped reading waited for
the rest of the body. Requests keep the impersonated browser's Accept-Encoding, and every coding it offers is decoded
here within the cap, a slice at a time, without holding the event loop. Most of these go over a real connection,
through curl, to a server on 127.0.0.1 (allowed).
"""

import asyncio
import functools
import gzip
import http.server
import threading
import time
import tracemalloc
import zlib
from collections.abc import Callable, Iterator
from compression import zstd
from types import SimpleNamespace
from typing import Any

import brotli
import pytest

from mealie.pkgs.safehttp import decoding as decoders
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


async def fetch_beside_a_ticker(url: str, **kwargs: Any) -> tuple[Any, float, float]:
    """
    `resilient_fetch` (or the exception it raised), how long it took, and the longest the event loop was held meanwhile:
    a task beside it wakes every 10 ms and records the CPU time the loop's thread spent since its last turn (CPU time,
    so a busy test machine that runs the process less often doesn't count)
    """
    held: list[float] = []
    done = asyncio.Event()

    async def tick() -> None:
        last = time.thread_time()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.thread_time()
            held.append(now - last)
            last = now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0)
    started = time.monotonic()
    try:
        result: Any = await fetch.resilient_fetch(url, **kwargs)
    except Exception as e:
        result = e
    elapsed = time.monotonic() - started
    done.set()
    await ticker
    return result, elapsed, max(held, default=0.0)


def sent_after_hang_up(server: LocalServer) -> int:
    time.sleep(0.3)  # the server notices the closed connection
    return server.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("impersonation", fetch.BROWSER_IMPERSONATIONS)
async def test_a_page_is_asked_for_with_the_browsers_own_accept_encoding(
    server: LocalServer, impersonation: str, monkeypatch: pytest.MonkeyPatch
):
    """Any other value would contradict the browser fingerprint (bot managers score that); each coding is decoded"""
    monkeypatch.setattr(fetch, "BROWSER_IMPERSONATIONS", [impersonation])
    server.headers = {"Content-Type": "text/html; charset=utf-8"}
    server.body = b"<html>pancakes</html>"

    result = await fetch.resilient_fetch(server.url)

    assert result is not None
    assert result.content == b"<html>pancakes</html>"
    [(method, headers)] = server.requests
    assert method == "GET"
    offered = {coding.strip() for coding in headers["accept-encoding"].split(",")}
    assert {"gzip", "deflate", "br"} <= offered
    assert offered <= set(decoders._DECODERS), f"{impersonation} offers a coding that isn't decoded: {offered}"


PAGE = b"<html>" + b"waffles " * 10_000 + b"</html>"


def raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def pieces(data: bytes, count: int) -> list[bytes]:
    size = -(-len(data) // count)
    return [data[start : start + size] for start in range(0, len(data), size)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coding", "body"),
    [
        ("gzip", gzip.compress(PAGE)),
        ("x-gzip", gzip.compress(PAGE)),
        ("deflate", zlib.compress(PAGE)),
        ("deflate", raw_deflate(PAGE)),  # without a zlib header, as old IIS and PHP servers send it
        ("deflate", gzip.compress(PAGE)),  # a gzip header under deflate, which curl reads too
        ("gzip", gzip.compress(PAGE[:9000]) + gzip.compress(PAGE[9000:])),  # two members
        ("gzip", b"".join(gzip.compress(piece) for piece in pieces(PAGE, decoders._MAX_FRAMES))),
        ("gzip", gzip.compress(PAGE) + b"\0" * 512),  # zero padding
        ("zstd", zstd.compress(PAGE)),
        ("zstd", zstd.compress(PAGE[:9000]) + zstd.compress(PAGE[9000:])),  # two frames
        ("zstd", b"".join(zstd.compress(piece) for piece in pieces(PAGE, decoders._MAX_FRAMES))),
        ("br", brotli.compress(PAGE)),
        ("gzip, br", brotli.compress(gzip.compress(PAGE))),  # gzip applied first, then br
        ("none", PAGE),  # curl's name for no coding
        ("identity", PAGE),
        # the content complete, its checksum trailer missing or cut short, as curl and browsers read it
        ("gzip", gzip.compress(PAGE)[:-8]),
        ("gzip", gzip.compress(PAGE)[:-4]),
        ("x-gzip", gzip.compress(PAGE)[:-1]),
        ("deflate", zlib.compress(PAGE)[:-4]),
        ("deflate", zlib.compress(PAGE)[:-2]),
        ("deflate", gzip.compress(PAGE)[:-8]),
        ("gzip", gzip.compress(PAGE[:9000]) + gzip.compress(PAGE[9000:])[:-8]),
        ("gzip, br", brotli.compress(gzip.compress(PAGE)[:-8])),
    ],
    ids=[
        "gzip",
        "x-gzip",
        "zlib deflate",
        "raw deflate",
        "gzip as deflate",
        "gzip members",
        "gzip at most members",
        "gzip padding",
        "zstd",
        "zstd frames",
        "zstd at most frames",
        "br",
        "gzip then br",
        "none",
        "identity",
        "gzip without its trailer",
        "gzip with half its trailer",
        "gzip short of a byte",
        "zlib without its checksum",
        "zlib with half its checksum",
        "gzip as deflate without its trailer",
        "last member without its trailer",
        "gzip without its trailer, then br",
    ],
)
async def test_a_compressed_page_is_decoded(server: LocalServer, coding: str, body: bytes):
    server.headers = {"Content-Type": "text/html", "Content-Encoding": coding}
    server.body = body

    result = await fetch.resilient_fetch(server.url)

    assert result is not None
    assert result.content == PAGE


@pytest.mark.asyncio
async def test_a_gzip_bomb_is_never_inflated_past_the_cap(server: LocalServer):
    # 128 KiB of gzip that inflates to 128 MiB, against a 1 MiB cap
    server.headers = {"Content-Type": "image/jpeg", "Content-Encoding": "gzip"}
    server.body = gzip_bomb(128 * MIB)

    result, peak = await fetch_measured(server.url, max_bytes=MIB)

    assert isinstance(result, fetch.ResponseTooLargeError)
    assert peak < 8 * MIB, f"{peak / MIB:.0f} MiB held while refusing a gzip body"


@functools.cache
def bomb(coding: str, inflated: int = 128 * MIB) -> bytes:
    """`inflated` zero bytes in `coding`, a few hundred KiB at most"""
    if coding == "gzip":
        return gzip_bomb(inflated)
    if coding == "deflate":  # raw
        compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        return b"".join(compressor.compress(b"\0" * MIB) for _ in range(inflated // MIB)) + compressor.flush()
    if coding == "zstd":  # streamed, so within a browser's window, as a server streaming it would send it
        compressor = zstd.ZstdCompressor(options={zstd.CompressionParameter.window_log: 20})
        return b"".join(compressor.compress(b"\0" * MIB) for _ in range(inflated // MIB)) + compressor.flush()
    if coding == "br":
        compressor = brotli.Compressor(quality=5)
        return b"".join(compressor.process(b"\0" * MIB) for _ in range(inflated // MIB)) + compressor.finish()
    raise ValueError(coding)


@pytest.mark.asyncio
@pytest.mark.parametrize("coding", ["gzip", "deflate", "zstd", "br"])
async def test_a_bomb_in_any_coding_is_never_decoded_past_the_cap(server: LocalServer, coding: str):
    # a few hundred KiB that decode to 128 MiB, against a 1 MiB cap
    server.headers = {"Content-Type": "image/jpeg", "Content-Encoding": coding}
    server.body = bomb(coding)

    started = time.monotonic()
    result, peak = await fetch_measured(server.url, max_bytes=MIB)

    assert isinstance(result, fetch.ResponseTooLargeError)
    assert peak < 8 * MIB, f"{peak / MIB:.0f} MiB held while refusing a {coding} body"
    assert time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_a_zstd_frame_wanting_a_larger_window_than_a_browser_allows_is_refused(server: LocalServer):
    """A 16 MiB window: browsers refuse it (8 MiB at most), and it isn't allocated here"""
    server.headers = {"Content-Type": "text/html", "Content-Encoding": "zstd"}
    server.body = zstd.compress(b"\0" * (16 * MIB), options={zstd.CompressionParameter.window_log: 24})

    result, peak = await fetch_measured(server.url)

    assert result is None
    assert peak < 16 * MIB, f"{peak / MIB:.0f} MiB held"


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
@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "gzip, compress"])
async def test_a_body_in_another_coding_fails_like_an_error(server: LocalServer, encoding: str):
    server.headers = {"Content-Type": "text/html", "Content-Encoding": encoding}
    server.body = b"\x8b\x00not really compressed"

    assert await fetch.resilient_fetch(server.url) is None
    assert len(server.requests) == 1  # a different fingerprint gets the same body


def _cut(data: bytes) -> bytes:
    return data[: len(data) - 12]


def _flip(data: bytes, at: int) -> bytes:
    changed = bytearray(data)
    changed[at] ^= 1
    return bytes(changed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coding", "body"),
    [
        ("gzip", b"\x1f\x8b\x08\x00 this isn't gzip"),
        ("gzip", gzip.compress(PAGE) + b"garbage"),  # read as the first member alone, before
        ("gzip", _cut(gzip.compress(PAGE))),
        ("deflate", zlib.compress(PAGE) + b"garbage"),
        ("deflate", _cut(raw_deflate(PAGE))),
        ("zstd", _cut(zstd.compress(PAGE))),
        ("zstd", zstd.compress(PAGE) + b"garbage"),
        ("br", _cut(brotli.compress(PAGE))),
        ("br", brotli.compress(PAGE) + b"garbage"),
        # cut a byte into the deflate data: all of the page decodes but the end of its last block
        ("gzip", gzip.compress(PAGE)[:-9]),
        ("deflate", zlib.compress(PAGE)[:-5]),
        ("gzip", _flip(gzip.compress(PAGE), -5)),
        ("deflate", _flip(zlib.compress(PAGE), -1)),
        ("gzip", gzip.compress(PAGE)[:-8] + b"\1\2\3"),
        ("gzip", gzip.compress(PAGE)[:-4] + b"garbage"),
        ("gzip", gzip.compress(PAGE[:9000])[:-8] + gzip.compress(PAGE[9000:])),
        ("gzip", b"".join(gzip.compress(piece) for piece in pieces(PAGE, decoders._MAX_FRAMES + 1))),
        ("zstd", b"".join(zstd.compress(piece) for piece in pieces(PAGE, decoders._MAX_FRAMES + 1))),
    ],
    ids=[
        "corrupt gzip",
        "gzip then garbage",
        "truncated gzip",
        "deflate then garbage",
        "truncated deflate",
        "truncated zstd",
        "zstd then garbage",
        "truncated br",
        "br then garbage",
        "gzip cut into its data",
        "zlib cut into its data",
        "gzip with a wrong checksum",
        "zlib with a wrong checksum",
        "gzip with a wrong part of its trailer",
        "gzip with half its trailer, then garbage",
        "a member without its trailer, then another",
        "gzip with too many members",
        "zstd with too many frames",
    ],
)
async def test_a_body_that_doesnt_decode_fails_like_an_error(
    server: LocalServer, coding: str, body: bytes, monkeypatch: pytest.MonkeyPatch
):
    """Never a shorter page or image than was sent"""
    server.headers = {"Content-Type": "text/html", "Content-Encoding": coding}
    server.body = body
    read: list[Any] = []
    read_capped: Callable[..., Any] = fetch._read_capped

    async def recorded(*args: Any, **kwargs: Any) -> bytes:
        try:
            content = await read_capped(*args, **kwargs)
        except Exception as e:
            read.append(e)
            raise
        read.append(content)
        return content

    monkeypatch.setattr(fetch, "_read_capped", recorded)

    assert await fetch.resilient_fetch(server.url) is None
    assert [type(outcome) for outcome in read] == [decoders.UnreadableEncodingError]


EMPTY_ZSTD_FRAME = zstd.compress(b"")
SKIPPABLE_ZSTD_FRAME = (0x184D2A50).to_bytes(4, "little") + (0).to_bytes(4, "little")
EMPTY_GZIP_MEMBER = gzip.compress(b"")


@pytest.mark.parametrize(
    ("codings", "body"),
    [
        (["zstd"], EMPTY_ZSTD_FRAME * 10_000),
        (["zstd"], SKIPPABLE_ZSTD_FRAME * 10_000),
        (["gzip"], EMPTY_GZIP_MEMBER * 10_000),
        (["zstd", "gzip"], gzip.compress(EMPTY_ZSTD_FRAME * 1_000_000)),  # 9 MB of empty frames in 30 KB
    ],
    ids=["empty zstd frames", "skippable zstd frames", "empty gzip members", "gzip of empty zstd frames"],
)
def test_a_frame_flood_is_refused_at_once(codings: list[str], body: bytes):
    """Each frame costs a new decoder: tens of thousands of empty ones held the event loop for the whole timeout"""
    decoding = decoders._Decoding(codings, fetch.DEFAULT_MAX_BYTES)
    started = time.monotonic()

    with pytest.raises(decoders.UnreadableEncodingError, match="frames"):
        for start in range(0, len(body), 1024):  # as `_read_capped` feeds it
            for _ in decoding.feed(body[start : start + 1024]):
                pass

    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame", [EMPTY_ZSTD_FRAME, SKIPPABLE_ZSTD_FRAME], ids=["empty zstd frames", "skippable zstd frames"]
)
async def test_a_page_of_empty_frames_fails_at_once_without_holding_the_event_loop(server: LocalServer, frame: bytes):
    """50 MiB of 9-byte frames decode to nothing; they froze Mealie for the whole 15 s fetch timeout"""
    server.headers = {"Content-Type": "text/html", "Content-Encoding": "zstd"}
    server.body = frame * (fetch.DEFAULT_MAX_BYTES // len(frame))

    result, elapsed, held = await fetch_beside_a_ticker(server.url)

    assert result is None
    assert len(server.requests) == 1  # a hard error, as an error status is
    assert elapsed < 2
    assert held < 0.1, f"the event loop was held for {held * 1000:.0f} ms"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coding", "body"),
    [
        ("zstd", EMPTY_ZSTD_FRAME * (fetch.DEFAULT_MAX_BYTES // len(EMPTY_ZSTD_FRAME))),
        ("zstd, gzip", gzip.compress(EMPTY_ZSTD_FRAME * (fetch.DEFAULT_MAX_BYTES // len(EMPTY_ZSTD_FRAME)))),
        ("identity", b"\1" * (16 * MIB)),
    ],
    ids=["empty zstd frames", "gzip of empty zstd frames", "identity"],
)
async def test_reading_a_body_gives_the_event_loop_turns(
    server: LocalServer, monkeypatch: pytest.MonkeyPatch, coding: str, body: bytes
):
    """
    However slow a body is to decode, the loop gets a turn every `_LOOP_TURN`: here the frame cap is lifted, so the
    floods decode until the timeout
    """
    monkeypatch.setattr(decoders, "_MAX_FRAMES", 10**9)
    server.headers = {"Content-Type": "text/html", "Content-Encoding": coding}
    server.body = body

    result, _, held = await fetch_beside_a_ticker(server.url, timeout=1)

    assert isinstance(result, fetch.ForceTimeoutException) or (result is not None and result.content == body)
    assert held < 0.1, f"the event loop was held for {held * 1000:.0f} ms"


ZEROS = b"\0" * (8 * MIB)


def stored_gzip(data: bytes) -> bytes:
    """gzip that doesn't compress: as large as what it holds"""
    compressor = zlib.compressobj(0, zlib.DEFLATED, 31)
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize(
    ("codings", "body"),
    [
        (["gzip"], gzip.compress(ZEROS)),
        (["deflate"], zlib.compress(ZEROS)),
        (["deflate"], raw_deflate(ZEROS)),
        (["zstd"], zstd.compress(ZEROS)),
        (["br"], brotli.compress(ZEROS, quality=5)),
        (["zstd", "zstd"], zstd.compress(zstd.compress(ZEROS))),
        (["gzip", "br"], brotli.compress(stored_gzip(ZEROS), quality=5)),
        (["gzip", "br", "zstd"], zstd.compress(brotli.compress(gzip.compress(ZEROS), quality=5))),
    ],
    ids=["gzip", "zlib", "raw deflate", "zstd", "br", "zstd twice", "stored gzip then br", "gzip, br, zstd"],
)
@pytest.mark.parametrize("chunk", [1024, 64 * MIB], ids=["1 KiB chunks", "at once"])
def test_a_body_is_decoded_a_slice_at_a_time(codings: list[str], body: bytes, chunk: int):
    """No stage makes more than a slice per call (br: about one more of its blocks), and none of the body is lost"""
    decoding = decoders._Decoding(codings, fetch.DEFAULT_MAX_BYTES)
    decoded = bytearray()
    largest = 0
    for start in range(0, len(body), chunk):
        for piece in decoding.feed(body[start : start + chunk]):
            decoded += piece
            largest = max(largest, len(piece))
    decoding.finish()

    assert decoded == ZEROS
    assert largest <= 2 * decoders._DECODE_SLICE


@functools.cache
def at_the_default_cap(codings: str, over: bool) -> bytes:
    """A body in `codings` (applied in that order) that decodes to just under the default cap, or well past it"""
    data = b"\0" * (fetch.DEFAULT_MAX_BYTES + (16 * MIB if over else -64 * 1024))
    for coding in codings.split(", "):
        if coding == "gzip":
            data = gzip.compress(data, mtime=0)
        elif coding == "stored gzip":
            data = stored_gzip(data)
        elif coding == "zstd":
            data = zstd.compress(data)
        elif coding == "br":
            data = brotli.compress(data, quality=5)
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "codings", ["gzip, gzip", "zstd, zstd", "stored gzip, br", "gzip, br, zstd"], ids=lambda codings: codings
)
async def test_stacked_codings_hold_no_more_than_a_plain_page(server: LocalServer, codings: str):
    """
    Each stage held its whole output: a 48-byte `zstd, zstd` body held 3x the cap, and br over a stored gzip 4x. A
    page at the cap holds it twice (the body read, and the bytes returned), whatever its codings.
    """
    server.headers = {"Content-Type": "text/html", "Content-Encoding": codings.replace("stored gzip", "gzip")}
    server.body = at_the_default_cap(codings, over=False)

    result, peak = await fetch_measured(server.url)

    assert result is not None and len(result.content) == fetch.DEFAULT_MAX_BYTES - 64 * 1024
    assert peak < 2 * fetch.DEFAULT_MAX_BYTES + 16 * MIB, f"{peak / MIB:.0f} MiB held"


@pytest.mark.asyncio
@pytest.mark.parametrize("codings", ["zstd", "br", "zstd, zstd", "gzip, br, zstd"], ids=lambda codings: codings)
async def test_a_bomb_in_any_codings_is_refused_holding_about_the_cap(server: LocalServer, codings: str):
    server.headers = {"Content-Type": "text/html", "Content-Encoding": codings}
    server.body = at_the_default_cap(codings, over=True)

    result, peak = await fetch_measured(server.url)

    assert isinstance(result, fetch.ResponseTooLargeError)
    assert peak < fetch.DEFAULT_MAX_BYTES + 16 * MIB, f"{peak / MIB:.0f} MiB held"


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
