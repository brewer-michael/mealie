"""Errors of the MCP authorization server (docs/ai/PHASE3.md §4)"""


class OAuthError(Exception):
    """
    An error response from the token or revocation endpoint (RFC 6749 §5.2, RFC 7009 §2.2.1): sent as JSON
    `{"error", "error_description"}`, with status 400, or 401 for `invalid_client`.
    """

    def __init__(self, error: str, description: str | None = None, *, basic_auth_used: bool = False) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.basic_auth_used = basic_auth_used

    @property
    def status_code(self) -> int:
        return 401 if self.error == "invalid_client" else 400

    @property
    def headers(self) -> dict[str, str]:
        # RFC 6749 §5.2: a client that authenticated with the Authorization header gets a 401 challenging its scheme
        if self.error == "invalid_client" and self.basic_auth_used:
            return {"WWW-Authenticate": 'Basic realm="Mealie"'}
        return {}

    def as_dict(self) -> dict[str, str]:
        body = {"error": self.error}
        if self.description:
            body["error_description"] = self.description
        return body


class AuthorizationPageError(Exception):
    """
    An authorization request that can't be answered with a redirect, because the client or the redirect URI is
    invalid (RFC 6749 §4.1.2.1): the user sees this message instead, and is never sent anywhere.
    """


class OAuthRequestNotFoundError(Exception):
    """A pending authorization request that doesn't exist, has expired or isn't the user's to decide"""
