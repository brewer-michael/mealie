"""
The recipe card routes (docs/ai/PHASE2.md §14). This router's lifespan runs the ingest dispatcher in every worker
process; FastAPI merges a router's lifespan into the app's through `include_router` (the Phase 3 precedent), so
`mealie/app.py` needs no change.
"""

from fastapi import APIRouter

from mealie.services.ai.ingest.runner.dispatcher import dispatcher

from . import about, eval_cases, jobs, notifiers, settings, upload

router = APIRouter(lifespan=dispatcher.lifespan)

router.include_router(upload.router)
router.include_router(jobs.router)
router.include_router(eval_cases.router)
router.include_router(settings.router)
router.include_router(notifiers.router)
router.include_router(about.router)
