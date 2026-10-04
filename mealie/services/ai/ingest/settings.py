"""
Recipe card ingestion's environment settings (docs/ai/PHASE2.md §15): a fork `BaseSettings` read from `AI_INGEST_*`, so
upstream's `AppSettings` is untouched. `mealie.core.config` has already loaded `.env` into the environment.
"""

from functools import cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.core.root_logger import get_logger

from .limits import MIB

logger = get_logger(__name__)

APP_DIR = Path("/app")
"""The container's code directory, which the inbox must stay out of"""


def _worker_default() -> bool:
    # tests drive the dispatcher themselves (`run_once()`)
    return not get_app_settings().TESTING


class IngestSettings(BaseSettings):
    ENABLED: bool = True
    """Off: the ingest routes answer 503 and no dispatcher runs"""
    WORKER: bool = Field(default_factory=_worker_default)
    """Run the dispatcher in this process (default on, off under `TESTING`)"""
    CONCURRENCY: int = Field(2, ge=1, le=32)
    """Task threads per worker process, plus one that only re-reads use"""
    MAX_UPLOAD_MB: int = Field(100, ge=1)
    """Per request; JSON bodies are capped at 45 MiB regardless"""
    RETENTION_DAYS: int = Field(14, ge=1)
    """How long committed and failed cards keep their files"""
    INBOX_DIR: Path | None = None
    """A watched folder outside `DATA_DIR` and `/app`, e.g. `/inbox`; unset turns the inbox off"""
    INBOX_POLL_SECONDS: int = Field(30, ge=1)
    INBOX_KEEP_PROCESSED: bool = True
    """Move ingested files to `processed/` rather than deleting them"""

    model_config = SettingsConfigDict(env_prefix="AI_INGEST_", extra="ignore")

    @field_validator("INBOX_DIR", mode="before")
    @classmethod
    def _blank_inbox_is_unset(cls, value: object) -> object:
        # Unraid and compose files pass an unused variable as `AI_INGEST_INBOX_DIR=''`, which `Path` reads as `.`
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * MIB


@cache
def get_ingest_settings() -> IngestSettings:
    return IngestSettings()


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


@cache
def inbox_root() -> Path | None:
    """
    The inbox folder, or None when it's off: `AI_INGEST_INBOX_DIR` unset, or refused because it's inside `DATA_DIR`
    or `/app` (backups would zip it, restores would wipe it and `entry.sh` would re-own it). A refusal is logged once.
    """
    configured = get_ingest_settings().INBOX_DIR
    if configured is None:
        return None

    root = configured.expanduser().resolve()
    data_dir = get_app_dirs().DATA_DIR.resolve()
    for forbidden in (data_dir, APP_DIR):
        if _is_within(root, forbidden) or _is_within(forbidden, root):
            logger.warning(
                f"AI_INGEST_INBOX_DIR ({root}) overlaps {forbidden}, which isn't allowed: the recipe card inbox is off"
            )
            return None

    return root
