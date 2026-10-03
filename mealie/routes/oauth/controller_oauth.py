"""
The MCP authorization server's endpoints (docs/ai/PHASE3.md §4): authorize, the consent page's API, token and
revoke. The work is done by `McpAuthorizationServer` in worker threads, never on the event loop.
"""

import html

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from mealie.db.db_setup import generate_session
from mealie.routes._base import BaseUserController, controller
from mealie.schema.mcp.mcp_oauth import McpOAuthDecision, McpOAuthDecisionOut, McpOAuthRequestOut
from mealie.schema.response import ErrorResponse
from mealie.services.oauth.errors import AuthorizationPageError, OAuthError, OAuthRequestNotFoundError
from mealie.services.oauth.server import McpAuthorizationServer
from mealie.services.oauth.urls import request_origin

router = APIRouter(prefix="/oauth", tags=["MCP: OAuth"])

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
"""RFC 6749 §5.1: token responses must not be cached"""
MAX_AUTHORIZE_QUERY_LENGTH = 8192
"""Anyone can send an authorization request: a longer one isn't read at all"""


def _error_page(message: str) -> HTMLResponse:
    """Shown instead of redirecting when the client or redirect URI is invalid (RFC 6749 §4.1.2.1)"""
    content = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Mealie</title></head>'
        '<body style="font-family: sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem">'
        f"<h1>This app can't connect to Mealie</h1><p>{html.escape(message)}</p>"
        "<p>Nothing was shared with it. A group manager can check the app under Group Settings in Mealie.</p>"
        "</body></html>"
    )
    return HTMLResponse(
        content,
        status_code=status.HTTP_400_BAD_REQUEST,
        headers={**NO_STORE, "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
    )


def _oauth_error(error: OAuthError) -> JSONResponse:
    """RFC 6749 §5.2"""
    return JSONResponse(error.as_dict(), status_code=error.status_code, headers={**NO_STORE, **error.headers})


async def _form_params(request: Request) -> list[tuple[str, str]]:
    # RFC 6749 §3.2: application/x-www-form-urlencoded; anything else reads as no parameters
    form = await request.form()
    return [(key, value) for key, value in form.multi_items() if isinstance(value, str)]


@router.get("/authorize")
def authorize(request: Request, session: Session = Depends(generate_session)) -> Response:
    """
    RFC 6749 §4.1.1. Sends the browser to the consent page (`/oauth/consent?request=…`), or back to the client
    with an error. A request naming an unknown client or an unregistered redirect URI gets an error page instead.
    """
    if len(request.scope.get("query_string", b"")) > MAX_AUTHORIZE_QUERY_LENGTH:
        return _error_page("The request is too long.")

    try:
        location = McpAuthorizationServer(session).authorize(
            request.query_params.multi_items(), request_origin(request.scope)
        )
    except AuthorizationPageError as e:
        return _error_page(str(e))

    return RedirectResponse(location, status_code=status.HTTP_302_FOUND, headers=NO_STORE)


@router.post("/token")
async def token(request: Request, session: Session = Depends(generate_session)) -> JSONResponse:
    """
    RFC 6749 §3.2: the `authorization_code` and `refresh_token` grants, with `client_secret_post`,
    `client_secret_basic` or (for public clients) `none`. Takes form data.
    """
    params = await _form_params(request)
    try:
        body = await run_in_threadpool(
            McpAuthorizationServer(session).token, params, request.headers.get("authorization")
        )
    except OAuthError as e:
        return _oauth_error(e)

    return JSONResponse(body, headers=NO_STORE)


@router.post("/revoke")
async def revoke(request: Request, session: Session = Depends(generate_session)) -> Response:
    """RFC 7009. Answers 200 whether or not the token was known."""
    params = await _form_params(request)
    try:
        await run_in_threadpool(McpAuthorizationServer(session).revoke, params, request.headers.get("authorization"))
    except OAuthError as e:
        return _oauth_error(e)

    return Response(status_code=status.HTTP_200_OK, headers=NO_STORE)


# ==========================================
# Consent (the SPA's /oauth/consent page)


def require_bearer_header(request: Request) -> None:
    """
    The decision must come with the user's bearer token, not just the session cookie a cross-site form could
    carry along, which makes it safe from CSRF
    """
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})


consent_router = APIRouter(prefix="/oauth/requests", tags=["MCP: OAuth"], dependencies=[Depends(require_bearer_header)])


def _request_not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail=ErrorResponse.respond(message="This request expired or was already answered. Start again in the app."),
    )


@controller(consent_router)
class OAuthConsentController(BaseUserController):
    @consent_router.get("/{handle}", response_model=McpOAuthRequestOut)
    def get_request(self, handle: str) -> McpOAuthRequestOut:
        """A pending authorization request: who is asking, for what, and where the browser goes next"""
        try:
            return McpAuthorizationServer(self.session).describe_request(handle, self.user)
        except OAuthRequestNotFoundError:
            raise _request_not_found() from None

    @consent_router.post("/{handle}", response_model=McpOAuthDecisionOut)
    def decide(self, handle: str, decision: McpOAuthDecision) -> McpOAuthDecisionOut:
        """
        Approves or denies a pending request, once. Returns where to send the browser: the client's redirect URI
        with a code (RFC 6749 §4.1.2), or with `error=access_denied` (§4.1.2.1), plus `state` and `iss`
        (RFC 9207). `allowWrites` grants `mcp:write` if the client asked for it and may have it.
        """
        try:
            location = McpAuthorizationServer(self.session).decide(
                handle, self.user, decision.approve, decision.allow_writes
            )
        except OAuthRequestNotFoundError:
            raise _request_not_found() from None

        return McpOAuthDecisionOut(redirect_to=location)
