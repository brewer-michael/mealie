"""
Which AI providers count as local, for "keep recipe cards on this server" (docs/ai/PHASE2.md §10).

A provider is local only when both hold: a manager switched on `runs_locally` ("Runs on my network"), and its base URL
is set and every address it resolves to is non-public (loopback, RFC 1918, link-local, CGNAT/Tailscale, as
`safehttp.transport.is_blocked_ip` defines them). An empty base URL is never local: the OpenAI SDK then reads
`OPENAI_BASE_URL`, and Claude's default is api.anthropic.com. The address check alone can't tell a LAN proxy to a cloud
API from a local model, and the flag alone could be a mistake.

Address lookups block and are cached for 60 seconds; call these from a worker thread, never the event loop.

That check decides which providers a local-only call may use. The connection itself is checked again when it opens:
under a local-only policy the provider SDKs get `private_http_client`, which looks the host up itself, refuses unless
every address is non-public, and connects to the address it checked. So a DNS change or rebinding between the cached
check and the SDK's own lookup can't send the call to a public address, and no proxy is used.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from mealie.core.root_logger import get_logger
from mealie.pkgs.safehttp.transport import is_blocked_ip
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.schema.recipe_ingest import LocalReadiness, ReaderInfo

if TYPE_CHECKING:
    import httpcore2
    import httpx2

    from mealie.services.openai.openai import OpenAIService

logger = get_logger(__name__)

ADDRESS_CACHE_SECONDS = 60

_cache_lock = threading.Lock()
_private_hosts: dict[str, tuple[float, bool]] = {}
"""Host name to (when the answer expires, whether every address it resolves to is private)"""


def clear_address_cache() -> None:
    with _cache_lock:
        _private_hosts.clear()


def _resolves_privately(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError, UnicodeError:
        return False  # unknown hosts are never local

    addresses = set()
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]  # drop an IPv6 zone
        try:
            addresses.add(ipaddress.ip_address(address))
        except ValueError:
            return False

    return bool(addresses) and all(is_blocked_ip(address) for address in addresses)


def host_is_private(host: str) -> bool:
    """Whether every address `host` resolves to is non-public (cached for `ADDRESS_CACHE_SECONDS`)"""
    key = host.lower().strip("[]")
    now = time.monotonic()
    with _cache_lock:
        cached = _private_hosts.get(key)
        if cached and cached[0] > now:
            return cached[1]

    private = _resolves_privately(key)
    with _cache_lock:
        _private_hosts[key] = (now + ADDRESS_CACHE_SECONDS, private)
    return private


def is_local_provider(provider: AIProviderOut) -> bool:
    """Whether a provider may be used for local-only work: marked as running locally, at a private address"""
    if not provider.runs_locally or not provider.base_url:
        return False

    try:
        host = urlsplit(provider.base_url).hostname
    except ValueError:
        return False

    if not host:
        return False
    return host_is_private(host)


def _candidates(service: OpenAIService, slot: AIProviderSlot) -> list[AIProviderOut]:
    """The slot's providers under the current policy; none when the slot isn't set up or nothing is allowed"""
    from mealie.services.openai.openai import OpenAINotEnabledException

    from .errors import AIProviderLimitReachedError, AIProviderLocalOnlyError

    try:
        return service.runtime.candidates(slot)
    except OpenAINotEnabledException, AIProviderLimitReachedError, AIProviderLocalOnlyError:
        return []


def local_readiness(service: OpenAIService) -> LocalReadiness:
    """
    For group managers setting up local-only cards: the local providers each slot a card uses would try, and the
    providers marked as running locally whose address isn't private, which local-only cards won't use.
    """
    from .policy import ai_call_policy

    with ai_call_policy(local_only=True):
        slots = {
            slot: [provider.name for provider in _candidates(service, slot)]
            for slot in (AIProviderSlot.image, AIProviderSlot.default, AIProviderSlot.fast)
        }

    flagged = [provider for provider in service.repos.group_ai_providers.get_all() if provider.runs_locally]
    return LocalReadiness(
        image=slots[AIProviderSlot.image],
        default=slots[AIProviderSlot.default],
        fast=slots[AIProviderSlot.fast],
        not_private=sorted(provider.name for provider in flagged if not is_local_provider(provider)),
    )


def card_reader(service: OpenAIService, *, local_only: bool) -> ReaderInfo | None:
    """
    The first provider reading a card would use under this policy, for the privacy chip: the image slot's first
    provider, else (with OCR available) the default slot's, reading Tesseract's text. None when cards can't be read
    that way, because the default slot has no allowed provider or there's neither an image provider nor OCR.
    """
    from mealie.services import ocr

    from .policy import ai_call_policy

    with ai_call_policy(local_only=local_only):
        default = _candidates(service, AIProviderSlot.default)
        if not default:
            return None

        if image := _candidates(service, AIProviderSlot.image):
            return ReaderInfo(name=image[0].name, local=is_local_provider(image[0]), via_ocr=False)

    if ocr.is_available():
        return ReaderInfo(name=default[0].name, local=is_local_provider(default[0]), via_ocr=True)

    return None


# ==========================================
# Connecting to the address checked (local-only calls)


class AddressNotPrivateError(OSError):
    """A local-only connection was refused: its host resolves to a public address, or doesn't resolve"""


def checked_private_addresses(host: str, port: int) -> list[str]:
    """
    The addresses to connect to for `host`, looked up now (not cached): every one is non-public, or
    `AddressNotPrivateError` is raised. An IP literal is checked as it is; an IPv6 zone is kept for connecting but
    not checked. Blocks; call it from a worker thread.
    """
    host = host.strip("[]")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None

    if literal is not None:
        if not is_blocked_ip(literal):
            raise AddressNotPrivateError(f"{literal} is a public address")
        return [host]

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        raise AddressNotPrivateError(f"{host} could not be looked up") from e

    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise AddressNotPrivateError(f"{host} has no address")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone
        except ValueError:
            raise AddressNotPrivateError(f"{host} resolves to an address that can't be checked") from None
        if not is_blocked_ip(ip):
            raise AddressNotPrivateError(f"{host} resolves to a public address ({ip})")

    return addresses


class _PrivateOnlyNetwork:
    """
    The network side of `private_http_client`'s connections (an `httpcore2.AsyncNetworkBackend`). Each new connection
    looks its host up (`checked_private_addresses`) and connects to a checked address, trying them in order as anyio
    does. TLS still runs against the host name: httpcore passes it to `start_tls` for SNI and the certificate check,
    and the request keeps its Host header.
    """

    def __init__(self, provider_name: str) -> None:
        import httpcore2

        self._provider_name = provider_name
        self._network = httpcore2.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        import anyio
        import httpcore2

        try:
            with anyio.fail_after(timeout):
                addresses = await anyio.to_thread.run_sync(
                    checked_private_addresses, host, port, abandon_on_cancel=True
                )
        except TimeoutError as e:
            raise httpcore2.ConnectTimeout(f"Looking up {host} timed out") from e
        except AddressNotPrivateError as e:
            logger.warning(f"AI provider '{self._provider_name}': refused to connect for a local-only call ({e})")
            raise httpcore2.ConnectError(str(e)) from e

        for address in addresses[:-1]:
            try:
                return await self._network.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except httpcore2.ConnectError, httpcore2.ConnectTimeout:
                continue  # the next address, as anyio's own `connect_tcp` would

        return await self._network.connect_tcp(
            addresses[-1], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        import httpcore2

        raise httpcore2.ConnectError("Local-only calls don't connect through Unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self._network.sleep(seconds)


def private_http_client(provider: AIProviderOut) -> httpx2.AsyncClient:
    """
    An HTTP client for a provider SDK (`http_client=`, both SDKs use httpx2) whose connections go only to non-public
    addresses, checked when each one opens (`_PrivateOnlyNetwork`), and never through a proxy from the environment.
    Otherwise as the SDKs' own default client: certificates as `SSL_CERT_FILE` says, redirects followed (each new
    host checked the same way). The SDK client closes it with itself (`close_client`).
    """
    import httpcore2
    import httpx2

    ssl_context = httpx2.create_ssl_context()
    limits = httpx2.Limits(max_connections=1000, max_keepalive_connections=100)
    transport = httpx2.AsyncHTTPTransport(verify=ssl_context, limits=limits, trust_env=False)
    # httpx can't be given a network backend; its transport is a thin wrapper of this connection pool
    transport._pool = httpcore2.AsyncConnectionPool(
        ssl_context=ssl_context,
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
        network_backend=cast("httpcore2.AsyncNetworkBackend", _PrivateOnlyNetwork(provider.name)),
    )
    return httpx2.AsyncClient(transport=transport, trust_env=False, timeout=provider.timeout, follow_redirects=True)


def local_only_http_client(provider: AIProviderOut) -> httpx2.AsyncClient | None:
    """
    The `http_client` to build a provider SDK client with: `private_http_client` under a local-only call policy, else
    None (the SDK's own client)
    """
    from .policy import current_policy, is_local_only

    return private_http_client(provider) if is_local_only(current_policy()) else None
