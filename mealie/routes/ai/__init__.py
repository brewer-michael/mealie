"""Fork-owned AI routes (docs/ai/PHASE1.md), kept in their own package to stay clear of upstream syncs"""

from fastapi import APIRouter

from . import controller_tools

router = APIRouter()

router.include_router(controller_tools.router)
