"""
Local-only calls connect only to the private address they checked (docs/ai/PHASE2.md §10): the provider SDKs get a
client that looks the host up when each connection opens, refuses a public address, keeps the host name for TLS and
ignores proxies from the environment. Real requests to test servers on loopback addresses; no other network.
"""

import datetime
import ipaddress
import json
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from mealie.schema.group.ai_providers import (
    AIProviderCreate,
    AIProviderOut,
    AIProviderProtocol,
    AIProviderSettingsUpdate,
    AIProviderSlot,
)
from mealie.schema.openai.general import OpenAIText
from mealie.services.ai import local
from mealie.services.ai.local import AddressNotPrivateError, checked_private_addresses
from mealie.services.ai.policy import ai_call_policy
from mealie.services.openai import OpenAIService
from tests.utils.fixture_schemas import TestUser

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")

PUBLIC_STAND_IN = ipaddress.ip_address("127.0.0.2")
"""A loopback address these tests treat as public, so that "the public host" can be a test server"""


# ==========================================
# Test servers


@dataclass
class Hit:
    path: str
    host: str | None


@dataclass
class ProviderServer:
    """Answers OpenAI chat completions and Claude messages as `name`, recording each request"""

    name: str
    address: str = "127.0.0.1"
    tls: ssl.SSLContext | None = None
    hits: list[Hit] = field(default_factory=list)
    server_names: list[str | None] = field(default_factory=list)
    """The TLS server names (SNI) clients asked for"""
    port: int = 0

    def answer(self, path: str) -> dict[str, Any] | None:
        content = json.dumps({"text": f"from {self.name}"})
        if path.endswith("/chat/completions"):
            return {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        if path.endswith("/v1/messages"):
            return {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }
        return None

    def handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                server.hits.append(Hit(path=self.path, host=self.headers.get("Host")))
                answer = server.answer(self.path)
                body = json.dumps(answer or {"error": "not found"}).encode()
                self.send_response(200 if answer else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return Handler

    @contextmanager
    def run(self) -> Iterator[ProviderServer]:
        httpd = ThreadingHTTPServer((self.address, 0), self.handler())
        httpd.daemon_threads = True
        if self.tls is not None:
            self.tls.sni_callback = lambda _socket, name, _context: self.server_names.append(name)
            httpd.socket = self.tls.wrap_socket(httpd.socket, server_side=True)
        self.port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(5)


@pytest.fixture()
def lan_server() -> Iterator[ProviderServer]:
    """The local model, on 127.0.0.1"""
    with ProviderServer("LAN").run() as server:
        yield server


@pytest.fixture()
def public_server() -> Iterator[ProviderServer]:
    """A host on "the internet" (`PUBLIC_STAND_IN`), which a local-only call must never reach"""
    with ProviderServer("Public", address=str(PUBLIC_STAND_IN)).run() as server:
        yield server


@pytest.fixture(autouse=True)
def no_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


@dataclass
class FakeDNS:
    """
    Answers lookups of the names in `answers` without a network, everything else as the system does. A name's answers
    are given in turn, the last one repeating: `["127.0.0.1"], ["127.0.0.2"]` answers the first lookup with 127.0.0.1
    and every later one with 127.0.0.2, as a rebinding DNS server might.
    """

    answers: dict[str, list[list[str]]] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)


@pytest.fixture()
def dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDNS]:
    fake = FakeDNS()
    real = socket.getaddrinfo

    def getaddrinfo(host: str | bytes | None, port: Any, *args: Any, **kwargs: Any) -> list:
        name = host.decode() if isinstance(host, bytes) else host
        if name not in fake.answers:
            return real(host, port, *args, **kwargs)

        fake.lookups.append(name)
        turns = fake.answers[name]
        addresses = turns[min(fake.lookups.count(name), len(turns)) - 1]
        port_number = int(port) if port is not None else 0
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port_number)) for address in addresses]

    # the SDKs' event loop and the local check both look names up through the socket module
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    # 127.0.0.2 stands in for a public address
    is_blocked_ip = local.is_blocked_ip
    monkeypatch.setattr(local, "is_blocked_ip", lambda ip: ip != PUBLIC_STAND_IN and is_blocked_ip(ip))
    local.clear_address_cache()
    yield fake
    local.clear_address_cache()


# ==========================================
# Providers


def create_provider(user: TestUser, name: str, base_url: str, **kwargs: Any) -> AIProviderOut:
    return user.repos.group_ai_providers.create(
        AIProviderCreate(name=name, model="m", api_key="k", base_url=base_url, runs_locally=True, **kwargs)
    )


def configure(user: TestUser, default: AIProviderOut, *routes: AIProviderOut) -> None:
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=default.id, image_provider_id=None, audio_provider_id=None),
    )
    user.repos.group_ai_provider_routes.replace_routes({AIProviderSlot.default: [p.id for p in routes]})


async def ask(user: TestUser) -> OpenAIText | None:
    return await OpenAIService(user.repos).get_response("prompt", "message", response_schema=OpenAIText)


def outcomes(user: TestUser) -> dict[str, tuple[bool, str | None]]:
    return {row.provider_name: (row.success, row.error_type) for row in user.repos.group_ai_usage.get_all()}


PROTOCOLS = [
    pytest.param(AIProviderProtocol.openai, "/v1", id="openai"),
    pytest.param(AIProviderProtocol.anthropic, "", id="anthropic"),
]


# ==========================================
# The address checked is the address used


@pytest.mark.parametrize(("protocol", "path"), PROTOCOLS)
@pytest.mark.asyncio
async def test_a_host_that_turns_public_after_the_check_is_refused(
    unique_user_fn_scoped: TestUser,
    dns: FakeDNS,
    public_server: ProviderServer,
    protocol: AIProviderProtocol,
    path: str,
):
    """DNS rebinding: private when the provider is checked, public when the SDK connects"""
    user = unique_user_fn_scoped
    dns.answers["rebind.test"] = [["127.0.0.1"], [str(PUBLIC_STAND_IN)]]
    configure(
        user, create_provider(user, "Rebinding", f"http://rebind.test:{public_server.port}{path}", protocol=protocol)
    )

    with ai_call_policy(local_only=True), pytest.raises(Exception) as e:
        await ask(user)

    assert public_server.hits == []
    assert dns.lookups.count("rebind.test") >= 2  # the cached check, then each connection
    assert any(isinstance(cause, AddressNotPrivateError) for cause in _chain(e.value))
    assert outcomes(user) == {"Rebinding": (False, "APIConnectionError")}


@pytest.mark.asyncio
async def test_a_refused_connection_hands_over_to_the_next_local_provider(
    unique_user_fn_scoped: TestUser, dns: FakeDNS, public_server: ProviderServer, lan_server: ProviderServer
):
    user = unique_user_fn_scoped
    dns.answers["rebind.test"] = [["127.0.0.1"], [str(PUBLIC_STAND_IN)]]
    rebinding = create_provider(user, "Rebinding", f"http://rebind.test:{public_server.port}/v1")
    lan = create_provider(user, "LAN", f"http://localhost:{lan_server.port}/v1")
    configure(user, rebinding, lan)

    with ai_call_policy(local_only=True):
        assert await ask(user) == OpenAIText(text="from LAN")

    assert public_server.hits == []
    assert outcomes(user) == {"Rebinding": (False, "APIConnectionError"), "LAN": (True, None)}


@pytest.mark.parametrize(("protocol", "path"), PROTOCOLS)
@pytest.mark.asyncio
async def test_a_private_host_is_reached_by_its_name(
    unique_user_fn_scoped: TestUser, lan_server: ProviderServer, protocol: AIProviderProtocol, path: str
):
    """`localhost` resolves to the loopback addresses (only 127.0.0.1 listens), and the Host header keeps the name"""
    user = unique_user_fn_scoped
    configure(user, create_provider(user, "LAN", f"http://localhost:{lan_server.port}{path}", protocol=protocol))

    with ai_call_policy(local_only=True):
        assert await ask(user) == OpenAIText(text="from LAN")

    (hit,) = lan_server.hits
    assert hit.host == f"localhost:{lan_server.port}"


@pytest.mark.asyncio
async def test_tls_checks_the_host_name_not_the_address(
    unique_user_fn_scoped: TestUser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The connection goes to 127.0.0.1, but SNI and the certificate check use `localhost`"""
    user = unique_user_fn_scoped
    ca_file, server_tls = _tls_for_localhost(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))

    with ProviderServer("TLS", tls=server_tls).run() as server:
        configure(user, create_provider(user, "TLS", f"https://localhost:{server.port}/v1"))
        with ai_call_policy(local_only=True):
            assert await ask(user) == OpenAIText(text="from TLS")

        assert server.server_names == ["localhost"]
        assert [hit.host for hit in server.hits] == [f"localhost:{server.port}"]


@pytest.mark.asyncio
async def test_local_only_calls_ignore_proxies_from_the_environment(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, lan_server: ProviderServer
):
    """A proxy would look the host up itself, and could forward the call anywhere"""
    user = unique_user_fn_scoped
    configure(user, create_provider(user, "LAN", f"http://localhost:{lan_server.port}/v1"))

    with ProviderServer("Proxy").run() as proxy:
        for name in ("HTTP_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
            monkeypatch.setenv(name.lower(), f"http://127.0.0.1:{proxy.port}")

        # without the policy the SDK's own client goes through it, as configured
        assert await ask(user) == OpenAIText(text="from Proxy")
        assert len(proxy.hits) == 1

        with ai_call_policy(local_only=True):
            assert await ask(user) == OpenAIText(text="from LAN")
        assert len(proxy.hits) == 1
        assert len(lan_server.hits) == 1


# ==========================================
# Which client the SDKs get


@pytest.mark.parametrize(("protocol", "path"), PROTOCOLS)
@pytest.mark.asyncio
async def test_only_local_only_calls_get_the_private_client_and_it_is_closed(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    lan_server: ProviderServer,
    protocol: AIProviderProtocol,
    path: str,
):
    user = unique_user_fn_scoped
    configure(user, create_provider(user, "LAN", f"http://localhost:{lan_server.port}{path}", protocol=protocol))
    made: list[Any] = []
    private_http_client = local.private_http_client

    def recording(provider: AIProviderOut) -> Any:
        made.append(client := private_http_client(provider))
        return client

    monkeypatch.setattr(local, "private_http_client", recording)

    await ask(user)
    assert made == []  # the SDK's own client

    with ai_call_policy(local_only=True):
        await ask(user)
    (client,) = made
    assert client.is_closed  # with the SDK client that used it, on the loop that used it


# ==========================================
# The connect-time check itself


@pytest.mark.parametrize(
    ("host", "addresses"),
    [
        ("127.0.0.1", ["127.0.0.1"]),
        ("[::1]", ["::1"]),
        ("fe80::1%eth0", ["fe80::1%eth0"]),  # the zone is kept for connecting, and not part of the check
        ("10.1.2.3", ["10.1.2.3"]),
        ("100.101.102.103", ["100.101.102.103"]),  # CGNAT, as Tailscale uses
    ],
)
def test_private_addresses_are_used_as_they_are(host: str, addresses: list[str]):
    assert checked_private_addresses(host, 80) == addresses


@pytest.mark.parametrize("host", ["93.184.216.34", "[2606:4700::1111]", "::ffff:8.8.8.8"])
def test_public_addresses_are_refused(host: str):
    with pytest.raises(AddressNotPrivateError):
        checked_private_addresses(host, 80)


def test_every_address_a_name_resolves_to_must_be_private(dns: FakeDNS):
    dns.answers["lan.test"] = [["192.168.1.20", "10.0.0.5"]]
    dns.answers["mixed.test"] = [["192.168.1.20", "8.8.8.8"]]

    assert checked_private_addresses("lan.test", 80) == ["192.168.1.20", "10.0.0.5"]
    with pytest.raises(AddressNotPrivateError, match="public address"):
        checked_private_addresses("mixed.test", 80)
    with pytest.raises(AddressNotPrivateError, match="could not be looked up"):
        checked_private_addresses("no-such-host.invalid", 80)


def test_each_connection_looks_the_name_up_again(dns: FakeDNS):
    """Never the 60-second cache of the provider check"""
    dns.answers["lan.test"] = [["192.168.1.20"]]
    checked_private_addresses("lan.test", 80)
    checked_private_addresses("lan.test", 80)
    assert dns.lookups == ["lan.test", "lan.test"]


# ==========================================
# Helpers


def _chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _tls_for_localhost(directory: Path) -> tuple[Path, ssl.SSLContext]:
    """A test CA's certificate file, and a server context with a certificate it signed for `localhost` only"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mealie test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_file = directory / "ca.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    chain_file = directory / "server.pem"
    chain_file.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain_file)
    return ca_file, context
