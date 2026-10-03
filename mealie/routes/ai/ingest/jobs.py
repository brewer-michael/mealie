"""
The job routes under `/api/ai/ingest/jobs` (docs/ai/PHASE2.md §14): review, re-read, rotate, page images, commit and
discard. Work item B3 adds the routes; `/jobs/counts` is declared before `/jobs/{id}`.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])
