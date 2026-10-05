"""
Fork (docs/ai/PHASE2.md, "Changes from the design"): a response body is read as the server sent it, decoded here, and
never past a cap. curl decoded gzip into an unbounded queue faster than its reader counted it (1 MiB of gzip held 1 GiB
in memory), and closing a response it stopped reading waited for (and held) the rest of the body. So curl decodes
nothing and stops receiving at the cap (`undecoded`), and a body that won't be read stops the transfer
(`_stop_transfer`). Requests keep their Accept-Encoding (a fetch's is the impersonated browser's own: any other value
would contradict its fingerprint), and a compressed body is decoded here, never past the cap, for every coding a browser
offers: gzip, deflate (zlib or raw), zstd and br. It is decoded a slice at a time (`_DECODE_SLICE`).

- `resilient_fetch` (fetch.py) reads a page or image so, within `DEFAULT_MAX_BYTES` or the caller's `max_bytes`, and
  gives the event loop turns meanwhile (`_pace`).
- `post` (transport.py: webhooks, recipe actions) reads the answer so, within `POST_MAX_BYTES`, and gives up on a
  request after twice its timeout (`post_options`).

Upstream's files hold commented one-line hooks into this module.
"""

import asyncio
import time
import zlib
from collections.abc import AsyncIterator, Iterator
from compression import zstd
from contextlib import asynccontextmanager
from typing import Any

import brotli
import httpx
from curl_cffi import CurlECode, CurlOpt

IDENTITY = "identity"
_NO_CODING = frozenset({IDENTITY, "none"})
"""What curl reads as no coding at all"""
_MAX_CODINGS = 5
"""Codings applied one over the other that are decoded at most, as curl"""
ZSTD_WINDOW_LOG_MAX = 23
"""An 8 MiB zstd window at most, as browsers allow (RFC 9659 §3): a frame asking for more is refused, not allocated"""
_DECODE_SLICE = 1024 * 1024
"""
The most output one decoder call makes (br: about one more of its blocks). Each slice goes through the stages after it
and into the body before the next is made, so a few MiB are in flight past the body, never a cap's worth per stage
"""
_MAX_FRAMES = 64
"""
The zstd frames, or gzip members, one body holds at most (a real one has one, or a few): each costs a new decoder, so a
body of thousands of empty ones would spend seconds decoding nothing
"""
_LOOP_TURN = 0.01
"""Seconds a body is read and decoded at most before the event loop gets a turn"""
POST_MAX_BYTES = 1024 * 1024
"""
The most of a POST's answer that is read, as sent and as decoded (`post`): its callers, webhooks and recipe actions,
look at its status alone. A larger answer is a `ResponseTooLargeError`.
"""


class UnreadableEncodingError(Exception):
    """Fork: a body in a coding that isn't decoded here, or that doesn't decode (corrupt, truncated, trailing data)"""


def _too_large(message: str) -> Exception:
    """Upstream's `ResponseTooLargeError` (fetch.py imports this module, so it isn't imported at the top)"""
    from .fetch import ResponseTooLargeError

    return ResponseTooLargeError(message)


def undecoded(max_bytes: int | None) -> dict[CurlOpt, Any]:
    """
    curl options for a body read here: no decoding by curl, and no body past `max_bytes` (None for a HEAD, whose
    declared length curl would refuse too). NOPROXY is set again by the transport (to "" with a proxy); options of our
    own replace safehttp's default, which is this
    """
    options: dict[CurlOpt, Any] = {CurlOpt.NOPROXY: "*", CurlOpt.HTTP_CONTENT_DECODING: 0}
    if max_bytes is not None:
        options[CurlOpt.MAXFILESIZE_LARGE] = max_bytes
    return options


def _stop_transfer(resp: httpx.Response) -> None:
    """
    Tells curl to drop the rest of a body that won't be read. Closing the response otherwise waits for the whole
    transfer, queueing every byte still to come. Other transports have nothing to stop.
    """
    curl_response = getattr(resp, "extensions", {}).get("curl", {}).get("response")
    quit_now = getattr(curl_response, "quit_now", None)
    if quit_now is not None:
        quit_now.set()


def _over_the_cap(error: BaseException) -> bool:
    """Whether curl stopped a body at `MAXFILESIZE_LARGE`, as raised or as the transport's error for it"""
    return any(
        getattr(candidate, "code", None) == CurlECode.FILESIZE_EXCEEDED for candidate in (error, error.__cause__)
    )


class _Decoder:
    """
    One content coding's decoder that never makes more than `max_bytes` of output (br may overshoot by one of its blocks
    before that's noticed). `feed` decodes what has arrived, yielding each call's output (a slice, `_DECODE_SLICE`, at
    most; maybe nothing) before it makes the next; `finish` refuses a stream that ended early. Anything that doesn't
    decode is an `UnreadableEncodingError`, never a shorter body.
    """

    def __init__(self, coding: str, max_bytes: int) -> None:
        self.coding = coding
        self._max_bytes = max_bytes
        self._fed = False
        self._frames = 1
        self.size = 0

    def _limit(self) -> int:
        """
        The most output the next call may make: a slice, or the rest of the cap and a byte past it (never 0, which would
        mean "no limit" to zlib and zstd: past the cap it has already raised)
        """
        return min(self._max_bytes + 1 - self.size, _DECODE_SLICE)

    def _count(self, piece: bytes) -> bytes:
        self.size += len(piece)
        if self.size > self._max_bytes:
            raise _too_large(f"response body decodes past {self._max_bytes} bytes")
        return piece

    def _next_frame(self) -> None:
        """Counts a zstd frame or gzip member past the first: no more than `_MAX_FRAMES`"""
        self._frames += 1
        if self._frames > _MAX_FRAMES:
            raise self._unreadable(f"more than {_MAX_FRAMES} frames")

    def _unreadable(self, why: object) -> UnreadableEncodingError:
        return UnreadableEncodingError(f"response body isn't valid {self.coding}: {why}")

    def feed(self, data: bytes) -> Iterator[bytes]:
        raise NotImplementedError

    def finish(self) -> None:
        raise NotImplementedError


_GZIP_MAGIC = b"\x1f\x8b"


def _zlib_wrapped(head: bytes) -> bool:
    """Whether a deflate body starts with a zlib (or gzip) header, rather than being raw deflate as old IIS sends"""
    if head[:2] == _GZIP_MAGIC:
        return True
    cmf, flg = head[0], head[1]
    return cmf & 0x0F == 8 and cmf >> 4 <= 7 and (cmf << 8 | flg) % 31 == 0


class _ZlibDecoder(_Decoder):
    """
    gzip, x-gzip and deflate. A gzip or zlib header is read either way (as curl does); deflate without one is raw
    deflate. A gzip body may hold several members (`_MAX_FRAMES`), each decoded in turn and counted toward the same cap,
    and may end in zero padding (as `gzip.decompress` reads it); anything else after the stream is refused. A stream
    whose deflate data ended but whose checksum trailer is missing or cut short is read, as curl reads it.
    """

    _AUTO = zlib.MAX_WBITS | 32
    _GZIP = zlib.MAX_WBITS | 16

    def __init__(self, coding: str, max_bytes: int) -> None:
        super().__init__(coding, max_bytes)
        self._head = b""
        """A deflate body's first bytes, until they tell a header from raw deflate"""
        self._inflate = None if coding == "deflate" else zlib.decompressobj(self._AUTO)
        self._new_member(raw=False)

    def _new_member(self, raw: bool) -> None:
        """Starts on a member (raw deflate has no trailer to check)"""
        self._raw = raw
        self._magic = b""
        """The member's first two bytes: gzip's magic number, else a zlib header"""
        self._check = 0
        """The CRC-32 (gzip) or Adler-32 (zlib) of the member's output so far, as its trailer should hold it"""
        self._isize = 0

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._fed = self._fed or bool(data)
        if self._inflate is None:
            self._head += data
            if len(self._head) < 2:
                return
            data, self._head = self._head, b""
            self._new_member(raw=not _zlib_wrapped(data))
            self._inflate = zlib.decompressobj(-zlib.MAX_WBITS if self._raw else self._AUTO)

        while True:
            if self._inflate.eof:
                if data:
                    data = self._next_member(data)
                if not data:
                    return
            if len(self._magic) < 2:
                self._magic += data[: 2 - len(self._magic)]
                self._check = 0 if self._magic == _GZIP_MAGIC else 1  # CRC-32's start value, else Adler-32's
            limit = self._limit()
            try:
                piece = self._inflate.decompress(data, limit)
            except zlib.error as e:
                raise self._unreadable(e) from e
            data = self._inflate.unused_data if self._inflate.eof else self._inflate.unconsumed_tail
            yield self._count(self._checked(piece))
            if not data and len(piece) < limit:
                return  # all taken, and no output held back for want of room

    def _checked(self, piece: bytes) -> bytes:
        if not self._raw:
            gzip = self._magic == _GZIP_MAGIC
            self._check = zlib.crc32(piece, self._check) if gzip else zlib.adler32(piece, self._check)
            self._isize += len(piece)
        return piece

    def _next_member(self, data: bytes) -> bytes:
        """What follows the end of the stream: the next gzip member (zero padding skipped), else an error"""
        if self.coding == "deflate":
            raise self._unreadable("data after the end of the stream")
        rest = data.lstrip(b"\0")
        if rest:
            self._next_frame()
            self._inflate = zlib.decompressobj(self._GZIP)  # anything but a gzip member fails its header check
            self._new_member(raw=False)
        return rest

    def finish(self) -> None:
        if not self._fed:
            return  # an empty body, as curl reads it
        if self._inflate is None or not (self._inflate.eof or self._only_its_trailer_is_missing()):
            raise self._unreadable("the stream ends early")

    def _only_its_trailer_is_missing(self) -> bool:
        """
        Whether the member's deflate data ended, and only its trailer is missing or cut short: the trailer it should
        have, fed to a copy of the decompressor from the part of it that arrived, ends the stream. A stream cut inside
        its deflate data can't end within a trailer's length, and a part that arrived wrong fails zlib's check.
        """
        if self._inflate is None or self._raw or len(self._magic) < 2:
            return False
        if self._magic == _GZIP_MAGIC:
            trailer = self._check.to_bytes(4, "little") + (self._isize & 0xFFFFFFFF).to_bytes(4, "little")
        else:
            trailer = self._check.to_bytes(4, "big")
        for arrived in range(len(trailer)):
            probe = self._inflate.copy()
            try:
                rest = probe.decompress(trailer[arrived:])
            except zlib.error:
                continue
            if probe.eof and not rest and not probe.unused_data:
                return True
        return False


class _ZstdDecoder(_Decoder):
    """zstd: each frame in turn (`_MAX_FRAMES`), counted toward the same cap, with a browser's window limit"""

    def __init__(self, coding: str, max_bytes: int) -> None:
        super().__init__(coding, max_bytes)
        self._zstd = self._frame()

    @staticmethod
    def _frame() -> zstd.ZstdDecompressor:
        return zstd.ZstdDecompressor(options={zstd.DecompressionParameter.window_log_max: ZSTD_WINDOW_LOG_MAX})

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._fed = self._fed or bool(data)
        while True:
            if self._zstd.eof:
                data = self._zstd.unused_data + data
                if not data:
                    return
                self._next_frame()
                self._zstd = self._frame()
            elif not data and self._zstd.needs_input:
                return
            try:
                piece = self._zstd.decompress(data, self._limit())
            except zstd.ZstdError as e:
                raise self._unreadable(e) from e
            data = b""
            yield self._count(piece)

    def finish(self) -> None:
        if self._fed and not self._zstd.eof:
            raise self._unreadable("the stream ends early")


class _BrotliDecoder(_Decoder):
    """
    br, through brotli's output limit: `process`, then `process(b"")` until it makes nothing more and can take more
    input (it can take more before it has made all it holds)
    """

    def __init__(self, coding: str, max_bytes: int) -> None:
        super().__init__(coding, max_bytes)
        self._brotli = brotli.Decompressor()

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._fed = self._fed or bool(data)
        if not data:
            return
        try:
            piece = self._brotli.process(data, output_buffer_limit=self._limit())
            while True:
                yield self._count(piece)
                if not piece and self._brotli.can_accept_more_data():
                    return
                piece = self._brotli.process(b"", output_buffer_limit=self._limit())
        except brotli.error as e:  # corrupt, or data after the end of the stream
            raise self._unreadable(e) from e

    def finish(self) -> None:
        if self._fed and not self._brotli.is_finished():
            raise self._unreadable("the stream ends early")


_DECODERS: dict[str, type[_Decoder]] = {
    "gzip": _ZlibDecoder,
    "x-gzip": _ZlibDecoder,
    "deflate": _ZlibDecoder,
    "zstd": _ZstdDecoder,
    "br": _BrotliDecoder,
}


class _Decoding:
    """A body's content codings undone in turn, last applied first, each stage never past `max_bytes`"""

    def __init__(self, codings: list[str], max_bytes: int) -> None:
        unknown = [coding for coding in codings if coding not in _DECODERS]
        if unknown or len(codings) > _MAX_CODINGS:
            raise UnreadableEncodingError(f"response body is encoded with {', '.join(codings)!r}, not decoded here")
        self._stages = [_DECODERS[coding](coding, max_bytes) for coding in reversed(codings)]

    def feed(self, data: bytes) -> Iterator[bytes]:
        """
        What `data` decodes to, a slice at a time: each stage's slice goes through the stages after it before the stage
        makes the next. An empty piece follows any decoder call that leaves nothing to pass on, so the reader gets
        control back between any two calls (`_pace`).
        """
        return self._through(0, data)

    def _through(self, stage: int, data: bytes) -> Iterator[bytes]:
        if stage == len(self._stages):
            yield data
            return
        for piece in self._stages[stage].feed(data):
            if piece:
                yield from self._through(stage + 1, piece)
            else:
                yield piece

    def finish(self) -> None:
        for stage in self._stages:
            stage.finish()


def codings_of(headers: httpx.Headers) -> list[str]:
    """The body's content codings in the order they were applied, lower-case, none for identity"""
    codings = (coding.strip().lower() for coding in headers.get("content-encoding", "").split(","))
    return [coding for coding in codings if coding and coding not in _NO_CODING]


async def _pace(start_time: float, turn: float, timeout: int) -> float:
    """
    Raises upstream's ForceTimeoutException once a body has taken `timeout` to read, and gives the event loop a turn
    when it has had none for `_LOOP_TURN`: curl's queue seldom runs dry, so reading and decoding a body otherwise held
    the loop until the end. Returns when the loop last had a turn.
    """
    now = time.monotonic()
    if now - start_time > timeout:
        from .fetch import ForceTimeoutException  # fetch.py imports this module, so it isn't imported at the top

        raise ForceTimeoutException()
    if now - turn < _LOOP_TURN:
        return turn
    await asyncio.sleep(0)
    return time.monotonic()


@asynccontextmanager
async def _stream(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> AsyncIterator[httpx.Response]:
    """
    `client.stream`. Whatever of the body isn't read when the block ends is never received, and curl stopping at the cap
    is a `ResponseTooLargeError`.
    """
    try:
        async with client.stream(method, url, **kwargs) as resp:
            try:
                yield resp
            finally:
                _stop_transfer(resp)
    except Exception as e:
        if _over_the_cap(e):
            raise _too_large("response body exceeds its cap") from e
        raise


def post_options(timeout: int) -> dict[CurlOpt, Any]:
    """
    `post`'s curl options: an answer read here (`undecoded`, `POST_MAX_BYTES`), and each request (each redirect's too)
    given up on after twice its `timeout`, as curl_cffi times a request it reads whole (connect, then read).
    httpx-curl-cffi has curl_cffi stream every request, and a streamed request's timeout only limits a stall: an answer
    trickled in held the request for as long as the server liked.
    """
    return {**undecoded(POST_MAX_BYTES), CurlOpt.TIMEOUT_MS: int(2 * timeout * 1000)}


def _send(
    client: httpx.Client, method: str, url: str, *, max_bytes: int = POST_MAX_BYTES, **kwargs: Any
) -> httpx.Response:
    """
    `client.request`, its answer read as `resilient_fetch` reads a page: decoded here, never past `max_bytes`
    (`ResponseTooLargeError`, and the rest is never received), and refused when it doesn't decode
    (`UnreadableEncodingError`), never returned short. The response comes back closed, its `content` read.
    """
    try:
        with client.stream(method, url, **kwargs) as resp:
            try:
                resp._content = _read(resp, max_bytes)  # what `resp.read()` sets
            finally:
                _stop_transfer(resp)
    except Exception as e:
        if _over_the_cap(e):
            raise _too_large(f"response body exceeds {max_bytes} bytes") from e
        raise
    return resp


def _read(resp: httpx.Response, max_bytes: int) -> bytes:
    """A streamed sync response's body, decoded a slice at a time, each counted before the next is made"""
    codings = codings_of(resp.headers)
    decoding = _Decoding(codings, max_bytes) if codings else None
    content = bytearray()
    for chunk in resp.iter_bytes():
        for piece in decoding.feed(chunk) if decoding else (chunk,):
            content += piece
            if len(content) > max_bytes:
                raise _too_large(f"response body exceeds {max_bytes} bytes")
    if decoding:
        decoding.finish()  # a stream that ends early is refused, never returned short
    return bytes(content)
