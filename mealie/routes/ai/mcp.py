"""
The MCP server's endpoint, `/api/mcp` (docs/ai/PHASE3.md §1).

Plain Starlette routes on a router whose lifespan runs the MCP session manager. FastAPI merges a router's lifespan
into the app's through `include_router`, so `mealie/app.py` needs no change for this.
"""

from fastapi import APIRouter
from starlette.routing import Route

from mealie.services.ai.mcp.endpoint import McpEndpoint
from mealie.services.ai.mcp.server import create_mcp_server

endpoint = McpEndpoint(create_mcp_server())

router = APIRouter(lifespan=endpoint.lifespan, include_in_schema=False)

# With and without a trailing slash: the SPA would answer a typed slash, and Home Assistant doesn't follow redirects
router.routes.append(Route("/mcp", endpoint, name="mcp"))
router.routes.append(Route("/mcp/", endpoint, name="mcp_slash"))
