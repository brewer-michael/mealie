"""
Discovery documents at the root (docs/ai/PHASE3.md §4 "Metadata"), and a JSON 404 for every other `/.well-known`
path.

The 404 matters in production: there the SPA answers any unknown GET with 200 HTML, and Home Assistant fetches
several discovery URLs at once and parses the first 2xx as JSON. These routes are registered before the SPA is
mounted, so they always answer first.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.routing import Route

from mealie.services.oauth.metadata import authorization_server_metadata, protected_resource_metadata
from mealie.services.oauth.urls import (
    AUTHORIZATION_SERVER_METADATA_PATH,
    MCP_PATH,
    PROTECTED_RESOURCE_METADATA_PATH,
    request_origin,
)

router = APIRouter(include_in_schema=False)


# RFC 9728 §3.1: at the path-inserted URL for the resource `/api/mcp` (where the 401 points), and at the root
@router.api_route(PROTECTED_RESOURCE_METADATA_PATH + MCP_PATH, methods=["GET", "HEAD"])
async def mcp_protected_resource_metadata(request: Request) -> JSONResponse:
    return JSONResponse(protected_resource_metadata(request_origin(request.scope)))


@router.api_route(PROTECTED_RESOURCE_METADATA_PATH, methods=["GET", "HEAD"])
async def protected_resource_metadata_root(request: Request) -> JSONResponse:
    return JSONResponse(protected_resource_metadata(request_origin(request.scope)))


# RFC 8414 §3.1: the issuer is the origin, so this is the root URL. The `/api/mcp` variant serves clients that
# build it from the MCP URL.
@router.api_route(AUTHORIZATION_SERVER_METADATA_PATH, methods=["GET", "HEAD"])
async def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
    return JSONResponse(authorization_server_metadata(request_origin(request.scope)))


@router.api_route(AUTHORIZATION_SERVER_METADATA_PATH + MCP_PATH, methods=["GET", "HEAD"])
async def mcp_authorization_server_metadata(request: Request) -> JSONResponse:
    return JSONResponse(authorization_server_metadata(request_origin(request.scope)))


async def well_known_not_found(_request: Request) -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


# A plain Starlette route, so it stays out of the OpenAPI document. Registered last: the routes above come first.
router.routes.append(Route("/.well-known/{path:path}", well_known_not_found, methods=["GET", "HEAD"]))
