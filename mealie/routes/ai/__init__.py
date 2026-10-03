"""Fork-owned AI routes (docs/ai/PHASE1.md, PHASE3.md), kept in their own package to stay clear of upstream syncs"""

from fastapi import APIRouter

from . import controller_tools, mcp

router = APIRouter()

router.include_router(controller_tools.router)
router.include_router(mcp.router)  # /api/mcp, and the lifespan that runs the MCP server
