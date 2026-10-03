"""The MCP authorization server's URL rules and secrets (docs/ai/PHASE3.md §4)"""

import pytest

from mealie.services.oauth.tokens import (
    format_scopes,
    hash_secret,
    is_s256_challenge,
    new_client_id,
    new_secret,
    pkce_matches,
    secret_matches,
)
from mealie.services.oauth.urls import (
    canonical_resource,
    check_redirect_uri,
    is_local_host,
    mcp_url,
    protected_resource_metadata_url,
    redirect_uri_matches,
    request_origin,
    with_query_params,
)


def _scope(host: str | None, scheme: str = "http", server: tuple[str, int] | None = ("10.0.0.2", 9000)) -> dict:
    headers = [] if host is None else [(b"host", host.encode("latin-1"))]
    return {"type": "http", "scheme": scheme, "headers": headers, "server": server, "path": "/"}


# ==========================================
# Origin


@pytest.mark.parametrize(
    "scheme, host, origin",
    [
        ("http", "mealie.lan:9925", "http://mealie.lan:9925"),
        ("http", "Mealie.LAN:9925", "http://mealie.lan:9925"),
        ("http", "mealie.lan:80", "http://mealie.lan"),
        ("https", "mealie.example.com:443", "https://mealie.example.com"),
        ("https", "mealie.example.com:4430", "https://mealie.example.com:4430"),
        # scheme comes from the ASGI scope, where uvicorn has applied X-Forwarded-Proto from trusted proxies
        ("https", "mealie.example.com", "https://mealie.example.com"),
        ("http", "192.168.1.20:9000", "http://192.168.1.20:9000"),
        ("http", "[::1]:9000", "http://[::1]:9000"),
        ("https", "[fd00::5]:443", "https://[fd00::5]"),
    ],
)
def test_request_origin(scheme: str, host: str, origin: str):
    assert request_origin(_scope(host, scheme)) == origin


@pytest.mark.parametrize("host", [None, "", 'evil"host', "a b", "host/path", "user@host", "host:port"])
def test_request_origin_falls_back_to_the_server_address(host: str | None):
    assert request_origin(_scope(host)) == "http://10.0.0.2:9000"
    assert request_origin(_scope(host, server=("::1", 9000))) == "http://[::1]:9000"
    assert request_origin(_scope(host, server=None)) == "http://localhost"


def test_server_urls():
    assert mcp_url("https://m.example") == "https://m.example/api/mcp"
    assert (
        protected_resource_metadata_url("https://m.example")
        == "https://m.example/.well-known/oauth-protected-resource/api/mcp"
    )


@pytest.mark.parametrize(
    "url, canonical",
    [
        ("http://testserver/api/mcp", "http://testserver/api/mcp"),
        ("http://testserver/api/mcp/", "http://testserver/api/mcp"),
        ("HTTP://TestServer/api/mcp", "http://testserver/api/mcp"),
        ("https://m.example:443/api/mcp", "https://m.example/api/mcp"),
        ("http://m.example:8080/api/mcp", "http://m.example:8080/api/mcp"),
        ("http://testserver/api/MCP", "http://testserver/api/MCP"),
        ("http://testserver/api/mcp#x", None),
        ("ftp://testserver/api/mcp", None),
        ("/api/mcp", None),
        ("http://user:pw@testserver/api/mcp", None),
        ("http://testserver:99999/api/mcp", None),
    ],
)
def test_canonical_resource(url: str, canonical: str | None):
    assert canonical_resource(url) == canonical


# ==========================================
# Redirect URIs


@pytest.mark.parametrize(
    "uri",
    [
        "https://my.home-assistant.io/redirect/oauth",
        "http://homeassistant.local:8123/auth/external/callback",
        "http://homeassistant:8123/auth/external/callback",
        "http://192.168.1.20:8123/auth/external/callback",
        "http://10.1.2.3/cb",
        "http://100.101.102.103:8123/cb",  # Tailscale
        "http://[fd12:3456::1]:8123/cb",
        "http://127.0.0.1/callback",
        "http://localhost:33418/callback",
        "http://[::1]/callback",
        "https://example.com/cb?tenant=1",
    ],
)
def test_valid_redirect_uris(uri: str):
    assert check_redirect_uri(uri) == uri


@pytest.mark.parametrize(
    "uri, reason",
    [
        ("http://example.com/cb", "local network"),
        ("http://8.8.8.8/cb", "local network"),
        ("https://example.com/cb#frag", "fragment"),
        ("https://user:pw@example.com/cb", "user name"),
        ("https://user@example.com/cb", "user name"),
        ("/relative/cb", "https://"),
        ("example.com/cb", "https://"),
        ("myapp://callback", "https://"),
        ("javascript:alert(1)", "https://"),
        ("https:///no-host", "absolute"),
        ("https://example.com/c b", "spaces"),
        ("", "spaces"),
        ("https://example.com:99999/cb", "absolute"),
    ],
)
def test_invalid_redirect_uris(uri: str, reason: str):
    with pytest.raises(ValueError, match=reason):
        check_redirect_uri(uri)


@pytest.mark.parametrize(
    "hostname, local",
    [
        ("homeassistant.local", True),
        ("ha.lan", True),
        ("ha.home.arpa", True),
        ("homeassistant", True),
        ("localhost", True),
        ("127.0.0.1", True),
        ("192.168.0.1", True),
        ("172.16.5.4", True),
        ("169.254.1.1", True),
        ("100.64.0.1", True),
        ("::1", True),
        ("fd00::1", True),
        ("example.com", False),
        ("8.8.8.8", False),
        ("100.128.0.1", False),
        ("2001:4860:4860::8888", False),
    ],
)
def test_is_local_host(hostname: str, local: bool):
    assert is_local_host(hostname) is local


@pytest.mark.parametrize(
    "registered, requested, matches",
    [
        ("https://my.home-assistant.io/redirect/oauth", "https://my.home-assistant.io/redirect/oauth", True),
        # exact matching: no normalisation at all
        ("https://my.home-assistant.io/redirect/oauth", "https://my.home-assistant.io/redirect/oauth/", False),
        ("https://my.home-assistant.io/redirect/oauth", "https://MY.home-assistant.io/redirect/oauth", False),
        ("https://my.home-assistant.io/redirect/oauth", "https://my.home-assistant.io:443/redirect/oauth", False),
        ("https://my.home-assistant.io/redirect/oauth", "https://my.home-assistant.io/redirect/oauth?x=1", False),
        ("http://homeassistant.local:8123/cb", "http://homeassistant.local:8124/cb", False),
        # RFC 8252 §7.3: loopback on any port
        ("http://127.0.0.1/callback", "http://127.0.0.1:43123/callback", True),
        ("http://127.0.0.1:5000/callback", "http://127.0.0.1:43123/callback", True),
        ("http://localhost/callback", "http://localhost:43123/callback", True),
        ("http://[::1]/callback", "http://[::1]:43123/callback", True),
        ("http://127.0.0.1/callback", "http://127.0.0.1:43123/other", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:43123/callback?x=1", False),
        ("http://127.0.0.1/callback", "http://localhost:43123/callback", False),
        ("http://127.0.0.1/callback", "https://127.0.0.1:43123/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:43123@evil.example/callback", False),
        ("http://127.0.0.1/callback", "http://evil.example:43123/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:99999/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:43123/callback#x", False),
    ],
)
def test_redirect_uri_matching(registered: str, requested: str, matches: bool):
    assert redirect_uri_matches(registered, requested) is matches


def test_query_params_keep_the_registered_uri():
    params = [("code", "abc"), ("state", "s t+/"), ("iss", "http://testserver")]
    assert (
        with_query_params("https://ha.example/cb", params)
        == "https://ha.example/cb?code=abc&state=s+t%2B%2F&iss=http%3A%2F%2Ftestserver"
    )
    assert with_query_params("https://ha.example/cb?tenant=1", params[:1]) == "https://ha.example/cb?tenant=1&code=abc"
    assert with_query_params("https://ha.example/cb?", params[:1]) == "https://ha.example/cb?code=abc"
    assert with_query_params("https://ha.example/cb", []) == "https://ha.example/cb"


# ==========================================
# Secrets and PKCE


def test_secrets_are_256_bit_and_stored_hashed():
    secret = new_secret("mmcp_at_")
    assert secret.startswith("mmcp_at_")
    assert len(secret.removeprefix("mmcp_at_")) == 43  # 32 bytes, base64url
    assert new_secret() != new_secret()

    digest = hash_secret(secret)
    assert len(digest) == 64 and secret not in digest
    assert secret_matches(secret, digest)
    assert not secret_matches(secret + "x", digest)
    assert not secret_matches(secret, None)


def test_client_ids_never_read_as_uuids():
    import uuid

    client_id = new_client_id()
    assert client_id.startswith("mmcp_")
    with pytest.raises(ValueError):
        uuid.UUID(client_id)


def test_pkce_s256():
    # RFC 7636 Appendix B
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert is_s256_challenge(challenge)
    assert pkce_matches(verifier, challenge)
    assert not pkce_matches(verifier[:-1] + "l", challenge)
    # a "plain" verifier equal to the challenge is not S256
    assert not pkce_matches(challenge, challenge)
    # RFC 7636 §4.1: 43 to 128 characters of the unreserved set
    assert not pkce_matches("short", challenge)
    assert not pkce_matches("a" * 129, challenge)
    assert not is_s256_challenge(challenge + "=")
    assert not is_s256_challenge("plain-challenge")


def test_scopes_are_written_in_a_fixed_order():
    assert format_scopes(["mcp:write", "mcp:read", "other"]) == "mcp:read mcp:write"
    assert format_scopes([]) == ""
