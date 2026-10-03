"""
The MCP authorization server's endpoints, as synchronous service methods (docs/ai/PHASE3.md §4). The routes run
them in worker threads.

Parameters come in as `(name, value)` pairs so repeated parameters can be refused (RFC 6749 §3.1, §3.2).
"""

import base64
import binascii
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import unquote_plus
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from mealie.db.models.ai_mcp import McpOAuthClient, McpOAuthCode, McpOAuthRequest, McpOAuthToken
from mealie.db.models.users.users import User
from mealie.repos.repository_mcp import RepositoryMcpOAuth
from mealie.schema.mcp.mcp_oauth import McpOAuthRequestOut, McpScope
from mealie.schema.user.user import PrivateUser
from mealie.services.ai.mcp.auth import invalidate_mcp_principals

from .errors import AuthorizationPageError, OAuthError, OAuthRequestNotFoundError
from .tokens import (
    ACCESS_TOKEN_PREFIX,
    ACCESS_TOKEN_TTL,
    CODE_TTL,
    REFRESH_TOKEN_PREFIX,
    REFRESH_TOKEN_TTL,
    REQUEST_TTL,
    SCOPE_READ,
    SCOPE_WRITE,
    SUPPORTED_SCOPES,
    format_scopes,
    hash_secret,
    is_s256_challenge,
    new_secret,
    parse_scopes,
    pkce_matches,
    predates_password_change,
    secret_matches,
)
from .urls import (
    CONSENT_PAGE_PATH,
    canonical_resource,
    display_host,
    mcp_url,
    redirect_uri_matches,
    with_query_params,
)

Params = Sequence[tuple[str, str]]

IGNORED_SCOPES = frozenset({"offline_access"})
"""Asked for by some clients to get a refresh token, which every client gets anyway"""
MAX_STATE_LENGTH = 2048
"""Home Assistant's `state` is about 600 characters"""


class _ParamError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _param(params: Params, name: str) -> str | None:
    """
    A single-valued parameter: RFC 6749 §3.1 and §3.2 forbid repeating one, and a parameter without a value is
    treated as omitted
    """
    values = [value for key, value in params if key == name]
    if len(values) > 1:
        raise _ParamError(name)
    return values[0] if values and values[0] else None


def _params(params: Params, name: str) -> list[str]:
    return [value for key, value in params if key == name and value]


class McpAuthorizationServer:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = RepositoryMcpOAuth(session)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    # ==========================================
    # Authorization endpoint (RFC 6749 §4.1.1)

    def authorize(self, params: Params, origin: str) -> str:
        """
        Validates an authorization request and stores it for the user's consent. Returns where to send the browser:
        the consent page, or the client's redirect URI with an error (§4.1.2.1).

        Raises `AuthorizationPageError` when the client or the redirect URI is invalid: then nothing may redirect
        to the client (§4.1.2.1, §10.15).
        """
        try:
            client_id = _param(params, "client_id")
            redirect_uri = _param(params, "redirect_uri")
        except _ParamError as e:
            raise AuthorizationPageError(f"The request repeats the {e.name} parameter.") from None

        if not client_id:
            raise AuthorizationPageError("The request doesn't say which app is asking (no client_id).")
        client = self.repo.get_client(client_id)
        if client is None:
            raise AuthorizationPageError("This app isn't registered with Mealie. Check its client ID.")

        # §3.1.2.3: compare against the registered redirect URIs (exactly, but for RFC 8252 §7.3 loopback ports).
        # It may be left out only when there's a single registered URI to use (OAuth 2.1 §4.1.1).
        if redirect_uri is not None:
            if not any(redirect_uri_matches(registered, redirect_uri) for registered in client.redirect_uris):
                raise AuthorizationPageError(
                    "This app asked to return to an address that isn't registered for it in Mealie."
                )
            target = redirect_uri
        elif len(client.redirect_uris) == 1:
            target = client.redirect_uris[0]
        else:
            raise AuthorizationPageError("The request doesn't say where to return to (no redirect_uri).")

        # From here on, errors go back to the client (§4.1.2.1), with `state` if there's exactly one
        try:
            state = _param(params, "state")
            repeated: str | None = None
        except _ParamError as e:
            state, repeated = None, e.name
        # it would be stored, and anyone can make a pending request: too long a state is refused, and not echoed
        state_too_long = state is not None and len(state) > MAX_STATE_LENGTH
        if state_too_long:
            state = None

        def fail(error: str, description: str) -> str:
            return self._redirect(target, origin, state, error=error, error_description=description)

        try:
            response_type = _param(params, "response_type")
            scope = _param(params, "scope")
            code_challenge = _param(params, "code_challenge")
            code_challenge_method = _param(params, "code_challenge_method")
        except _ParamError as e:
            repeated = e.name
        if repeated:
            return fail("invalid_request", f"The {repeated} parameter is repeated")
        if state_too_long:
            return fail("invalid_request", f"state is longer than {MAX_STATE_LENGTH} characters")

        if response_type is None:
            return fail("invalid_request", "response_type is required")
        if response_type != "code":
            return fail("unsupported_response_type", "Only the authorization code flow is supported")

        # §3.3: unknown scopes are an error. `mcp:write` for a client that may not have it is left out instead: the
        # AS may grant less than asked for, and the token response says what was granted.
        requested = parse_scopes(scope)
        if unknown := sorted(set(requested) - set(SUPPORTED_SCOPES) - IGNORED_SCOPES):
            return fail("invalid_scope", f"Unknown scope: {' '.join(unknown)}")
        allowed = [SCOPE_READ]
        if SCOPE_WRITE in requested and client.allow_write_scope:
            allowed.append(SCOPE_WRITE)

        # RFC 7636 §4.3-4.4: S256 only (an absent method means "plain"). OAuth 2.1 §7.5.1 lets only a confidential
        # client go without PKCE, here when its registration says so.
        if code_challenge is not None:
            if code_challenge_method != "S256":
                return fail("invalid_request", "code_challenge_method must be S256")
            if not is_s256_challenge(code_challenge):
                return fail("invalid_request", "code_challenge is not a valid S256 challenge")
        elif code_challenge_method is not None:
            return fail("invalid_request", "code_challenge_method was given without a code_challenge")
        elif not (client.is_confidential and client.pkce_optional):
            return fail("invalid_request", "PKCE is required: send a code_challenge with code_challenge_method=S256")

        # RFC 8707 §2: the only resource here is this server's MCP endpoint, which is also the default
        resource = canonical_resource(mcp_url(origin))
        if resource is None:
            return fail("server_error", "This server's address can't be used as a resource")
        if any(canonical_resource(value) != resource for value in _params(params, "resource")):
            return fail("invalid_target", f"The only resource here is {resource}")

        now = self._now()
        handle = new_secret()
        self.repo.add_request(
            McpOAuthRequest(
                handle_hash=hash_secret(handle),
                oauth_client_id=client.id,
                redirect_uri=target,
                redirect_uri_provided=redirect_uri is not None,
                scopes=format_scopes(allowed),
                state=state,
                code_challenge=code_challenge,
                resource=resource,
                issuer=origin,
                expires_at=now + REQUEST_TTL,
                created_at=now,
            ),
            now,
        )
        self.session.commit()

        return with_query_params(CONSENT_PAGE_PATH, [("request", handle)])

    @staticmethod
    def _redirect(redirect_uri: str, issuer: str, state: str | None, **params: str) -> str:
        """The client's redirect URI with response parameters, `state` if the request had one, and `iss`"""
        query = list(params.items())
        if state is not None:
            query.append(("state", state))
        # RFC 9207 §2: identifies this server, on success and on error alike
        query.append(("iss", issuer))
        return with_query_params(redirect_uri, query)

    # ==========================================
    # Consent

    def _pending_request(self, handle: str, user: PrivateUser) -> McpOAuthRequest:
        request = self.repo.get_request(hash_secret(handle)) if handle else None
        # A client belongs to a group: only that group's users can connect it
        if request is None or request.expires_at <= self._now() or request.oauth_client.group_id != user.group_id:
            raise OAuthRequestNotFoundError()
        return request

    def describe_request(self, handle: str, user: PrivateUser) -> McpOAuthRequestOut:
        request = self._pending_request(handle, user)
        scopes = parse_scopes(request.scopes)
        return McpOAuthRequestOut(
            client_name=request.oauth_client.name,
            scopes=[McpScope(scope) for scope in scopes],
            writes_offered=SCOPE_WRITE in scopes,
            redirect_host=display_host(request.redirect_uri),
            expires_at=request.expires_at,
        )

    def decide(self, handle: str, user: PrivateUser, approve: bool, allow_writes: bool) -> str:
        """Records the user's decision on a pending request; returns the client redirect URI to send them to"""
        request = self._pending_request(handle, user)
        if not self.repo.delete_request(request.id):
            raise OAuthRequestNotFoundError()

        if not approve:
            location = self._redirect(
                request.redirect_uri,
                request.issuer,
                request.state,
                error="access_denied",
                error_description="The user declined",
            )
            self.session.commit()
            return location

        scopes = [SCOPE_READ]
        if allow_writes and SCOPE_WRITE in parse_scopes(request.scopes):
            scopes.append(SCOPE_WRITE)

        # §4.1.2: short-lived, single use, bound to the client, user, redirect URI, scopes, resource and challenge
        code = new_secret()
        now = self._now()
        self.repo.add_code(
            McpOAuthCode(
                code_hash=hash_secret(code),
                oauth_client_id=request.oauth_client_id,
                user_id=user.id,
                redirect_uri=request.redirect_uri,
                redirect_uri_provided=request.redirect_uri_provided,
                scopes=format_scopes(scopes),
                resource=request.resource,
                code_challenge=request.code_challenge,
                expires_at=now + CODE_TTL,
                created_at=now,
            )
        )
        location = self._redirect(request.redirect_uri, request.issuer, request.state, code=code)
        self.session.commit()
        return location

    # ==========================================
    # Client authentication (RFC 6749 §2.3.1, §3.2.1)

    def _authenticate_client(self, params: Params, authorization: str | None) -> McpOAuthClient:
        basic: tuple[str, str] | None = None
        if authorization and authorization[:6].lower() == "basic ":
            try:
                decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
                basic_id, basic_secret = decoded.split(":", 1)
            except binascii.Error, UnicodeDecodeError, ValueError:
                raise OAuthError("invalid_client", "Malformed Basic credentials", basic_auth_used=True) from None
            # §2.3.1: both are form-encoded before they're joined
            basic = (unquote_plus(basic_id), unquote_plus(basic_secret))

        try:
            form_id = _param(params, "client_id")
            form_secret = _param(params, "client_secret")
        except _ParamError as e:
            raise OAuthError("invalid_request", f"The {e.name} parameter is repeated") from None

        secret: str | None
        if basic is not None:
            # §2.3: one authentication method per request
            if form_secret is not None or (form_id is not None and form_id != basic[0]):
                raise OAuthError("invalid_request", "Use one client authentication method")
            client_id, secret = basic
        elif form_id is not None:
            client_id, secret = form_id, form_secret
        else:
            raise OAuthError("invalid_client", "Client authentication is required")

        # The request's first read locks the client until it commits, so granting and revoking its tokens take turns
        client = self.repo.get_client(client_id, for_update=True)
        if client is None:
            raise OAuthError("invalid_client", "Unknown client", basic_auth_used=basic is not None)

        if client.is_confidential:
            # compared in constant time
            if secret is None or not secret_matches(secret, client.client_secret_hash):
                raise OAuthError("invalid_client", "Client authentication failed", basic_auth_used=basic is not None)
        elif secret is not None:
            # a public client has no secret to send (method "none")
            raise OAuthError("invalid_client", "This client has no secret", basic_auth_used=basic is not None)

        return client

    # ==========================================
    # Token endpoint (RFC 6749 §3.2, §4.1.3, §6)

    def token(self, params: Params, authorization: str | None) -> dict[str, str | int]:
        """Returns the RFC 6749 §5.1 response body, or raises `OAuthError` (§5.2)"""
        try:
            grant_type = _param(params, "grant_type")
        except _ParamError as e:
            raise OAuthError("invalid_request", f"The {e.name} parameter is repeated") from None
        if grant_type is None:
            raise OAuthError("invalid_request", "grant_type is required")

        client = self._authenticate_client(params, authorization)

        try:
            if grant_type == "authorization_code":
                return self._exchange_code(client, params)
            if grant_type == "refresh_token":
                return self._refresh(client, params)
        except _ParamError as e:
            raise OAuthError("invalid_request", f"The {e.name} parameter is repeated") from None

        raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token")

    def _check_user(self, client: McpOAuthClient, user_id: UUID, issued_at: datetime) -> User:
        user = self.repo.get_user(user_id)
        if user is None or user.group_id != client.group_id:
            raise OAuthError("invalid_grant", "The user is gone")
        # Changing a password evicts everything issued before it, as it does for upstream's tokens. It also revokes
        # them; this covers a grant that was being committed at the time.
        if predates_password_change(issued_at, user.tokens_valid_after):
            raise OAuthError("invalid_grant", "The user's password changed since this was issued")
        return user

    def _check_resource(self, params: Params, resource: str) -> None:
        # RFC 8707 §2.2: a token request may name the resource again, but only the one granted
        if any(canonical_resource(value) != resource for value in _params(params, "resource")):
            raise OAuthError("invalid_target", f"The only resource granted is {resource}")

    def _exchange_code(self, client: McpOAuthClient, params: Params) -> dict[str, str | int]:
        code = _param(params, "code")
        if code is None:
            raise OAuthError("invalid_request", "code is required")

        now = self._now()
        row = self.repo.get_code(hash_secret(code))
        if row is None or row.oauth_client_id != client.id:
            raise OAuthError("invalid_grant", "Invalid authorization code")

        if row.used:
            # §4.1.2: a code used twice is refused, and the tokens issued from it are revoked (§10.5)
            self._revoke_family(row.family_id, now)
            raise OAuthError("invalid_grant", "The authorization code was already used")

        if row.expires_at <= now:
            raise OAuthError("invalid_grant", "The authorization code expired")

        # §4.1.3: the same redirect_uri as the authorization request, if it had one
        redirect_uri = _param(params, "redirect_uri")
        if (row.redirect_uri_provided or redirect_uri is not None) and redirect_uri != row.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri doesn't match the authorization request")

        # RFC 7636 §4.6. Without a challenge, a verifier is refused too (OAuth 2.1 §4.1.3), so PKCE can't be dropped
        # from the authorization request alone.
        code_verifier = _param(params, "code_verifier")
        if row.code_challenge is not None:
            if code_verifier is None or not pkce_matches(code_verifier, row.code_challenge):
                raise OAuthError("invalid_grant", "code_verifier doesn't match the code_challenge")
        elif code_verifier is not None:
            raise OAuthError("invalid_grant", "The authorization request had no code_challenge")

        self._check_resource(params, row.resource)
        user = self._check_user(client, row.user_id, row.created_at)

        family_id = uuid4()
        if not self.repo.claim_code(row.id, family_id):
            # another request exchanged it in the meantime: that's reuse too
            self.session.rollback()
            self._revoke_family(self.repo.code_family(row.id), now)
            raise OAuthError("invalid_grant", "The authorization code was already used")

        response = self._issue(client, user, row.scopes, row.scopes, row.resource, family_id, now, granted_at=now)
        self.session.commit()
        return response

    def _refresh(self, client: McpOAuthClient, params: Params) -> dict[str, str | int]:
        refresh_token = _param(params, "refresh_token")
        if refresh_token is None:
            raise OAuthError("invalid_request", "refresh_token is required")

        now = self._now()
        row = self.repo.get_token(hash_secret(refresh_token))
        # §6: the refresh token must have been issued to the authenticated client
        if row is None or row.kind != "refresh" or row.oauth_client_id != client.id:
            raise OAuthError("invalid_grant", "Invalid refresh token")

        if row.revoked_at is not None:
            # OAuth 2.1 §4.3.1: a refresh token that was already rotated is being replayed, so the whole family is
            # revoked (whoever holds the current one has to authorize again)
            self._revoke_family(row.family_id, now)
            raise OAuthError("invalid_grant", "The refresh token was revoked")

        if row.expires_at <= now:
            raise OAuthError("invalid_grant", "The refresh token expired")

        self._check_resource(params, row.resource)
        try:
            user = self._check_user(client, row.user_id, row.created_at)
        except OAuthError:
            self._revoke_family(row.family_id, now)
            raise

        # §6: a narrower scope may be asked for, never more than was granted
        granted = parse_scopes(row.scopes)
        access_scopes = row.scopes
        if (scope := _param(params, "scope")) is not None:
            requested = parse_scopes(scope)
            if not set(requested) <= set(granted):
                raise OAuthError("invalid_scope", "The requested scope exceeds what was granted")
            access_scopes = format_scopes(requested)

        # OAuth 2.1 §4.3.1: rotate. The new refresh token keeps the granted scope (§6) and another 90 days.
        if not self.repo.rotate(row.id, now):
            self.session.rollback()
            self._revoke_family(row.family_id, now)
            raise OAuthError("invalid_grant", "The refresh token was revoked")

        response = self._issue(
            client, user, access_scopes, row.scopes, row.resource, row.family_id, now, granted_at=row.granted_at
        )
        self.session.commit()
        return response

    def _issue(
        self,
        client: McpOAuthClient,
        user: User,
        access_scopes: str,
        refresh_scopes: str,
        resource: str,
        family_id: UUID,
        now: datetime,
        *,
        granted_at: datetime,
    ) -> dict[str, str | int]:
        access_token = new_secret(ACCESS_TOKEN_PREFIX)
        refresh_token = new_secret(REFRESH_TOKEN_PREFIX)
        for kind, value, scopes, ttl in (
            ("access", access_token, access_scopes, ACCESS_TOKEN_TTL),
            ("refresh", refresh_token, refresh_scopes, REFRESH_TOKEN_TTL),
        ):
            self.repo.add_token(
                McpOAuthToken(
                    token_hash=hash_secret(value),
                    kind=kind,
                    oauth_client_id=client.id,
                    user_id=user.id,
                    family_id=family_id,
                    scopes=scopes,
                    resource=resource,
                    expires_at=now + ttl,
                    granted_at=granted_at,
                    created_at=now,
                )
            )
        client.last_used_at = now

        # §5.1. `expires_in` is always sent: Home Assistant requires it. `scope` too, since it can be less than
        # what was asked for (§3.3).
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "refresh_token": refresh_token,
            "scope": access_scopes,
        }

    def _revoke_family(self, family_id: UUID | None, now: datetime) -> None:
        if family_id is None:
            return
        self.repo.revoke_family(family_id, now)
        self.session.commit()
        invalidate_mcp_principals(family_id=family_id)

    # ==========================================
    # Revocation endpoint (RFC 7009)

    def revoke(self, params: Params, authorization: str | None) -> None:
        """
        §2.1: authenticates the client, then revokes the token if it was issued to that client. A refresh token
        takes its whole family with it; an access token goes alone. Unknown tokens are not an error (§2.2).
        """
        client = self._authenticate_client(params, authorization)
        try:
            token = _param(params, "token")
        except _ParamError as e:
            raise OAuthError("invalid_request", f"The {e.name} parameter is repeated") from None
        if token is None:
            raise OAuthError("invalid_request", "token is required")

        # token_type_hint is only a hint (§2.1): both kinds are looked up the same way
        row = self.repo.get_token(hash_secret(token))
        if row is None or row.oauth_client_id != client.id:
            return

        now = self._now()
        if row.kind == "refresh":
            self._revoke_family(row.family_id, now)
            return

        self.repo.revoke_token(row.id, now)
        self.session.commit()
        invalidate_mcp_principals(token_hash=row.token_hash)
