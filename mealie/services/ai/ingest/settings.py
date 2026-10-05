"""
Recipe card ingestion's environment settings (docs/ai/PHASE2.md §15): a fork `BaseSettings` read from `AI_INGEST_*`, so
upstream's `AppSettings` is untouched. `mealie.core.config` has already loaded `.env` into the environment.
"""

import os
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
MOUNTED_INBOX = Path("/inbox")
"""Where the compose file and the Unraid template mount the inbox: used when it's a mount and nothing else is set"""
DEFAULT_INBOX_DIR_MODE = "2775"
"""The inbox folders' mode when `AI_INGEST_INBOX_DIR_MODE` is unset, or isn't a mode"""


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
    """A watched folder outside `DATA_DIR` and `/app`, e.g. `/inbox`; unset: `/inbox` if a folder is mounted there,
    else the inbox is off"""
    INBOX_POLL_SECONDS: int = Field(30, ge=1)
    INBOX_KEEP_PROCESSED: bool = True
    """Move ingested files to `processed/` rather than deleting them"""
    INBOX_PROCESSED_DAYS: int | None = Field(None, ge=1)
    """Files in `processed/` older than this many days are deleted (once a day); unset keeps them all"""
    INBOX_DIR_MODE: str = DEFAULT_INBOX_DIR_MODE
    """The octal mode of the inbox folders Mealie creates (setgid and group-writable, so writers in its group can add
    photos); existing folders are never changed. A value that isn't a mode is logged, and 2775 is used."""
    ORIENT: bool = True
    """Turn sideways cards upright with Tesseract when it's installed, whatever `OCR_ENABLED` says"""
    LOCK_DIR: Path | None = None
    """Where the ingest write lock lives, for a `DATA_DIR` on a filesystem without file locks; unset: `DATA_DIR`"""
    GROUP_CONCURRENCY: int = Field(0, ge=0)
    """At most this many cards of one group are read at once, across worker processes; 0: no cap"""
    MAX_PROCESSING_PER_USER: int = Field(0, ge=0)
    """An upload is refused with 429 while this many of its sender's cards wait to be read, counted before its body
    and again as each card goes in: a request whose first card finds the cap reached is a 429, and a later card past
    it is refused on its own (`quota`). 0: no cap"""
    URL_FETCH: bool = False
    """Accept image URLs in the upload API's JSON (`{"images": [{"url": ...}]}`), downloaded by the server; off: they
    are refused `url_not_allowed` and nothing is fetched"""
    URL_ALLOW_HOSTS: str = ""
    """Hostnames and addresses (or CIDR ranges), comma-separated, that image URLs may reach although they're on a
    private network (Home Assistant's, say); on top of `HTTP_ALLOW_LIST`. `HTTP_DISALLOW_LIST` still wins."""
    URL_TIMEOUT: int = Field(20, ge=1, le=300)
    """Seconds one image URL's download may take in all, redirects included"""
    PDF_CPU_SECONDS: int = Field(20, ge=1, le=600)
    """
    The CPU time one PDF's pages may take to render (the renderer's `RLIMIT_CPU`); its rendering may take 1.5 times
    this in all, waiting for a CPU included. A scanned card of 4 pages takes about 4 s on a current x86-64 core. A PDF
    that runs out of it is refused `pdf_not_supported`, and so are the later PDFs of its upload, unrendered (the inbox
    leaves its group's other PDFs in the folder for its next scan); the log says "A PDF wasn't rendered within ...".
    Raise it on a slow NAS or ARM board whose card PDFs are refused that way. One process renders one PDF at a time,
    so a larger value also lets a hostile PDF hold up other uploads' PDFs longer.
    """
    PDF_UNCONFINED: bool = False
    """
    Render PDFs even where the system can't confine the renderer (its seccomp filter doesn't apply: an architecture
    other than x86-64 and arm64, a kernel or container without seccomp; Landlock alone, which leaves UDP, TCP below ABI
    4 and the server's process within reach, isn't enough), with only the protections that do apply (an isolated
    interpreter, its time and memory limits, Landlock where the kernel has it); off: such PDFs are refused
    `pdf_not_supported`, and the log says why. Run unconfined as root, a process the renderer starts can leave its
    process group and outlive the time limit. The server closes its end of the renderer's output at the time limit
    plus 5 s, so no server thread or pipe stays with it, but that process is left running.
    """

    # an empty variable is unset: Unraid and compose files pass unused ones as `''`
    model_config = SettingsConfigDict(env_prefix="AI_INGEST_", extra="ignore", env_ignore_empty=True)

    @field_validator("INBOX_DIR", "LOCK_DIR", "INBOX_PROCESSED_DAYS", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `Path` reads `''` as `.`, the current directory
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("INBOX_DIR_MODE", mode="before")
    @classmethod
    def _octal_mode(cls, value: object) -> str:
        # a mistyped mode never stops Mealie from starting (these settings are read at startup): it's logged, and the
        # inbox's new folders get the default
        text = str(value).strip().lower().removeprefix("0o")
        if not text:
            return DEFAULT_INBOX_DIR_MODE
        if any(digit not in "01234567" for digit in text) or int(text, 8) > 0o7777:
            logger.error(
                f"AI_INGEST_INBOX_DIR_MODE is {str(value)!r}, which isn't an octal file mode from 0 to 7777 (such as "
                f"2775): the recipe card inbox's new folders get {DEFAULT_INBOX_DIR_MODE}"
            )
            return DEFAULT_INBOX_DIR_MODE
        return format(int(text, 8), "o")

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * MIB

    @property
    def inbox_dir_mode(self) -> int:
        """`INBOX_DIR_MODE` as a number"""
        return int(self.INBOX_DIR_MODE, 8)

    @property
    def url_allow_hosts(self) -> list[str]:
        """`URL_ALLOW_HOSTS` as a list"""
        return [host.strip() for host in self.URL_ALLOW_HOSTS.split(",") if host.strip()]


@cache
def get_ingest_settings() -> IngestSettings:
    return IngestSettings()


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _mounted_inbox() -> Path | None:
    """`/inbox` when it's a folder with something mounted on it (a share), else None: an unmounted `/inbox` is just
    part of the container"""
    try:
        if os.path.isdir(MOUNTED_INBOX) and os.path.ismount(MOUNTED_INBOX):
            return MOUNTED_INBOX
    except OSError:
        pass
    return None


@cache
def inbox_root() -> Path | None:
    """
    The inbox folder, or None when it's off: `AI_INGEST_INBOX_DIR`, or when that's unset, `/inbox` if a folder is
    mounted there; refused when it's inside `DATA_DIR` or `/app` (backups would zip it, restores would wipe it and
    `entry.sh` would re-own it). `AI_INGEST_INBOX_DIR=/inbox` with nothing there (the Unraid template's value, its
    folder not mapped) is off too. A refusal, and an inbox found mounted, is logged once.
    """
    configured = get_ingest_settings().INBOX_DIR
    detected = configured is None
    if configured is None:
        configured = _mounted_inbox()
        if configured is None:
            return None
    elif configured == MOUNTED_INBOX and not os.path.isdir(MOUNTED_INBOX):
        logger.warning(
            f"AI_INGEST_INBOX_DIR is {MOUNTED_INBOX}, but no folder is mapped there: the recipe card inbox is off"
        )
        return None

    root = configured.expanduser().resolve()
    data_dir = get_app_dirs().DATA_DIR.resolve()
    variable = f"The folder mounted at {MOUNTED_INBOX}" if detected else f"AI_INGEST_INBOX_DIR ({root})"
    for forbidden in (data_dir, APP_DIR):
        if _is_within(root, forbidden) or _is_within(forbidden, root):
            logger.warning(f"{variable} overlaps {forbidden}, which isn't allowed: the recipe card inbox is off")
            return None

    if detected:
        logger.info(f"Recipe card inbox on at {MOUNTED_INBOX} (a mounted folder)")
    return root
