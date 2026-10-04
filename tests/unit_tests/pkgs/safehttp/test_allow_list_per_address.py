"""
An allowed network lets through only the addresses inside it (fork hook in `transport._validate`): a DNS answer that
mixes an allowed address with another private one is refused, so a name an attacker controls can't reach loopback,
the cloud metadata address or another LAN host by listing one allowed address beside it. This covers upstream's
`HTTP_ALLOW_LIST` and the fork's `AI_INGEST_URL_ALLOW_HOSTS`, which both feed `allow_hosts`.
"""

import asyncio
import http.server
import socket
import threading

import httpx
import pytest

from mealie.pkgs.safehttp import transport as safehttp_transport
from mealie.pkgs.safehttp.transport import AsyncSafeTransport, InvalidDomainError, SafeTransport


def _resolve_to(monkeypatch: pytest.MonkeyPatch, ips: list[str]) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in ips[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(safehttp_transport.socket, "getaddrinfo", fake_getaddrinfo)


def _validate(allow: list[str], url: str = "http://rebind.example:9000/") -> list[str] | None:
    return AsyncSafeTransport(allow_hosts=allow)._validate(httpx.Request("GET", url))


@pytest.mark.parametrize("allow", [["192.168.1.0/24"], ["192.168.1.5"]], ids=["network", "one address"])
@pytest.mark.parametrize(
    "answer",
    [["127.0.0.1", "192.168.1.5"], ["192.168.1.5", "169.254.169.254"], ["192.168.1.5", "10.0.0.1"]],
    ids=["loopback first", "metadata", "another LAN"],
)
def test_a_mixed_answer_is_refused(monkeypatch: pytest.MonkeyPatch, allow: list[str], answer: list[str]):
    _resolve_to(monkeypatch, answer)
    with pytest.raises(InvalidDomainError, match="local resource"):
        _validate(allow)


def test_a_name_whose_every_address_is_allowed_passes(monkeypatch: pytest.MonkeyPatch):
    _resolve_to(monkeypatch, ["192.168.1.5", "192.168.1.6"])
    assert _validate(["192.168.1.0/24"]) == ["rebind.example:9000:192.168.1.5,192.168.1.6"]


def test_public_addresses_beside_an_allowed_one_pass(monkeypatch: pytest.MonkeyPatch):
    _resolve_to(monkeypatch, ["93.184.216.34", "192.168.1.5"])
    assert _validate(["192.168.1.5"]) == ["rebind.example:9000:93.184.216.34,192.168.1.5"]


def test_a_mapped_address_inside_the_network_passes(monkeypatch: pytest.MonkeyPatch):
    _resolve_to(monkeypatch, ["::ffff:192.168.1.5"])
    assert _validate(["192.168.1.0/24"]) == ["rebind.example:9000:[::ffff:192.168.1.5]"]


def test_a_listed_host_name_still_allows_its_whole_answer(monkeypatch: pytest.MonkeyPatch):
    _resolve_to(monkeypatch, ["127.0.0.1", "192.168.1.5"])
    assert _validate(["rebind.example"]) == ["rebind.example:9000:127.0.0.1,192.168.1.5"]


def test_an_address_outside_the_network_is_refused(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(InvalidDomainError):
        _validate(["192.168.1.0/24"], "http://127.0.0.1:9000/")
    assert _validate(["192.168.1.0/24"], "http://192.168.1.9:9000/") is None  # a literal needs no pin


def test_the_sync_transport_refuses_a_mixed_answer_too(monkeypatch: pytest.MonkeyPatch):
    _resolve_to(monkeypatch, ["127.0.0.1", "192.168.1.5"])
    with pytest.raises(InvalidDomainError):
        SafeTransport(allow_hosts=["192.168.1.0/24"])._validate(httpx.Request("POST", "http://rebind.example/"))


class _Loopback:
    """An HTTP server on 127.0.0.1 that records the paths it was asked for"""

    def __init__(self) -> None:
        self.paths: list[str] = []
        paths = self.paths

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"secret")

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Loopback:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_nothing_reaches_loopback_through_a_mixed_answer(monkeypatch: pytest.MonkeyPatch):
    _resolve_to(monkeypatch, ["127.0.0.1", "192.168.1.5"])

    async def fetch(port: int) -> None:
        transport = AsyncSafeTransport(allow_hosts=["192.168.1.0/24"])
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(f"http://rebind.example:{port}/internal/admin")

    with _Loopback() as server:
        with pytest.raises(InvalidDomainError):
            asyncio.run(fetch(server.port))
    assert server.paths == []
