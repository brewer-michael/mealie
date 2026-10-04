"""
The inbox folder (docs/ai/PHASE2.md §1.3): `AI_INGEST_INBOX_DIR/<group-slug>/<household-slug>/`, scanned by the
dispatcher. Files are taken once they've settled, claimed by an atomic rename, opened once with `O_NOFOLLOW` and passed
to intake, then moved to `processed/` (or `failed/` with the reason).

- **Off** unless `AI_INGEST_INBOX_DIR` is set and outside `DATA_DIR` and `/app` (`settings.inbox_root`). Each scan
  creates every household's folder; unknown folders are logged once and ignored. Inbox jobs have no uploader.
- **A file is one card; a first-level subfolder is one multi-page card** (pages in name order). Skipped: anything that
  isn't a regular file or directory by `lstat` (symlinks included), names starting with `.` or `~`, partial-download
  suffixes, `Thumbs.db`, `desktop.ini`, and the reserved `processed/`, `failed/` and `.mealie-claimed/`.
- **Settled:** an entry is taken once its `(size, mtime_ns)` (every file's, for a subfolder) is unchanged since this
  process's previous scan and at least `INBOX_SETTLE` old: cameras, SMB and scanners write in place.
- **Claim:** `os.rename` into `.mealie-claimed/<claim_ms>__<uuid>__<name>` beside it, on the share's own filesystem.
  A scanner that loses the race gets `FileNotFoundError`. The claim time is in the name because a rename (and
  Syncthing, rsync, `cp -p`) keeps the file's old mtime.
- **Open once:** every page is opened with `O_NOFOLLOW`, checked with `fstat` to be a regular file whose real path is
  inside the inbox root, and that file object goes to intake; nothing reopens it by path. Intake confirms the claimed
  path still exists just before its insert commits.
- **Then** a rename to a unique name in `processed/YYYY-MM/` (or an unlink with `AI_INGEST_INBOX_KEEP_PROCESSED=false`);
  a rejected card goes to `failed/` with `<name>.error.txt`.
- **Crash safety:** a claim older than `INBOX_CLAIM_RETRY` (by the time in its name) is claimed again by a second
  rename to a fresh claim time, so only one process retries it. A card already inserted is then found by its content
  hash, and the file is just moved to `processed/`.
- **Paused** for a restore: the scan stops before each file while the marker is set. A group that can't read cards, or
  is at its processing quota, keeps its files where they are until it can.
"""

import errno
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.db.models.group import Group
from mealie.db.models.household import Household
from mealie.schema.recipe_ingest import IngestRejectReason, IngestSource
from mealie.services.ai.errors import IngestPaused

from . import limits, storage
from .intake import (
    ClaimLost,
    IntakeAccepted,
    IntakeCard,
    IntakeOptions,
    IntakePage,
    IntakeRejected,
    IntakeService,
    ReadingReadiness,
    reading_readiness,
    source_name,
)
from .settings import get_ingest_settings, inbox_root

logger = get_logger(__name__)

CLAIM_DIR = ".mealie-claimed"
PROCESSED_DIR = "processed"
FAILED_DIR = "failed"
RESERVED_NAMES = frozenset({CLAIM_DIR, PROCESSED_DIR, FAILED_DIR})
PARTIAL_SUFFIXES = (".tmp", ".part", ".crdownload", ".partial", ".download", ".filepart")
IGNORED_NAMES = frozenset({"thumbs.db", "desktop.ini"})
ERROR_SUFFIX = ".error.txt"
INBOX_LOCALE = "en-US"
"""Inbox cards have no uploader whose language they could take"""

NAME_MAX_BYTES = 255
_CLAIM_NAME = re.compile(r"^(?P<ms>\d+)__(?P<token>[0-9a-f]{32})__(?P<name>.+)$", re.DOTALL)

REJECTION_TEXT = {
    IngestRejectReason.too_large: "The photo is larger than 30 MB.",
    IngestRejectReason.unsupported_format: "This isn't a supported image. Use JPEG, PNG, WebP, HEIC, AVIF or TIFF.",
    IngestRejectReason.pdf_not_supported: "PDFs can't be scanned. Save a photo of the card instead.",
    IngestRejectReason.too_many_pixels: "The photo has more than 100 megapixels.",
    IngestRejectReason.unreadable_image: "The image couldn't be read. It may be damaged or incomplete.",
    IngestRejectReason.too_many_pages: "A folder can hold at most 4 pages of one card.",
    IngestRejectReason.duplicate: "This card was already scanned.",
}


class _Refused(Exception):
    """A claimed entry that can't be read safely (a symlink, a device, a path outside the inbox)"""


@dataclass(frozen=True)
class HouseholdFolder:
    group_id: UUID
    household_id: UUID
    group_slug: str
    household_slug: str
    path: Path

    @property
    def key(self) -> str:
        """`<group-slug>/<household-slug>`: the batch source key, and how logs name the folder"""
        return f"{self.group_slug}/{self.household_slug}"


@dataclass
class _ScanState:
    """What this process remembers between scans: each folder's entries as last seen, and what it already logged"""

    lock: threading.Lock = field(default_factory=threading.Lock)
    seen: dict[str, dict[str, tuple]] = field(default_factory=dict)
    logged: set[str] = field(default_factory=set)

    def log_once(self, key: str, message: str) -> None:
        with self.lock:
            if key in self.logged:
                return
            self.logged.add(key)
        logger.warning(message)

    def reset(self) -> None:
        with self.lock:
            self.seen.clear()
            self.logged.clear()


_state = _ScanState()


def reset_state() -> None:
    """Forgets what earlier scans saw (for tests)"""
    _state.reset()


# ==================================================================================================================
# Folders and entries


def _safe_slug(slug: str | None) -> bool:
    """A slug usable as one path component"""
    if not slug:
        return False
    return "/" not in slug and "\\" not in slug and not slug.startswith(".")


def household_folders(session: Session, root: Path) -> list[HouseholdFolder]:
    """Every household's inbox folder, created if missing (idempotent)"""
    rows = session.execute(
        sa.select(Group.id, Group.slug, Household.id, Household.slug)
        .join(Household, Household.group_id == Group.id)
        .order_by(Group.slug, Household.slug)
    ).all()
    if session.in_transaction():
        session.commit()

    folders: list[HouseholdFolder] = []
    for group_id, group_slug, household_id, household_slug in rows:
        if not (_safe_slug(group_slug) and _safe_slug(household_slug)):
            continue
        folder = HouseholdFolder(group_id, household_id, group_slug, household_slug, root / group_slug / household_slug)
        try:
            folder.path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _state.log_once(f"mkdir:{folder.key}", f"Couldn't create the recipe card inbox folder {folder.key}: {e}")
            continue
        folders.append(folder)
    return folders


def _log_unknown_folders(root: Path, folders: list[HouseholdFolder]) -> None:
    known: dict[str, set[str]] = {}
    for folder in folders:
        known.setdefault(folder.group_slug, set()).add(folder.household_slug)

    try:
        groups = list(os.scandir(root))
    except OSError:
        return
    for group_entry in groups:
        if _ignored_name(group_entry.name):
            continue
        if group_entry.name not in known:
            _state.log_once(
                f"unknown:{group_entry.name}",
                f"The recipe card inbox has an entry that isn't a group's folder: {group_entry.name} (ignored)",
            )
            continue
        try:
            households = list(os.scandir(group_entry.path))
        except OSError:
            continue
        for household_entry in households:
            if _ignored_name(household_entry.name) or household_entry.name in known[group_entry.name]:
                continue
            _state.log_once(
                f"unknown:{group_entry.name}/{household_entry.name}",
                "The recipe card inbox has an entry that isn't a household's folder: "
                f"{group_entry.name}/{household_entry.name} (ignored)",
            )


def _ignored_name(name: str) -> bool:
    lowered = name.lower()
    return (
        name.startswith((".", "~"))
        or lowered.endswith(PARTIAL_SUFFIXES)
        or lowered in IGNORED_NAMES
        or name in RESERVED_NAMES
    )


def _page_entries(directory: str) -> list[os.DirEntry]:
    """A card folder's files that aren't ignored by name, sorted by name"""
    with os.scandir(directory) as entries:
        return sorted((entry for entry in entries if not _ignored_name(entry.name)), key=lambda entry: entry.name)


def _signature(entry: os.DirEntry) -> tuple | None:
    """
    What has to stay the same between two scans for an entry to count as settled, with its newest mtime; None when the
    entry isn't a card (not a regular file or directory by `lstat`, or an empty folder)
    """
    st = entry.stat(follow_symlinks=False)
    if stat.S_ISREG(st.st_mode):
        return (("", st.st_size, st.st_mtime_ns),)
    if not stat.S_ISDIR(st.st_mode):
        return None

    files = []
    for page in _page_entries(entry.path):
        page_st = page.stat(follow_symlinks=False)
        if stat.S_ISREG(page_st.st_mode):
            files.append((page.name, page_st.st_size, page_st.st_mtime_ns))
    return tuple(files) or None


def _settled_entries(folder: HouseholdFolder, now: float) -> list[str]:
    """The folder's new cards that have settled since this process's previous scan, oldest first"""
    current: dict[str, tuple] = {}
    newest: dict[str, int] = {}
    try:
        with os.scandir(folder.path) as entries:
            for entry in entries:
                if _ignored_name(entry.name):
                    continue
                try:
                    signature = _signature(entry)
                except OSError:
                    continue  # gone, or unreadable
                if signature is None:
                    continue
                current[entry.name] = signature
                newest[entry.name] = max(mtime for _, _, mtime in signature)
    except OSError as e:
        _state.log_once(f"scan:{folder.key}", f"Couldn't read the recipe card inbox folder {folder.key}: {e}")
        return []

    key = str(folder.path)
    with _state.lock:
        previous = _state.seen.get(key, {})
        _state.seen[key] = current

    settle_ns = limits.INBOX_SETTLE * 1_000_000_000
    now_ns = int(now * 1_000_000_000)
    ready = [
        name
        for name, signature in current.items()
        if previous.get(name) == signature and now_ns - newest[name] >= settle_ns
    ]
    return sorted(ready, key=lambda name: (newest[name], name))


# ==================================================================================================================
# Claims


def _claim_name(claim_ms: int, name: str) -> str:
    prefix = f"{claim_ms}__{uuid4().hex}__"
    room = NAME_MAX_BYTES - len(prefix.encode())
    encoded = name.encode()
    if len(encoded) > room:
        name = encoded[:room].decode(errors="ignore")
    return prefix + name


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def claim(folder: HouseholdFolder, name: str) -> Path | None:
    """
    Takes `<folder>/<name>` by renaming it into `.mealie-claimed/`; None when another scanner got there first (or it's
    gone). The rename stays on the share's filesystem.
    """
    claim_dir = folder.path / CLAIM_DIR
    claim_dir.mkdir(exist_ok=True)
    claimed = claim_dir / _claim_name(_now_ms(), name)
    try:
        os.rename(folder.path / name, claimed)
    except FileNotFoundError:
        return None
    return claimed


def _parse_claim(claimed_name: str) -> tuple[int, str] | None:
    match = _CLAIM_NAME.match(claimed_name)
    if not match:
        return None
    return int(match.group("ms")), match.group("name")


def stale_claims(folder: HouseholdFolder, now_ms: int) -> list[str]:
    """Claims older than `INBOX_CLAIM_RETRY` by the time in their names (never by mtime), oldest first"""
    claim_dir = folder.path / CLAIM_DIR
    try:
        names = os.listdir(claim_dir)
    except FileNotFoundError:
        return []
    stale = []
    for claimed_name in names:
        parsed = _parse_claim(claimed_name)
        if parsed is None:
            _state.log_once(
                f"claim:{folder.key}/{claimed_name}",
                f"An entry in the recipe card inbox's claim folder of {folder.key} isn't a claim (ignored)",
            )
            continue
        if now_ms - parsed[0] > limits.INBOX_CLAIM_RETRY * 1000:
            stale.append((parsed[0], claimed_name))
    return [claimed_name for _, claimed_name in sorted(stale)]


def reclaim(folder: HouseholdFolder, claimed_name: str) -> Path | None:
    """Claims a stale claim again under a fresh claim time, so only one process retries it; None if one already did"""
    parsed = _parse_claim(claimed_name)
    if parsed is None:
        return None
    claim_dir = folder.path / CLAIM_DIR
    fresh = claim_dir / _claim_name(_now_ms(), parsed[1])
    try:
        os.rename(claim_dir / claimed_name, fresh)
    except FileNotFoundError:
        return None
    return fresh


# ==================================================================================================================
# Opening claimed files


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _fd_path(fd: int) -> str | None:
    """Where an open file really is (Linux), whatever path opened it"""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def open_page(path: Path, root: Path) -> BinaryIO:
    """
    Opens a claimed page once: with `O_NOFOLLOW` (a symlink fails), `O_NONBLOCK` (a FIFO can't hang the scan), then
    `fstat` must show a regular file whose real path is inside the inbox root. Raises `_Refused` otherwise, and
    `FileNotFoundError` when it's gone.
    """
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        raise
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise _Refused("a symbolic link, which is never followed") from e
        raise _Refused(f"unreadable ({e.strerror})") from e

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Refused("not a regular file")
        real = _fd_path(fd) or os.path.realpath(path)
        if not _within(real, os.path.realpath(root)):
            raise _Refused("outside the inbox folder")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _open_card(claimed: Path, root: Path) -> list[tuple[BinaryIO, str]]:
    """The claimed entry's pages, opened: the file itself, or a folder's files in name order"""
    st = claimed.lstat()
    if stat.S_ISREG(st.st_mode):
        return [(open_page(claimed, root), "")]
    if not stat.S_ISDIR(st.st_mode):
        raise _Refused("not a regular file or folder")

    opened: list[tuple[BinaryIO, str]] = []
    try:
        if not _within(os.path.realpath(claimed), os.path.realpath(root)):
            raise _Refused("outside the inbox folder")
        for entry in _page_entries(str(claimed)):
            opened.append((open_page(Path(entry.path), root), entry.name))
        if not opened:
            raise _Refused("the folder is empty")
    except BaseException:
        for file, _ in opened:
            file.close()
        raise
    return opened


# ==================================================================================================================
# Moving claimed entries on


def _unique_target(directory: Path, name: str) -> Path:
    target = directory / name
    if not os.path.lexists(target):
        return target
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        stem, suffix = name, ""
    else:
        suffix = "." + suffix
    return directory / f"{stem}-{_now_ms()}-{uuid4().hex[:8]}{suffix}"


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def finish(folder: HouseholdFolder, claimed: Path, name: str) -> None:
    """A card that's in Mealie: to `processed/YYYY-MM/` under a unique name, or deleted when nothing is kept"""
    if not get_ingest_settings().INBOX_KEEP_PROCESSED:
        _remove(claimed)
        return
    month = folder.path / PROCESSED_DIR / datetime.now(UTC).strftime("%Y-%m")
    month.mkdir(parents=True, exist_ok=True)
    os.rename(claimed, _unique_target(month, name))


def fail(folder: HouseholdFolder, claimed: Path, name: str, reason: str) -> None:
    """A card that can't be added: to `failed/` with `<name>.error.txt` saying why"""
    failed = folder.path / FAILED_DIR
    failed.mkdir(exist_ok=True)
    target = _unique_target(failed, name)
    os.rename(claimed, target)
    note = target.with_name(target.name + ERROR_SUFFIX)
    note.write_text(f"{reason}\n", encoding="utf-8")


# ==================================================================================================================
# The scan


@dataclass
class _GroupGate:
    """
    Whether a group's inbox cards may be taken in this scan: the upload API's checks 3 and 4. It can read cards (with
    local providers, when it keeps cards local) and is under its processing quota; otherwise the files wait.
    """

    readiness: ReadingReadiness
    taken: int = 0

    @property
    def readable(self) -> bool:
        return self.readiness.can_read and (self.readiness.local_ready or not self.readiness.group_local_only)

    @property
    def open(self) -> bool:
        return self.readable and self.readiness.processing + self.taken < limits.MAX_PROCESSING_JOBS_PER_GROUP


def _gate(session: Session, folder: HouseholdFolder, gates: dict[UUID, _GroupGate]) -> _GroupGate:
    gate = gates.get(folder.group_id)
    if gate is None:
        gate = _GroupGate(reading_readiness(session, folder.group_id, folder.household_id))
        gates[folder.group_id] = gate
        if not gate.readable:
            _state.log_once(
                f"cannot-read:{folder.group_id}",
                f"Recipe cards in the inbox of {folder.group_slug} wait until the group can read them (AI providers)",
            )
    return gate


def _ingest_claimed(
    session: Session, root: Path, folder: HouseholdFolder, claimed: Path, *, local_only: bool, recovered: bool
) -> bool:
    """Intake for one claimed entry, then where it goes; whether a job was created"""
    parsed = _parse_claim(claimed.name)
    name = parsed[1] if parsed else claimed.name

    try:
        pages = _open_card(claimed, root)
    except FileNotFoundError:
        return False  # another scanner retried it
    except _Refused as e:
        fail(folder, claimed, name, f"Not added: {e}.")
        return False

    try:
        card = IntakeCard(
            pages=[IntakePage(file, page_name or name, index) for index, (file, page_name) in enumerate(pages)],
            source_name=source_name(f"inbox/{folder.key}", name),
        )
        options = IntakeOptions(
            source=IngestSource.inbox,
            source_key=folder.key,
            local_only=local_only,
            locale=INBOX_LOCALE,
        )

        def still_claimed() -> bool:
            # a retry by another scanner would have renamed it
            return os.path.lexists(claimed)

        outcome = IntakeService(session, folder.group_id, folder.household_id).ingest(
            card, options, confirm=still_claimed
        )
    except ClaimLost:
        return False
    finally:
        for file, _ in pages:
            file.close()

    if isinstance(outcome, IntakeAccepted):
        logger.info(f"Recipe card job {outcome.job_id} from the inbox of {folder.key}")
        try:
            finish(folder, claimed, name)
        except OSError:
            # the job exists: when the claim is retried, the content hash finds it and the file is just moved
            logger.exception(f"Couldn't move an ingested file out of the inbox claim folder of {folder.key}")
        return True

    assert isinstance(outcome, IntakeRejected)
    if outcome.reason == IngestRejectReason.duplicate and recovered:
        # a retried claim whose card was inserted before a crash: the job exists, so the file is just moved
        finish(folder, claimed, name)
        return False

    reason = f"Not added ({outcome.reason.value}): {REJECTION_TEXT[outcome.reason]}"
    if outcome.duplicate_of:
        reason += f" Recipe card job {outcome.duplicate_of}."
    fail(folder, claimed, name, reason)
    return False


@dataclass
class _FolderScan:
    claims: int = 0
    """Entries claimed (the scan's budget counts these)"""
    created: int = 0
    """Jobs created"""
    paused: bool = False
    """A restore paused ingestion: the scan stops"""


def _scan_folder(
    session: Session, root: Path, folder: HouseholdFolder, gates: dict[UUID, _GroupGate], budget: int
) -> _FolderScan:
    """Retries the folder's stale claims, then takes its settled cards"""
    result = _FolderScan()
    work: list[tuple[str, str]] = [("stale", claimed_name) for claimed_name in stale_claims(folder, _now_ms())]
    work += [("new", name) for name in _settled_entries(folder, time.time())]

    for kind, name in work:
        if result.claims >= budget:
            break
        if storage.is_paused():
            result.paused = True
            break
        gate = _gate(session, folder, gates)
        if not gate.open:
            break

        claimed = reclaim(folder, name) if kind == "stale" else claim(folder, name)
        if claimed is None:
            continue
        result.claims += 1
        try:
            if _ingest_claimed(
                session,
                root,
                folder,
                claimed,
                local_only=gate.readiness.group_local_only,
                recovered=kind == "stale",
            ):
                result.created += 1
                gate.taken += 1
        except IngestPaused:
            result.paused = True  # the claim stays; it's retried after INBOX_CLAIM_RETRY
            break
        except Exception:
            # the claim stays and is retried later; the scan goes on with the next card
            logger.exception(f"Couldn't take a recipe card from the inbox of {folder.key}")
    return result


def scan_once() -> int:
    """One scan of every household folder (skipped while paused): the number of files ingested"""
    root = inbox_root()
    if root is None or not get_ingest_settings().ENABLED or storage.is_paused():
        return 0
    if not root.is_dir():
        _state.log_once("root", f"The recipe card inbox {root} doesn't exist or isn't a folder")
        return 0

    created = 0
    budget = limits.INBOX_FILES_PER_TICK
    with session_context() as session:
        folders = household_folders(session, root)
        _log_unknown_folders(root, folders)
        gates: dict[UUID, _GroupGate] = {}
        for folder in folders:
            if budget <= 0:
                break
            scanned = _scan_folder(session, root, folder, gates, budget)
            budget -= scanned.claims
            created += scanned.created
            if scanned.paused:
                break
    return created
