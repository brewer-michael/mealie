"""
Fork: safehttp follows redirects only between http(s) URLs, and never from https to http (redirects.py). The curl
transport fetches more than http: a redirect to `file:///etc/hostname` used to return the file.
"""

import http.server
import threading
from types import SimpleNamespace

import httpx
import pytest

from mealie.pkgs import safehttp
from mealie.pkgs.safehttp import fetch
from mealie.pkgs.safehttp import transport as safehttp_transport
from mealie.pkgs.safehttp.redirects import UnsafeRedirectError, acheck_redirect, check_redirect
from mealie.pkgs.safehttp.transport import AsyncSafeTransport, InvalidDomainError, SafeTransport

UNSAFE_TARGETS = [
    "file:///etc/passwd",
    "FILE:///etc/passwd",
    "ftp://ftp.example.com/x",
    "gopher://example.com:70/_x",
    "dict://example.com:11211/stat",
    "javascript:alert(1)",
]


class Server:
    """An `httpx.MockTransport` answering each URL from `routes` (a status and a Location) or with 200, recording what
    it was asked for"""

    def __init__(self, routes: dict[str, tuple[int, str]]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url in self.routes:
            status, location = self.routes[url]
            return httpx.Response(status, headers={"Location": location})
        return httpx.Response(200, text=f"landed on {url}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


async def follow(server: Server, url: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=server.transport(), event_hooks={"response": [acheck_redirect]}) as client:
        return await client.get(url, follow_redirects=True)


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("target", UNSAFE_TARGETS)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_off_http_is_refused(target: str, status: int):
    server = Server({"https://recipes.example/r": (status, target)})
    with pytest.raises(UnsafeRedirectError):
        await follow(server, "https://recipes.example/r")
    assert server.requested == ["https://recipes.example/r"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["http://recipes.example/r2", "http://other.example/r", "//other.example/x"])
async def test_a_redirect_from_https_to_http_is_refused(target: str):
    server = Server({"https://recipes.example/r": (302, target)})
    if target.startswith("//"):
        # scheme-relative: stays on https, so it's followed
        response = await follow(server, "https://recipes.example/r")
        assert response.url == httpx.URL("https://other.example/x")
        return
    with pytest.raises(UnsafeRedirectError):
        await follow(server, "https://recipes.example/r")
    assert server.requested == ["https://recipes.example/r"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "location", "landed"),
    [
        ("http://recipes.example/r", "https://recipes.example/r", "https://recipes.example/r"),
        ("http://recipes.example/r", "http://www.recipes.example/r", "http://www.recipes.example/r"),
        ("https://recipes.example/r", "https://cdn.example/r.jpg", "https://cdn.example/r.jpg"),
        ("https://recipes.example/a/r", "../b/r?x=1", "https://recipes.example/b/r?x=1"),
        ("https://recipes.example/r", "/s", "https://recipes.example/s"),
    ],
)
async def test_redirects_within_http_and_up_to_https_are_followed(start: str, location: str, landed: str):
    server = Server({start: (302, location)})
    response = await follow(server, start)
    assert response.status_code == 200
    assert str(response.url) == landed
    assert server.requested == [start, landed]


def test_a_response_that_isnt_a_redirect_passes():
    for response in [
        httpx.Response(200, request=httpx.Request("GET", "https://a.example")),
        httpx.Response(302, request=httpx.Request("GET", "https://a.example")),  # no Location
        httpx.Response(200, headers={"Location": "file:///x"}, request=httpx.Request("GET", "https://a.example")),
    ]:
        check_redirect(response)


def test_unsafe_redirects_are_still_refused_by_callers_of_blocked_hosts():
    """A caller that only catches `InvalidDomainError` still refuses it; the routes catch it first to say why"""
    assert issubclass(UnsafeRedirectError, InvalidDomainError)
    assert safehttp.UnsafeRedirectError is UnsafeRedirectError


@pytest.mark.parametrize(("location", "downgrade"), [("http://recipes.example/r", True), ("file:///etc/passwd", False)])
def test_a_refused_redirect_says_whether_it_was_a_downgrade(location: str, downgrade: bool):
    response = httpx.Response(
        302, headers={"Location": location}, request=httpx.Request("GET", "https://recipes.example/r")
    )
    with pytest.raises(UnsafeRedirectError) as raised:
        check_redirect(response)
    assert raised.value.downgrade is downgrade


# ---------------------------------------------------------------------------
# Where safehttp follows redirects
# ---------------------------------------------------------------------------
def _settings(**overrides) -> SimpleNamespace:
    settings = {
        "SCRAPER_PROXY_URL": None,
        "SCRAPER_PROXY_MODE": fetch.ScraperProxyMode.always,
        "SCRAPER_FLARESOLVERR_URL": None,
        "SCRAPER_FLARESOLVERR_TIMEOUT": 60,
        "http_allow_list": [],
        "http_disallow_list": [],
    }
    return SimpleNamespace(**(settings | overrides))


@pytest.mark.asyncio
async def test_resilient_fetch_refuses_a_redirect_to_a_file(monkeypatch: pytest.MonkeyPatch):
    server = Server({"https://recipes.example/r": (302, "file:///etc/passwd")})
    monkeypatch.setattr(fetch, "_build_transport", lambda impersonate, proxy=None, max_bytes=None: server.transport())
    monkeypatch.setattr(fetch, "get_app_settings", _settings)

    with pytest.raises(UnsafeRedirectError):
        await fetch.resilient_fetch("https://recipes.example/r")
    assert server.requested == ["https://recipes.example/r"]


@pytest.mark.asyncio
async def test_resilient_fetch_refuses_a_downgrade_and_follows_an_upgrade(monkeypatch: pytest.MonkeyPatch):
    server = Server(
        {
            "https://recipes.example/old": (301, "http://recipes.example/new"),
            "http://recipes.example/r": (301, "https://recipes.example/r"),
        }
    )
    monkeypatch.setattr(fetch, "_build_transport", lambda impersonate, proxy=None, max_bytes=None: server.transport())
    monkeypatch.setattr(fetch, "get_app_settings", _settings)

    with pytest.raises(UnsafeRedirectError):
        await fetch.resilient_fetch("https://recipes.example/old")

    result = await fetch.resilient_fetch("http://recipes.example/r")
    assert result is not None
    assert result.url == "https://recipes.example/r"
    assert result.text == "landed on https://recipes.example/r"


def test_post_refuses_a_redirect_to_a_file(monkeypatch: pytest.MonkeyPatch):
    server = Server({"https://hooks.example/h": (307, "file:///etc/passwd")})
    monkeypatch.setattr(safehttp_transport, "SafeTransport", lambda **kwargs: httpx.MockTransport(server.handle))

    with pytest.raises(UnsafeRedirectError):
        safehttp.post("https://hooks.example/h", json={"x": 1})
    assert server.requested == ["https://hooks.example/h"]


@pytest.mark.parametrize("url", ["file:///etc/hostname", "ftp://127.0.0.1/x", "gopher://127.0.0.1/_x"])
def test_the_transport_fetches_only_http(url: str):
    """The backstop for clients without the hook: curl would fetch these"""
    transport = SafeTransport(allow_hosts=["127.0.0.1", "localhost"])
    with pytest.raises(InvalidDomainError):
        transport.handle_request(httpx.Request("GET", url))


class _RedirectingServer:
    """A real server on 127.0.0.1 that redirects every GET to `location`"""

    def __init__(self, location: str) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _RedirectingServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


@pytest.mark.asyncio
async def test_a_real_redirect_to_a_local_file_returns_nothing(monkeypatch: pytest.MonkeyPatch):
    """The reported case, end to end through the curl transport: with the host allowed, this returned the file"""
    with _RedirectingServer("file:///etc/hostname") as server:
        monkeypatch.setattr(fetch, "get_app_settings", lambda: _settings(http_allow_list=["127.0.0.1"]))
        with pytest.raises(UnsafeRedirectError):
            await fetch.resilient_fetch(f"http://127.0.0.1:{server.port}/")

        # a client without the hook is stopped by the transport
        transport = AsyncSafeTransport(allow_hosts=["127.0.0.1"])
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(InvalidDomainError):
                await client.get(f"http://127.0.0.1:{server.port}/", follow_redirects=True)
