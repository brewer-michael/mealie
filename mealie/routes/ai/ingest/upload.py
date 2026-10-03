"""`POST /api/ai/ingest` and the batch routes (docs/ai/PHASE2.md §1.2, §1.4). Work item B2 adds the routes."""

from fastapi import APIRouter

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])
