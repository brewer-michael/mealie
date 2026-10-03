"""`GET /api/ai/about` (docs/ai/PHASE2.md §14): public. Work item B2 adds the route."""

from fastapi import APIRouter

router = APIRouter(prefix="/ai/about", tags=["AI: Recipe Cards"])
