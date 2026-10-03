"""
Fork-owned routes for the MCP server's authorization (docs/ai/PHASE3.md §4, §6): `/.well-known` discovery at the
root, the OAuth endpoints under `/api/oauth`, and the client, connected-app and API token grant APIs.

Included by `mealie/app.py` on its own, because the discovery documents live outside `/api`, and before the SPA is
mounted, so the SPA can never answer for them.
"""

from fastapi import APIRouter

from . import controller_mcp_clients, controller_mcp_user, controller_oauth, well_known

router = APIRouter()

router.include_router(well_known.router)
router.include_router(controller_oauth.router, prefix="/api")
router.include_router(controller_oauth.consent_router, prefix="/api")
router.include_router(controller_mcp_clients.router, prefix="/api")
router.include_router(controller_mcp_user.router, prefix="/api")
