"""
An allowed network vouches only for the addresses inside it (fork hook in `transport._validate`): when a DNS answer
mixes an allowed address with other private ones, those are left out of curl's pin, so a name an attacker controls
can't reach loopback, the cloud metadata address or another LAN host by listing one allowed address beside it, and a
LAN host whose answer also holds its own ULA or link-local IPv6 address is still fetched through its allowed IPv4 one.
Through a proxy, which resolves the name itself, such an answer is refused. This covers upstream's `HTTP_ALLOW_LIST`
and the fork's `AI_INGEST_URL_ALLOW_HOSTS`, which both feed `allow_hosts`.
"""

import asyncio
import http.server
import socket
import threading

import httpx
import pytest

from mealie.pkgs import safehttp
from mealie.pkgs.safehttp import transport as safehttp_transport
from mealie.pkgs.safehttp.transport import AsyncSafeTransport, InvalidDomainError, SafeTransport


def _resolve_to(monkeypatch: pytest.MonkeyPatch, ips: list[str]) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        def family(ip: str) -> socket.AddressFamily:
            return socket.AF_INET6 if ":" in ip else socket.AF_INET

        return [(family(ip), socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(safehttp_transport.socket, "getaddrinfo", fake_getaddrinfo)


def _validate(allow: list[str], url: str = "http://rebind.example:9000/", **kwargs) -> list[str] | None:
    return AsyncSafeTransport(allow_hosts=allow, **kwargs)._validate(httpx.Request("GET", url))


@pytest.mark.parametrize("allow", [["192.168.1.0/24"], ["192.168.1.5"]], ids=["network", "one address"])
@pytest.mark.parametrize(
    "answer",
    [["127.0.0.1", "192.168.1.5"], ["192.168.1.5", "169.254.169.254"], ["192.168.1.5", "10.0.0.1", "::1"]],
    ids=["loopback first", "metadata", "another LAN"],
)
def test_a_mixed_answer_is_pinned_to_the_allowed_address_alone(
    monkeypatch: pytest.MonkeyPatch, allow: list[str], answer: list[str]
):
    _resolve_to(monkeypatch, answer)
    assert _validate(allow) == ["rebind.example:9000:192.168.1.5"]


@pytest.mark.parametrize("allow", [["192.168.1.0/24"], ["192.168.1.20"]], ids=["network", "one address"])
@pytest.mark.parametrize(
    "answer",
    [["192.168.1.20", "fd00::20"], ["fe80::20%eth0", "192.168.1.20"], ["fd12:3456:789a::20", "192.168.1.20"]],
    ids=["ULA", "link-local first", "ULA first"],
)
def test_a_dual_stack_lan_host_is_fetched_through_its_allowed_ipv4_address(
    monkeypatch: pytest.MonkeyPatch, allow: list[str], answer: list[str]
):
    """OpenWrt's dnsmasq (and a FRITZ!Box with ULA on) answer a `.lan` name with its ULA or link-local AAAA too"""
    _resolve_to(monkeypatch, answer)
    assert _validate(allow, "http://homeassistant.lan:8123/api/webhook/x") == ["homeassistant.lan:8123:192.168.1.20"]


def test_public_addresses_stay_in_the_pin(monkeypatch: pytest.MonkeyPatch):
    _resolve_to(monkeypatch, ["93.184.216.34", "127.0.0.1", "192.168.1.5", "2606:2800:220:1::1"])
    assert _validate(["192.168.1.5"]) == ["rebind.example:9000:93.184.216.34,192.168.1.5,[2606:2800:220:1::1]"]


@pytest.mark.parametrize(
    "answer", [["127.0.0.1", "10.0.0.1"], ["fd00::20"], ["127.0.0.1", "93.184.216.34"]], ids=["v4", "v6", "public"]
)
def test_an_answer_without_an_allowed_address_is_refused(monkeypatch: pytest.MonkeyPatch, answer: list[str]):
    """Nothing in it is vouched for: any private address refuses it, as upstream did"""
    _resolve_to(monkeypatch, answer)
    with pytest.raises(InvalidDomainError, match="local resource"):
        _validate(["192.168.1.0/24"])


def test_through_a_proxy_a_mixed_answer_is_refused(monkeypatch: pytest.MonkeyPatch):
    """The proxy resolves the name itself and could pick the address left out of the pin"""
    _resolve_to(monkeypatch, ["192.168.1.20", "fd00::20"])
    with pytest.raises(InvalidDomainError, match="local resource"):
        _validate(["192.168.1.0/24"], proxy="http://proxy:8080")

    _resolve_to(monkeypatch, ["192.168.1.20", "192.168.1.21"])
    assert _validate(["192.168.1.0/24"], proxy="http://proxy:8080") == ["rebind.example:9000:192.168.1.20,192.168.1.21"]


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


def test_the_sync_transport_pins_a_mixed_answer_too(monkeypatch: pytest.MonkeyPatch):
    _resolve_to(monkeypatch, ["127.0.0.1", "192.168.1.5"])
    transport = SafeTransport(allow_hosts=["192.168.1.0/24"])
    assert transport._validate(httpx.Request("POST", "http://rebind.example/")) == ["rebind.example:80:192.168.1.5"]


class _Loopback:
    """An HTTP server on 127.0.0.1 that records the paths it was asked for"""

    def __init__(self) -> None:
        self.paths: list[str] = []
        paths = self.paths

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"secret")

            def do_GET(self) -> None:
                self._answer()

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self._answer()

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
    """
    A rebinding name answers [127.0.0.1, <allowed address>]: curl only tries the allowed one. 127.0.0.2 stands for the
    allowed LAN address here: nothing listens on it, so the fetch fails at once instead of reaching 127.0.0.1.
    """
    _resolve_to(monkeypatch, ["127.0.0.1", "127.0.0.2"])

    async def fetch(port: int) -> None:
        transport = AsyncSafeTransport(allow_hosts=["127.0.0.2"], timeout=5)
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(f"http://rebind.example:{port}/internal/admin")

    with _Loopback() as server:
        with pytest.raises(httpx.ConnectError):
            asyncio.run(fetch(server.port))
    assert server.paths == []


@pytest.mark.parametrize("ipv6", ["fd12:3456:789a::20", "fe80::20%eth0"], ids=["ULA", "link-local"])
def test_a_webhook_reaches_a_dual_stack_lan_host(monkeypatch: pytest.MonkeyPatch, ipv6: str):
    """`safehttp.post`, which webhooks and recipe actions send with; 127.0.0.1 stands for the host's allowed IPv4"""
    _resolve_to(monkeypatch, [ipv6, "127.0.0.1"])

    with _Loopback() as server:
        response = safehttp.post(
            f"http://homeassistant.lan:{server.port}/api/webhook/x", json={"x": 1}, allow_hosts=["127.0.0.0/8"]
        )

    assert response.status_code == 200
    assert server.paths == ["/api/webhook/x"]
