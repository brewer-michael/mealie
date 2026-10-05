"""
Fork: `safehttp.post` (webhooks, recipe actions) reads the answer as `resilient_fetch` reads a page (decoding.py): curl
decodes nothing and stops receiving at a cap (`POST_MAX_BYTES`), and a compressed answer is decoded here within it. It
used to take the whole answer, decoded by curl: 300 KB of gzip from any household's webhook URL held 300 MB. Each
request is also given up on in time (`post_options`), however slowly the answer comes. These go over a real connection,
through curl, to a server on 127.0.0.1 (allowed), with the keyword arguments the callers pass.
"""

import functools
import gzip
import http.server
import json
import threading
import time
import tracemalloc
import zlib
from collections.abc import Iterator
from compression import zstd
from dataclasses import dataclass, field
from typing import Any

import brotli
import curl_cffi.requests.exceptions
import httpx
import pytest

from mealie.pkgs import safehttp
from mealie.pkgs.safehttp import decoding

MIB = 1024 * 1024
OK = json.dumps({"ok": True}).encode()


@dataclass
class Answer:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    repeat: int = 1
    block: int = MIB
    """The body is sent `repeat` times in blocks of this size"""
    pause: float = 0.0
    """Seconds between blocks"""


class LocalServer:
    """
    An HTTP/1.0 server answering each path with its `Answer` (without a Content-Length the body ends when the connection
    closes), and what it saw: the bodies posted, and how many body bytes it got out before the client hung up
    """

    def __init__(self) -> None:
        self.answers: dict[str, Answer] = {"/hook": Answer(body=OK)}
        self.posted: list[bytes] = []
        self.sent = 0
        served = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:
                served.posted.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                answer = served.answers[self.path]
                try:
                    self.send_response(answer.status)
                    for name, value in answer.headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    for _ in range(answer.repeat):
                        for start in range(0, len(answer.body), answer.block):
                            block = answer.body[start : start + answer.block]
                            self.wfile.write(block)
                            self.wfile.flush()
                            served.sent += len(block)
                            time.sleep(answer.pause)
                except OSError:
                    pass  # the client hung up

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str = "/hook") -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture()
def server() -> Iterator[LocalServer]:
    local = LocalServer()
    yield local
    local.close()


def post(url: str, timeout: int = 15) -> httpx.Response:
    """`safehttp.post` as the webhook publisher and the recipe-action task call it"""
    return safehttp.post(url, json={"x": 1}, timeout=timeout, allow_hosts=["127.0.0.1"], deny_hosts=[])


def post_measured(url: str, timeout: int = 15) -> tuple[Any, int, float]:
    """`post`'s response (or the exception it raised), the most memory Python objects took meanwhile, and the time"""
    started = time.monotonic()
    tracemalloc.start()
    try:
        try:
            result: Any = post(url, timeout)
        except Exception as e:
            result = e
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak, time.monotonic() - started


def sent_after_hang_up(server: LocalServer) -> int:
    time.sleep(0.3)  # the server notices the closed connection
    return server.sent


@functools.cache
def bomb(coding: str, inflated: int = 128 * MIB) -> bytes:
    """`inflated` zero bytes in `coding`, a few hundred KiB at most"""
    zeros = b"\0" * MIB
    if coding in ("gzip", "deflate"):
        compressor = zlib.compressobj(9, zlib.DEFLATED, 31 if coding == "gzip" else zlib.MAX_WBITS)
        return b"".join(compressor.compress(zeros) for _ in range(inflated // MIB)) + compressor.flush()
    if coding == "zstd":  # streamed, so within a browser's window, as a server streaming it would send it
        zcompressor = zstd.ZstdCompressor(options={zstd.CompressionParameter.window_log: 20})
        return b"".join(zcompressor.compress(zeros) for _ in range(inflated // MIB)) + zcompressor.flush()
    if coding == "br":
        bcompressor = brotli.Compressor(quality=5)
        return b"".join(bcompressor.process(zeros) for _ in range(inflated // MIB)) + bcompressor.finish()
    raise ValueError(coding)


@pytest.mark.parametrize("coding", ["gzip", "deflate", "zstd", "br"])
def test_a_bomb_answer_is_refused_at_once(server: LocalServer, coding: str):
    # a few hundred KiB that decode to 128 MiB
    server.answers["/hook"] = Answer(headers={"Content-Type": "application/json", "Content-Encoding": coding})
    server.answers["/hook"].body = bomb(coding)

    result, peak, elapsed = post_measured(server.url())

    assert isinstance(result, safehttp.ResponseTooLargeError)
    assert peak < 8 * MIB, f"{peak / MIB:.0f} MiB held while refusing a {coding} answer"
    assert elapsed < 5
    assert server.posted == [b'{"x":1}']  # the webhook itself was sent


@pytest.mark.parametrize("declared", [False, True])
def test_an_answer_over_the_cap_stops_the_transfer(server: LocalServer, declared: bool):
    # 64 MiB from a fast server: curl stops receiving at the cap, or refuses the declared length unread
    answer = Answer(body=b"\1" * MIB, repeat=64)
    if declared:
        answer.headers["Content-Length"] = str(64 * MIB)
    server.answers["/hook"] = answer

    result, peak, _ = post_measured(server.url())

    assert isinstance(result, safehttp.ResponseTooLargeError)
    assert peak < 8 * MIB, f"{peak / MIB:.0f} MiB held while refusing an answer over the cap"
    assert sent_after_hang_up(server) < 16 * MIB, f"{server.sent / MIB:.0f} MiB sent"


def test_a_redirects_body_is_never_decoded(server: LocalServer):
    """httpx reads each redirect's body whole before following it; curl inflated that too"""
    server.answers["/hook"] = Answer(307, {"Location": "/landed", "Content-Encoding": "gzip"}, bomb("gzip"))
    server.answers["/landed"] = Answer(body=OK)

    result, peak, _ = post_measured(server.url())

    assert isinstance(result, httpx.Response), result
    assert result.json() == {"ok": True}
    assert peak < 8 * MIB, f"{peak / MIB:.0f} MiB held for a redirect"


@pytest.mark.parametrize(
    ("codings", "body"),
    [
        ("", OK),
        ("identity", OK),
        ("gzip", gzip.compress(OK)),
        ("deflate", zlib.compress(OK)),
        ("zstd", zstd.compress(OK)),
        ("br", brotli.compress(OK)),
        ("gzip, br", brotli.compress(gzip.compress(OK))),
    ],
    ids=["no coding", "identity", "gzip", "deflate", "zstd", "br", "gzip then br"],
)
def test_an_answer_within_the_cap_is_read_decoded(server: LocalServer, codings: str, body: bytes):
    headers = {"Content-Type": "application/json", "Content-Encoding": codings} if codings else {}
    server.answers["/hook"] = Answer(headers=headers, body=body)

    response = post(server.url())

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert server.posted == [b'{"x":1}']


@pytest.mark.parametrize("coding", ["identity", "gzip"])
def test_an_answer_at_the_cap_is_read(server: LocalServer, coding: str):
    data = b"\2" * decoding.POST_MAX_BYTES
    server.answers["/hook"] = Answer(
        headers={"Content-Encoding": coding}, body=gzip.compress(data) if coding == "gzip" else data
    )

    response = post(server.url())

    assert response.content == data


def test_an_answer_just_over_the_cap_is_refused(server: LocalServer):
    server.answers["/hook"] = Answer(headers={"Content-Encoding": "gzip"}, body=gzip.compress(b"\2" * (MIB + 1)))

    with pytest.raises(safehttp.ResponseTooLargeError):
        post(server.url())


def test_an_error_status_still_raises_for_status(server: LocalServer):
    """What the webhook publisher looks at (`hard_fail`)"""
    server.answers["/hook"] = Answer(500, {"Content-Encoding": "gzip"}, gzip.compress(b"it broke"))

    response = post(server.url())

    assert response.status_code == 500
    assert response.text == "it broke"
    with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()


@pytest.mark.parametrize(
    ("coding", "body"),
    [
        ("compress", b"\x1f\x9d not decoded here"),
        ("gzip", b"\x1f\x8b\x08\x00 this isn't gzip"),
        ("gzip", gzip.compress(OK)[:-12]),  # cut short
    ],
    ids=["another coding", "corrupt gzip", "truncated gzip"],
)
def test_an_answer_that_doesnt_decode_is_refused(server: LocalServer, coding: str, body: bytes):
    server.answers["/hook"] = Answer(headers={"Content-Encoding": coding}, body=body)

    with pytest.raises(decoding.UnreadableEncodingError):
        post(server.url())


def test_a_trickled_answer_is_given_up_on_in_time(server: LocalServer):
    """A streamed request's timeout only limits a stall: 1 KiB every 0.25 s held the request for all 16 s"""
    server.answers["/hook"] = Answer(
        headers={"Content-Length": str(64 * 1024)}, body=b"\3" * 64 * 1024, block=1024, pause=0.25
    )

    started = time.monotonic()
    with pytest.raises(curl_cffi.requests.exceptions.Timeout):  # raised mid-body, as curl_cffi raises it
        post(server.url(), timeout=1)

    assert time.monotonic() - started < 5
