"""The discovery documents (docs/ai/PHASE3.md §4 "Metadata"), built from the request's origin"""

from typing import Any

from .tokens import SUPPORTED_SCOPES
from .urls import OAUTH_PATH, mcp_url

TOKEN_ENDPOINT_AUTH_METHODS = ["client_secret_post", "client_secret_basic", "none"]


def protected_resource_metadata(origin: str) -> dict[str, Any]:
    """RFC 9728 §2. Home Assistant requires `resource` to equal the URL its user typed, exactly."""
    return {
        "resource": mcp_url(origin),
        "authorization_servers": [origin],
        "scopes_supported": list(SUPPORTED_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Mealie",
    }


def authorization_server_metadata(origin: str) -> dict[str, Any]:
    """
    RFC 8414 §2. `issuer` is exactly the origin listed in `authorization_servers` above, as MCP clients check
    (RFC 8414 §3.3).
    """
    return {
        "issuer": origin,
        "authorization_endpoint": f"{origin}{OAUTH_PATH}/authorize",
        "token_endpoint": f"{origin}{OAUTH_PATH}/token",
        "revocation_endpoint": f"{origin}{OAUTH_PATH}/revoke",
        "scopes_supported": list(SUPPORTED_SCOPES),
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": TOKEN_ENDPOINT_AUTH_METHODS,
        "revocation_endpoint_auth_methods_supported": TOKEN_ENDPOINT_AUTH_METHODS,
        # RFC 9207 §3: every authorization response carries `iss`
        "authorization_response_iss_parameter_supported": True,
    }
