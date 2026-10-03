"""
The server's own URLs, as clients reach it, and redirect URI rules (docs/ai/PHASE3.md §4).

Every endpoint, the metadata documents and the MCP endpoint derive `<origin>` with `request_origin`, so the
issuer, the resource and the URLs in `WWW-Authenticate` always agree.
"""

import ipaddress
import re
from urllib.parse import SplitResult, quote, urlencode, urlsplit

from starlette.datastructures import Headers
from starlette.types import Scope

from mealie.core.config import get_app_settings

MCP_PATH = "/api/mcp"
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"
OAUTH_PATH = "/api/oauth"
CONSENT_PAGE_PATH = "/oauth/consent"
"""The SPA's consent page"""

_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST = re.compile(r"(?:[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?")
"""A Host header worth echoing back (lowercase): a DNS name or an IP literal, and an optional port"""

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
"""Hosts whose `http` redirect URIs match on any port (RFC 8252 §7.3)"""

_LAN_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".localhost")
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
"""RFC 6598, e.g. Tailscale; not `is_private` in Python"""

MAX_REDIRECT_URI_LENGTH = 2000


# ==========================================
# This server's URLs


def _without_default_port(scheme: str, host: str) -> str:
    port = f":{_DEFAULT_PORTS.get(scheme)}"
    return host.removesuffix(port) if host.endswith(port) and not host.endswith("]") else host


def request_origin(scope: Scope) -> str:
    """
    `scheme://host[:port]` as the client reached this server: the ASGI scope's scheme (uvicorn has already applied
    `X-Forwarded-Proto` from trusted proxies) and the `Host` header, lowercased and without a default port. A
    `Host` that isn't a plain host name or IP literal falls back to the address the server listens on.

    A plain `http` request for an `https` `BASE_URL`'s host and port gets `https` (see `_base_url_origin`).
    """
    scheme = str(scope.get("scheme") or "http").lower()
    host = Headers(scope=scope).get("host", "").strip().lower()

    if not _HOST.fullmatch(host):
        server = scope.get("server")
        if server and server[0]:
            address, port = server[0], server[1]
            address = f"[{address}]" if ":" in address else address
            host = f"{address}:{port}" if port else address
        else:
            host = "localhost"

    return _base_url_origin(scheme, host) or f"{scheme}://{_without_default_port(scheme, host)}"


def _base_url_origin(scheme: str, host: str) -> str | None:
    """
    An `https` `BASE_URL`'s origin for a plain `http` request that names its host and port (or no port, as a TLS
    proxy forwarding from 443 sends it). Only ever an upgrade: a request that arrived over TLS keeps `https`.

    Behind a TLS proxy that uvicorn doesn't trust (it trusts `X-Forwarded-Proto` only from `HOST_IP`, which the
    Docker image sets to the container's gateway, not to a proxy container's address), the scope says `http` while
    clients use `https://`. OAuth clients compare the resource with the URL they were given as an exact string, so
    the scheme has to be right.
    """
    settings = get_app_settings()
    if scheme != "http" or settings.is_default_base_url:
        return None

    base = _split(settings.BASE_URL)
    if base is None or base.scheme != "https" or not base.hostname or "@" in base.netloc:
        return None
    request = _split(f"{scheme}://{host}")
    if request is None or request.hostname != base.hostname:
        return None

    base_port = base.port or _DEFAULT_PORTS[base.scheme]
    if (request.port or _DEFAULT_PORTS[base.scheme]) != base_port:
        return None

    return f"{base.scheme}://{_without_default_port(base.scheme, base.netloc.lower())}"


def mcp_url(origin: str) -> str:
    """The MCP endpoint's URL: the resource (RFC 8707) every token is bound to"""
    return origin + MCP_PATH


def protected_resource_metadata_url(origin: str) -> str:
    """RFC 9728 §3.1: the well-known path inserted before the resource's path"""
    return origin + PROTECTED_RESOURCE_METADATA_PATH + MCP_PATH


def canonical_resource(url: str) -> str | None:
    """
    `url` compared as a resource (RFC 8707 §2): lowercase scheme and host, no default port, no trailing slash.
    `None` if it isn't an absolute http(s) URL without a fragment (RFC 8707 §2 forbids one).
    """
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        return None

    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname or parts.fragment or "#" in url:
        return None
    if parts.username is not None or parts.password is not None:
        return None

    host = _without_default_port(parts.scheme, parts.netloc.lower())
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{host}{path}{query}"


# ==========================================
# Redirect URIs


def _split(uri: str) -> SplitResult | None:
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        return None
    return parts


def is_local_host(hostname: str) -> bool:
    """
    Whether `hostname` can only be reached on a local network: loopback, private and link-local addresses, and
    names that only resolve locally (no dot, `.local`, `.lan`, ...). It's decided from the name alone: nothing is
    resolved.
    """
    hostname = hostname.lower().rstrip(".")
    if hostname == "localhost" or ("." not in hostname and ":" not in hostname):
        return True
    if hostname.endswith(_LAN_SUFFIXES):
        return True

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False

    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or (address.version == 4 and address in _SHARED_ADDRESS_SPACE)
    )


def check_redirect_uri(uri: str) -> str:
    """
    Returns `uri` if it can be registered as a redirect URI, else raises a `ValueError` saying why:
    - absolute `http` or `https` (RFC 6749 §3.1.2), with no fragment (§3.1.2) and no user info;
    - an ASCII host: an internationalised name in its `xn--` form;
    - plain `http` only for loopback and local-network hosts: Home Assistant's own callback is often
      `http://homeassistant.local:8123/auth/external/callback`, and native apps use loopback (RFC 8252 §7.3).
    """
    if not uri or len(uri) > MAX_REDIRECT_URI_LENGTH or any(c.isspace() for c in uri):
        raise ValueError("Redirect URIs must be URLs without spaces")
    if not uri.startswith(("http://", "https://")):
        raise ValueError(f"Redirect URIs must start with https:// or http:// ({uri})")

    parts = _split(uri)
    if parts is None or not parts.hostname:
        raise ValueError(f"Redirect URIs must be absolute URLs with a host ({uri})")
    if "#" in uri:
        raise ValueError(f"Redirect URIs can't have a fragment ({uri})")
    if "@" in parts.netloc:
        raise ValueError(f"Redirect URIs can't contain a user name or password ({uri})")
    if not parts.netloc.isascii():
        # the consent page shows the host, where a lookalike letter from another script could pass for the real one
        ascii_host = _idna(parts.hostname)
        hint = f", {ascii_host}" if ascii_host else ""
        raise ValueError(f"Write the host of a redirect URI in its xn-- form{hint} ({uri})")
    if parts.scheme == "http" and not is_local_host(parts.hostname):
        raise ValueError(f"Use https://, or http:// only for a local network address ({uri})")

    return uri


def redirect_uri_matches(registered: str, requested: str) -> bool:
    """
    Exact string matching (RFC 6749 §3.1.2.3, OAuth 2.1 §2.3.1), except that a loopback `http` redirect URI
    matches on any port (RFC 8252 §7.3), since native apps such as Claude Code pick a free port each time.
    """
    if registered == requested:
        return True

    ours, theirs = _split(registered), _split(requested)
    if ours is None or theirs is None:
        return False
    if ours.scheme != "http" or theirs.scheme != "http" or ours.hostname not in LOOPBACK_HOSTS:
        return False
    if "@" in theirs.netloc or "#" in requested:
        return False

    return (ours.hostname, ours.path, ours.query) == (theirs.hostname, theirs.path, theirs.query)


def display_host(uri: str) -> str:
    """
    The host (and port) of `uri` as the consent page shows it, in ASCII: an internationalised name in its `xn--`
    form, so a lookalike letter from another script stands out. Registration refuses such hosts too.
    """
    netloc = urlsplit(uri).netloc
    if netloc.isascii():
        return netloc

    host, colon, port = netloc.partition(":")
    return (_idna(host) or quote(host, safe="")) + colon + quote(port, safe="")


def _idna(host: str) -> str | None:
    """`host` with each label in its ASCII (`xn--`) form, or `None` if it isn't a valid internationalised name"""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def with_query_params(uri: str, params: list[tuple[str, str]]) -> str:
    """
    `uri` with `params` appended to its query, keeping any query it already has (RFC 6749 §3.1.2). Built by
    appending so the registered URI comes back exactly as registered.
    """
    query = urlencode(params)
    if not query:
        return uri
    if uri.endswith(("?", "&")):
        return uri + query
    return f"{uri}{'&' if '?' in uri else '?'}{query}"
