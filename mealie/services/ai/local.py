"""
Which AI providers count as local, for "keep recipe cards on this server" (docs/ai/PHASE2.md §10).

A provider is local only when both hold: a manager switched on `runs_locally` ("Runs on my network"), and its base URL
is set and every address it resolves to is non-public (loopback, RFC 1918, link-local, CGNAT/Tailscale, as
`safehttp.transport.is_blocked_ip` defines them). An empty base URL is never local: the OpenAI SDK then reads
`OPENAI_BASE_URL`, and Claude's default is api.anthropic.com. The address check alone can't tell a LAN proxy to a cloud
API from a local model, and the flag alone could be a mistake.

Address lookups block and are cached for 60 seconds; call these from a worker thread, never the event loop.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mealie.core.root_logger import get_logger
from mealie.pkgs.safehttp.transport import is_blocked_ip
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.schema.recipe_ingest import LocalReadiness, ReaderInfo

if TYPE_CHECKING:
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
