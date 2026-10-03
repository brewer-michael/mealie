"""
Saving reviewed cards as eval cases, and managing them (docs/ai/PHASE2.md §11.6). Work item B7 adds the routes.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])
