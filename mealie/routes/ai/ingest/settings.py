"""`GET`/`PUT /api/ai/ingest/settings` (docs/ai/PHASE2.md §10, §14). Work item B4 adds the routes."""

from fastapi import APIRouter

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])
