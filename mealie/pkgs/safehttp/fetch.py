import asyncio
import random
import time
import zlib
from collections.abc import AsyncIterator
from compression import zstd
from contextlib import asynccontextmanager
from dataclasses import dataclass

import brotli
import httpx
from curl_cffi import CurlECode, CurlOpt
from httpx import AsyncClient

from mealie.core.config import get_app_settings
from mealie.core.root_logger import get_logger
from mealie.core.settings.settings import ScraperProxyMode

from . import flaresolverr
from .redirects import acheck_redirect
from .transport import AsyncSafeTransport

SCRAPER_TIMEOUT = 15

# Overall wall-clock budget for a single fetch, across all impersonation attempts and backoffs.
# Bounds worst-case latency when a site repeatedly blocks or stalls us.
SCRAPER_TOTAL_TIMEOUT = 45

BROWSER_IMPERSONATIONS = [
    "chrome",
    "firefox",
    "safari",
    "edge",
]

NON_CHALLENGE_4XX = frozenset({404, 410})
RATE_LIMIT_STATUS_CODES = frozenset({429, 503})

# Substrings found in the bodies of bot-challenge/interstitial pages that are served with a
# 200 status (so status/header checks alone would treat them as a successful fetch). Kept
# specific to anti-bot infrastructure identifiers to avoid false positives on real content.
_CHALLENGE_BODY_MARKERS: tuple[bytes, ...] = (
    b"__cf_chl",
    b"cf-browser-verification",
    b"/cdn-cgi/challenge-platform",
    b"challenges.cloudflare.com",
    b"_incapsula_resource",
    b"distil_r_captcha",
    b"px-captcha",
    b"perimeterx",
    b"datadome",
)
_CHALLENGE_BODY_SAMPLE = 4096

# fork hook (docs/ai/PHASE2.md, "Changes from the design"): a body is read as the server sent it, never past a cap.
# curl decoded gzip into an unbounded queue faster than `_read_capped` counted it (1 MiB of gzip held 1 GiB in memory),
# and closing a response it stopped reading waited for (and held) the rest of the body. So curl decodes nothing and
# stops receiving at the cap, and a refused body stops the transfer. Requests keep the impersonated browser's own
# Accept-Encoding (any other value would contradict its fingerprint), and a compressed body is decoded here, never past
# the cap, for every coding a browser offers: gzip, deflate (zlib or raw), zstd and br. A caller without `max_bytes`
# gets this cap.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
IDENTITY = "identity"
_NO_CODING = frozenset({IDENTITY, "none"})
"""What curl reads as no coding at all"""
_MAX_CODINGS = 5
"""Codings applied one over the other that are decoded at most, as curl"""
ZSTD_WINDOW_LOG_MAX = 23
"""An 8 MiB zstd window at most, as browsers allow (RFC 9659 §3): a frame asking for more is refused, not allocated"""

_BASE_BACKOFF = 1.0
_MAX_BACKOFF = 5.0
_BACKOFF_JITTER = 0.5

logger = get_logger()


class ForceTimeoutException(Exception):
    """Raised when reading a response body exceeds the fetch timeout."""


class ResponseTooLargeError(Exception):
    """Raised when a response body exceeds the caller's byte budget."""


class UnreadableEncodingError(Exception):
    """Fork: a body in a coding that isn't decoded here, or that doesn't decode (corrupt, truncated, trailing data)"""


@dataclass
class FetchResult:
    """The outcome of a resilient fetch, decoupled from the (now-closed) streaming response."""

    content: bytes
    status_code: int
    url: str
    headers: httpx.Headers
    encoding: str | None

    @property
    def text(self) -> str:
        # Mirrors the decoding behavior of requests' `text` property.
        try:
            return str(self.content, self.encoding, errors="replace")  # type: ignore[arg-type]
        except LookupError, TypeError:
            # LookupError: unknown encoding name. TypeError: encoding is None.
            return str(self.content, errors="replace")


def is_challenge_status(status_code: int) -> bool:
    if status_code == 503:
        return True
    if 400 <= status_code < 500:
        return status_code not in NON_CHALLENGE_4XX
    return False


def headers_indicate_challenge(headers: httpx.Headers) -> bool:
    # Cloudflare sets `cf-mitigated: challenge` on interstitial/challenge responses,
    # which can otherwise carry a 200 status.
    return "cf-mitigated" in headers


def body_indicates_challenge(content: bytes) -> bool:
    sample = content[:_CHALLENGE_BODY_SAMPLE].lower()
    return any(marker in sample for marker in _CHALLENGE_BODY_MARKERS)


def _build_transport(
    impersonate: str, proxy: str | None = None, max_bytes: int | None = DEFAULT_MAX_BYTES
) -> AsyncSafeTransport:
    settings = get_app_settings()
    kwargs: dict = {
        "impersonate": impersonate,
        "default_headers": True,
        # disable SSL verification since we can handle untrusted data and some sites don't have certs
        # (this also covers the proxy connection, so no separate proxy-verify knob is needed)
        "verify": False,
        "allow_hosts": settings.http_allow_list,
        "deny_hosts": settings.http_disallow_list,
    }
    if proxy:
        # The transport still validates the target host, but it cannot pin the connection: curl
        # hands the hostname to the proxy, which does its own resolution. Routing egress through a
        # proxy is an explicit operator choice, so that trade-off is theirs to make.
        kwargs["proxy"] = proxy
    # fork hook (DEFAULT_MAX_BYTES): no decoding by curl, and no body past `max_bytes` (None for a HEAD, whose declared
    # length curl would refuse too). NOPROXY is set again by the transport (to "" with a proxy); options of our own
    # replace safehttp's default, which is this
    kwargs["curl_options"] = {CurlOpt.NOPROXY: "*", CurlOpt.HTTP_CONTENT_DECODING: 0}
    if max_bytes is not None:
        kwargs["curl_options"][CurlOpt.MAXFILESIZE_LARGE] = max_bytes
    return AsyncSafeTransport(**kwargs)


def _stop_transfer(resp: httpx.Response) -> None:
    """
    Fork hook (DEFAULT_MAX_BYTES): tells curl to drop the rest of a body that won't be read. Closing the response
    otherwise waits for the whole transfer, queueing every byte still to come. Other transports have nothing to stop.
    """
    curl_response = getattr(resp, "extensions", {}).get("curl", {}).get("response")
    quit_now = getattr(curl_response, "quit_now", None)
    if quit_now is not None:
        quit_now.set()


def _over_the_cap(error: BaseException) -> bool:
    """Fork hook: whether curl stopped a body at `MAXFILESIZE_LARGE`, as raised or as the transport's error for it"""
    return any(
        getattr(candidate, "code", None) == CurlECode.FILESIZE_EXCEEDED for candidate in (error, error.__cause__)
    )


class _Decoder:
    """
    Fork hook (DEFAULT_MAX_BYTES): one content coding's decoder that never makes more than `max_bytes` of output (br
    may overshoot by one of its blocks before that's noticed). `feed` decodes what has arrived; `finish` refuses a
    stream that ended early. Anything that doesn't decode is an `UnreadableEncodingError`, never a shorter body.
    """

    def __init__(self, coding: str, max_bytes: int) -> None:
        self.coding = coding
        self._max_bytes = max_bytes
        self._fed = False
        self.size = 0

    def _room(self) -> int:
        # never 0, which would mean "no limit" to zlib and zstd: past the cap it has already raised
        return self._max_bytes + 1 - self.size

    def _count(self, piece: bytes) -> bytes:
        self.size += len(piece)
        if self.size > self._max_bytes:
            raise ResponseTooLargeError(f"response body decodes past {self._max_bytes} bytes")
        return piece

    def _unreadable(self, why: object) -> UnreadableEncodingError:
        return UnreadableEncodingError(f"response body isn't valid {self.coding}: {why}")

    def feed(self, data: bytes) -> bytes:
        raise NotImplementedError

    def finish(self) -> None:
        raise NotImplementedError


def _zlib_wrapped(head: bytes) -> bool:
    """Whether a deflate body starts with a zlib (or gzip) header, rather than being raw deflate as old IIS sends"""
    if head[:2] == b"\x1f\x8b":
        return True
    cmf, flg = head[0], head[1]
    return cmf & 0x0F == 8 and cmf >> 4 <= 7 and (cmf << 8 | flg) % 31 == 0


class _ZlibDecoder(_Decoder):
    """
    gzip, x-gzip and deflate. A gzip or zlib header is read either way (as curl does); deflate without one is raw
    deflate. A gzip body may hold several members, each decoded in turn and counted toward the same cap, and may end
    in zero padding (as `gzip.decompress` reads it); anything else after the stream is refused.
    """

    _AUTO = zlib.MAX_WBITS | 32
    _GZIP = zlib.MAX_WBITS | 16

    def __init__(self, coding: str, max_bytes: int) -> None:
        super().__init__(coding, max_bytes)
        self._head = b""
        """A deflate body's first bytes, until they tell a header from raw deflate"""
        self._inflate = None if coding == "deflate" else zlib.decompressobj(self._AUTO)

    def feed(self, data: bytes) -> bytes:
        self._fed = self._fed or bool(data)
        if self._inflate is None:
            self._head += data
            if len(self._head) < 2:
                return b""
            data, self._head = self._head, b""
            self._inflate = zlib.decompressobj(self._AUTO if _zlib_wrapped(data) else -zlib.MAX_WBITS)

        out = bytearray()
        while data:
            if self._inflate.eof:
                data = self._next_member(data)
                if not data:
                    break
            try:
                piece = self._inflate.decompress(data, self._room())
            except zlib.error as e:
                raise self._unreadable(e) from e
            out += self._count(piece)
            data = self._inflate.unused_data if self._inflate.eof else self._inflate.unconsumed_tail
        return bytes(out)

    def _next_member(self, data: bytes) -> bytes:
        """What follows the end of the stream: the next gzip member (zero padding skipped), else an error"""
        if self.coding == "deflate":
            raise self._unreadable("data after the end of the stream")
        rest = data.lstrip(b"\0")
        if rest:
            self._inflate = zlib.decompressobj(self._GZIP)  # anything but a gzip member fails its header check
        return rest

    def finish(self) -> None:
        if not self._fed:
            return  # an empty body, as curl reads it
        if self._inflate is None or not self._inflate.eof:
            raise self._unreadable("the stream ends early")


class _ZstdDecoder(_Decoder):
    """zstd: each frame in turn, counted toward the same cap, with a browser's window limit"""

    def __init__(self, coding: str, max_bytes: int) -> None:
        super().__init__(coding, max_bytes)
        self._zstd = self._frame()

    @staticmethod
    def _frame() -> zstd.ZstdDecompressor:
        return zstd.ZstdDecompressor(options={zstd.DecompressionParameter.window_log_max: ZSTD_WINDOW_LOG_MAX})

    def feed(self, data: bytes) -> bytes:
        self._fed = self._fed or bool(data)
        out = bytearray()
        while True:
            if self._zstd.eof:
                data = self._zstd.unused_data + data
                if not data:
                    return bytes(out)
                self._zstd = self._frame()  # the next frame
            elif not data and self._zstd.needs_input:
                return bytes(out)
            try:
                piece = self._zstd.decompress(data, self._room())
            except zstd.ZstdError as e:
                raise self._unreadable(e) from e
            data = b""
            out += self._count(piece)

    def finish(self) -> None:
        if self._fed and not self._zstd.eof:
            raise self._unreadable("the stream ends early")


class _BrotliDecoder(_Decoder):
    """br, through brotli's output limit (`process`, then `process(b"")` until it can take more input)"""

    def __init__(self, coding: str, max_bytes: int) -> None:
        super().__init__(coding, max_bytes)
        self._brotli = brotli.Decompressor()

    def feed(self, data: bytes) -> bytes:
        self._fed = self._fed or bool(data)
        out = bytearray()
        try:
            if data:
                out += self._count(self._brotli.process(data, output_buffer_limit=self._room()))
            while not self._brotli.can_accept_more_data():
                out += self._count(self._brotli.process(b"", output_buffer_limit=self._room()))
        except brotli.error as e:  # corrupt, or data after the end of the stream
            raise self._unreadable(e) from e
        return bytes(out)

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
    """
    Fork hook (DEFAULT_MAX_BYTES): a body's content codings undone in turn, last applied first, each stage never past
    `max_bytes`
    """

    def __init__(self, codings: list[str], max_bytes: int) -> None:
        unknown = [coding for coding in codings if coding not in _DECODERS]
        if unknown or len(codings) > _MAX_CODINGS:
            raise UnreadableEncodingError(f"response body is encoded with {', '.join(codings)!r}, not decoded here")
        self._stages = [_DECODERS[coding](coding, max_bytes) for coding in reversed(codings)]

    def feed(self, data: bytes) -> bytes:
        for stage in self._stages:
            data = stage.feed(data)
        return data

    def finish(self) -> None:
        for stage in self._stages:
            stage.finish()


def _cap(max_bytes: int | None) -> int:
    """Fork hook: the most of a body that is read, `max_bytes` or DEFAULT_MAX_BYTES, whichever is less"""
    return min(max_bytes, DEFAULT_MAX_BYTES) if max_bytes is not None else DEFAULT_MAX_BYTES


def codings_of(headers: httpx.Headers) -> list[str]:
    """Fork hook: the body's content codings in the order they were applied, lower-case, none for identity"""
    codings = (coding.strip().lower() for coding in headers.get("content-encoding", "").split(","))
    return [coding for coding in codings if coding and coding not in _NO_CODING]


@asynccontextmanager
async def _stream(client: AsyncClient, method: str, url: str, timeout: int) -> AsyncIterator[httpx.Response]:
    """
    Fork hook (DEFAULT_MAX_BYTES): `client.stream`. Whatever of the body isn't read when the block ends is never
    received, and curl stopping at the cap is a `ResponseTooLargeError`.
    """
    try:
        async with client.stream(method, url, timeout=timeout, follow_redirects=True) as resp:
            try:
                yield resp
            finally:
                _stop_transfer(resp)
    except Exception as e:
        if _over_the_cap(e):
            raise ResponseTooLargeError("response body exceeds its cap") from e
        raise


async def _read_capped(resp: httpx.Response, timeout: int, max_bytes: int | None = None) -> bytes:
    """
    Reads a streaming body, aborting if it takes longer than ``timeout`` seconds or, when
    ``max_bytes`` is set, if the body grows past that many bytes.

    Mitigates abuse from URLs that serve arbitrarily large or slow content.
    """
    # fork hook (DEFAULT_MAX_BYTES): every body has a cap, and a compressed one is counted as decoded
    max_bytes = _cap(max_bytes)
    codings = codings_of(resp.headers)
    decoding = _Decoding(codings, max_bytes) if codings else None

    if max_bytes is not None:
        declared_length = resp.headers.get("content-length")
        if declared_length and declared_length.isdigit() and int(declared_length) > max_bytes:
            raise ResponseTooLargeError(f"declared content-length {declared_length} exceeds {max_bytes} bytes")

    content = bytearray()  # fork: `bytes +=` copied the whole body for every 1 KiB chunk
    start_time = time.monotonic()
    async for chunk in resp.aiter_bytes(chunk_size=1024):
        content += decoding.feed(chunk) if decoding else chunk
        if time.monotonic() - start_time > timeout:
            raise ForceTimeoutException()
        # Servers that omit or understate Content-Length are caught by the running total.
        if max_bytes is not None and len(content) > max_bytes:
            raise ResponseTooLargeError(f"response body exceeds {max_bytes} bytes")
    if decoding:
        decoding.finish()  # fork hook: a stream that ends early is refused, never returned short
    return bytes(content)


async def _sleep_backoff(retry_after: str | None, deadline: float) -> None:
    """Sleeps a jittered backoff before the next attempt, never past the overall deadline."""
    delay = _BASE_BACKOFF
    if retry_after:
        try:
            # Retry-After may be a delta-seconds integer; if it's an HTTP-date we ignore it.
            delay = max(delay, float(retry_after))
        except ValueError:
            pass

    delay = min(delay, _MAX_BACKOFF) + random.uniform(0, _BACKOFF_JITTER)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    await asyncio.sleep(min(delay, remaining))


async def _attempt(
    url: str,
    method: str,
    timeout: int,
    impersonation: str,
    read_body: bool,
    proxy: str | None,
    max_bytes: int | None = None,
) -> tuple[FetchResult | None, bool, int, str | None]:
    """
    Performs a single fetch attempt with one browser impersonation.

    Returns ``(result, blocked, status_code, retry_after)``:
    - ``result`` is set on success (and ``blocked`` is False).
    - ``blocked`` is True when the response looks like a bot challenge and rotating to another
      fingerprint may help.
    - When both ``result`` is None and ``blocked`` is False, the response was a hard error that
      rotating won't fix, and the caller should stop.
    """
    transport = _build_transport(impersonation, proxy, _cap(max_bytes) if read_body else None)  # fork: curl's cap
    # fork hook: no redirect off http(s), or from https to http (redirects.py)
    async with AsyncClient(transport=transport, event_hooks={"response": [acheck_redirect]}) as client:
        async with _stream(client, method, url, timeout) as resp:  # fork hook (DEFAULT_MAX_BYTES)
            status_code = resp.status_code
            retry_after = resp.headers.get("Retry-After")

            blocked = is_challenge_status(status_code) or headers_indicate_challenge(resp.headers)

            if blocked:
                logger.debug(f'Challenge/block detected (status={status_code}) with impersonation "{impersonation}"')
                return None, True, status_code, retry_after

            if status_code >= 400:
                # A genuine client/server error (e.g. 404, 410, 500) that a different fingerprint
                # won't resolve. Stop rotating.
                logger.debug(f'Error status code {status_code} with impersonation "{impersonation}"')
                return None, False, status_code, retry_after

            content = b""
            if read_body:
                content = await _read_capped(resp, timeout, max_bytes)
                if body_indicates_challenge(content):
                    logger.debug(f'Challenge page body detected with impersonation "{impersonation}"')
                    return None, True, status_code, retry_after

            result = FetchResult(
                content=content,
                status_code=status_code,
                url=str(resp.url),
                headers=resp.headers,
                encoding=resp.encoding,
            )
            return result, False, status_code, retry_after


async def _rotate(
    url: str,
    method: str,
    timeout: int,
    read_body: bool,
    proxy: str | None,
    deadline: float,
    max_bytes: int | None = None,
) -> tuple[FetchResult | None, bool]:
    """
    Cycles through browser impersonations (in randomized order) for a single egress path
    (direct or via ``proxy``).

    Returns ``(result, blocked)``. ``blocked`` is True only when every impersonation was rejected
    by a challenge/block -- i.e. the failure might be worth escalating (e.g. to a proxy). It is
    False on success or on a hard error, where escalation wouldn't help.
    """
    impersonations = list(BROWSER_IMPERSONATIONS)
    random.shuffle(impersonations)

    for index, impersonation in enumerate(impersonations):
        if time.monotonic() >= deadline:
            logger.debug("Scraper budget exhausted before trying all impersonations")
            break

        logger.debug(f'Trying browser impersonation: "{impersonation}"')
        try:
            result, blocked, status_code, retry_after = await _attempt(
                url, method, timeout, impersonation, read_body, proxy, max_bytes
            )
        except UnreadableEncodingError as e:  # fork hook (DEFAULT_MAX_BYTES): a hard error, as an error status is
            logger.debug(f'{e} with impersonation "{impersonation}"')
            return None, False

        if result is not None:
            return result, False

        if not blocked:
            # Hard error; rotating fingerprints won't help.
            return None, False

        is_last = index == len(impersonations) - 1
        if not is_last and status_code in RATE_LIMIT_STATUS_CODES:
            await _sleep_backoff(retry_after, deadline)

    return None, True


def _solution_to_result(solution: flaresolverr.FlareSolverrSolution) -> FetchResult:
    return FetchResult(
        content=solution.html.encode("utf-8", errors="replace"),
        status_code=solution.status_code,
        url=solution.url,
        headers=httpx.Headers({"content-type": "text/html; charset=utf-8"}),
        encoding="utf-8",
    )


async def resilient_fetch(
    url: str,
    *,
    method: str = "GET",
    timeout: int = SCRAPER_TIMEOUT,
    allow_flaresolverr: bool = True,
    max_bytes: int | None = None,
) -> FetchResult | None:
    """
    Fetches a URL while cycling through browser TLS impersonations (via httpx-curl-cffi) to
    bypass bot-detection systems that fingerprint the TLS handshake (JA3/JA4), such as Cloudflare.

    Impersonations are tried in a randomized order. On a detected challenge/block (a challenge
    status code, a ``cf-mitigated`` header, or challenge markers in an otherwise-200 body) the
    next fingerprint is tried, with a short jittered backoff for rate-limit statuses. A genuine
    error status (e.g. 404) stops the rotation immediately, since a new fingerprint won't help.

    When ``SCRAPER_PROXY_URL`` is configured, requests egress through it: in ``always`` mode every
    request uses the proxy; in ``fallback`` mode a direct attempt is made first and the proxy is
    only used to retry when every direct impersonation was blocked.

    As a last resort, if the fetch is still blocked and ``SCRAPER_FLARESOLVERR_URL`` is configured,
    the request is escalated to FlareSolverr (a headless browser). This only applies to HTML fetches
    (``allow_flaresolverr`` and a body-returning method), since FlareSolverr returns HTML, not the
    binary content an image download needs.

    The whole operation is bounded by ``SCRAPER_TOTAL_TIMEOUT``, and each attempt's body read is
    bounded by ``timeout`` seconds, to mitigate abuse from URLs that serve arbitrarily large content.
    Callers that persist what they download should also pass ``max_bytes``, which rejects a body
    over that size (raising ``ResponseTooLargeError``) rather than letting time alone bound it.

    Returns a ``FetchResult`` for the first successful response, or ``None`` if every impersonation
    was blocked, the server returned a hard error, or the budget was exhausted.
    """
    logger.debug(f"Fetching URL: {url}")

    read_body = method.upper() != "HEAD"
    deadline = time.monotonic() + SCRAPER_TOTAL_TIMEOUT

    settings = get_app_settings()
    proxy = settings.SCRAPER_PROXY_URL or None
    proxy_first = bool(proxy) and settings.SCRAPER_PROXY_MODE == ScraperProxyMode.always

    result, blocked = await _rotate(
        url, method, timeout, read_body, proxy if proxy_first else None, deadline, max_bytes
    )
    if result is not None:
        return result

    # In `fallback` mode, escalate to the proxy only when a direct attempt was blocked (not on a
    # hard error, and not if we already used the proxy above).
    if blocked and proxy and not proxy_first:
        logger.debug("Direct fetch blocked; retrying through configured proxy")
        result, blocked = await _rotate(url, method, timeout, read_body, proxy, deadline, max_bytes)
        if result is not None:
            return result

    # Final escalation: a real browser via FlareSolverr. HTML-only, and only when still blocked.
    # Note: the impersonation rotation above always runs first, so its SSRF guard (which rejects
    # private target IPs) has already vetted `url` before we hand it to FlareSolverr.
    if blocked and read_body and allow_flaresolverr and settings.SCRAPER_FLARESOLVERR_URL:
        logger.debug("Fetch still blocked; escalating to FlareSolverr")
        solution = await flaresolverr.solve(
            settings.SCRAPER_FLARESOLVERR_URL, url, settings.SCRAPER_FLARESOLVERR_TIMEOUT
        )
        if solution is not None:
            return _solution_to_result(solution)

    return result
