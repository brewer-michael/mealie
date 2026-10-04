"""
Image URLs in `POST /api/ai/ingest` (docs/ai/PHASE2.md §1.2), `{"images": [{"url": "http://ha.local/local/x.jpg"}]}`:
for Home Assistant's `rest_command`, which can send a URL but not a file.

**Off unless `AI_INGEST_URL_FETCH` is on**: an image URL is then refused `url_not_allowed` and nothing is fetched. When
it's on, a URL is fetched by the server, so it's held to what a server-side fetch on a user's behalf may do:
- **Only `http` and `https`**, with no `user:password@` in it; nothing of the caller's is sent (no cookies, no
  credentials, nothing from a `.netrc`), and cookies a response sets aren't kept for the next hop.
- **Only public addresses** (safehttp's `AsyncSafeTransport`, a fresh one per URL): a host resolving to a private,
  loopback or link-local address is refused unless it's in `HTTP_ALLOW_LIST` or `AI_INGEST_URL_ALLOW_HOSTS` (Home
  Assistant's address, say); `HTTP_DISALLOW_LIST` refuses whatever it names. The connection is pinned to the address
  that was checked (no DNS rebinding), TLS is verified, and no proxy from the environment is used.
- **At most `MAX_REDIRECTS` redirects**, each checked like the first, and never off http(s) or from https to http.
- **The body** is refused `too_large` from its `Content-Length`, or once the bytes received pass the cap (it isn't read
  further), and spooled like a decoded base64 image. The whole download, redirects included, has
  `AI_INGEST_URL_TIMEOUT` seconds.
- **Logs name the host only**: a Home Assistant camera proxy URL carries its access token in the query. The image's
  name is the URL's last path segment, without the query or fragment.

What comes back goes through intake like any upload, so a page that isn't an image is `unsupported_format`.
"""

import contextvars
import http.cookiejar
import logging
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast
from urllib.parse import unquote

import anyio
import httpx

from mealie.core.config import get_app_settings
from mealie.core.root_logger import get_logger
from mealie.pkgs.safehttp import AsyncSafeTransport, InvalidDomainError, acheck_redirect
from mealie.schema.recipe_ingest import IngestRejectReason

from . import limits
from .settings import get_ingest_settings

logger = get_logger(__name__)

MAX_REDIRECTS = 3
SPOOL_MAX_BYTES = 1024 * 1024
"""A downloaded image is kept in memory up to this size, then in an unnamed file in the system temp directory"""
ACCEPT = "image/*,*/*;q=0.5"

_fetching: contextvars.ContextVar[bool] = contextvars.ContextVar("ingest_url_fetching", default=False)


class _QuietWhileFetching(logging.Filter):
    """httpx logs every request's full URL at INFO; while an image URL is fetched, that line could carry a token"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _fetching.get()


_HTTPX_LOGGER = logging.getLogger("httpx")
if not any(isinstance(existing, _QuietWhileFetching) for existing in _HTTPX_LOGGER.filters):
    _HTTPX_LOGGER.addFilter(_QuietWhileFetching())


class _NoCookies(http.cookiejar.DefaultCookiePolicy):
    """A redirect's `Set-Cookie` isn't kept, so nothing is sent back on the next hop"""

    def set_ok(self, cookie: http.cookiejar.Cookie, request: object) -> bool:
        return False

    def return_ok(self, cookie: http.cookiejar.Cookie, request: object) -> bool:
        return False


@dataclass
class FetchedImage:
    file: BinaryIO
    """Spooled, positioned at the start; the caller closes it"""
    size: int


class _Refused(Exception):
    def __init__(self, reason: IngestRejectReason, why: str) -> None:
        super().__init__(why)
        self.reason = reason


def url_filename(url: str) -> str | None:
    """The URL's last path segment, decoded, without the query or fragment: the image's name; None without one"""
    try:
        path = httpx.URL(url.strip()).path
    except httpx.InvalidURL, TypeError:
        return None
    name = unquote(path.rstrip("/").rpartition("/")[2])
    return name or None


def _host(url: str) -> str:
    """What a log line may say about a URL: its host"""
    try:
        return httpx.URL(url.strip()).host or "?"
    except httpx.InvalidURL, TypeError:
        return "?"


def _transport(allow_hosts: list[str], deny_hosts: list[str], timeout: int) -> httpx.AsyncBaseTransport:
    """A fresh SSRF-checking transport for one URL (tests serve their own responses behind the same checks)"""
    return AsyncSafeTransport(allow_hosts=allow_hosts, deny_hosts=deny_hosts, timeout=timeout, verify=True)


def _check_url(url: str) -> httpx.URL:
    """The URL as fetched; `_Refused` when it isn't http(s), has no host or carries credentials"""
    try:
        parsed = httpx.URL(url.strip())
    except httpx.InvalidURL, TypeError:
        raise _Refused(IngestRejectReason.url_not_allowed, "not a URL") from None
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise _Refused(IngestRejectReason.url_not_allowed, "only http and https URLs are fetched")
    if parsed.userinfo:
        raise _Refused(IngestRejectReason.url_not_allowed, "a URL with a user name or password isn't fetched")
    return parsed


async def _download(url: httpx.URL, max_bytes: int, timeout: int) -> FetchedImage:
    app_settings = get_app_settings()
    settings = get_ingest_settings()
    transport = _transport(
        [*app_settings.http_allow_list, *settings.url_allow_hosts], app_settings.http_disallow_list, timeout
    )
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        event_hooks={"response": [acheck_redirect]},  # no redirect off http(s), or from https to http
        cookies=http.cookiejar.CookieJar(policy=_NoCookies()),  # a jar, not `httpx.Cookies`: that copies it
        trust_env=False,  # no proxy or `.netrc` credentials from the environment
        timeout=timeout,
    ) as client:
        async with client.stream("GET", url, headers={"Accept": ACCEPT}) as response:
            if not 200 <= response.status_code < 300:  # an error, or a redirect with nowhere to go
                raise _Refused(IngestRejectReason.url_fetch_failed, f"HTTP {response.status_code}")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise _Refused(IngestRejectReason.too_large, "its Content-Length is over the limit")

            spooled: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
            size = 0
            try:
                async for chunk in response.aiter_bytes():  # as received: counted before the next is read
                    size += len(chunk)
                    if size > max_bytes:
                        raise _Refused(IngestRejectReason.too_large, "its body is over the limit")
                    spooled.write(chunk)
                spooled.seek(0)
            except BaseException:
                spooled.close()
                raise
            return FetchedImage(cast(BinaryIO, spooled), size)


async def fetch_image(url: str, *, max_bytes: int = limits.MAX_FILE_BYTES) -> FetchedImage | IngestRejectReason:
    """
    Downloads one image URL, or says why not: `url_not_allowed` (fetching is off, the URL isn't plain http(s), or it
    leads somewhere it may not go), `url_fetch_failed` (network error, timeout, an HTTP error, too many redirects) or
    `too_large` (more than `max_bytes`). The caller closes the file.
    """
    settings = get_ingest_settings()
    host = _host(url)
    if not settings.URL_FETCH:
        logger.info(f"An image URL on {host} wasn't fetched: AI_INGEST_URL_FETCH is off")
        return IngestRejectReason.url_not_allowed

    token = _fetching.set(True)
    try:
        parsed = _check_url(url)
        with anyio.fail_after(settings.URL_TIMEOUT):
            return await _download(parsed, max_bytes, settings.URL_TIMEOUT)
    except _Refused as e:
        logger.info(f"An image URL on {host} wasn't fetched: {e}")
        return e.reason
    except InvalidDomainError:
        # the address (or a redirect's) isn't allowed; the transport's own message would name the whole URL
        logger.info(f"An image URL on {host} wasn't fetched: it leads to an address that isn't allowed")
        return IngestRejectReason.url_not_allowed
    except TimeoutError, httpx.TimeoutException:
        logger.info(f"An image URL on {host} wasn't fetched within {settings.URL_TIMEOUT} seconds")
        return IngestRejectReason.url_fetch_failed
    except httpx.TooManyRedirects:
        logger.info(f"An image URL on {host} wasn't fetched: more than {MAX_REDIRECTS} redirects")
        return IngestRejectReason.url_fetch_failed
    except Exception as e:
        # network errors (by type only: their messages name the URL)
        logger.info(f"An image URL on {host} wasn't fetched ({type(e).__name__})")
        return IngestRejectReason.url_fetch_failed
    finally:
        _fetching.reset(token)
