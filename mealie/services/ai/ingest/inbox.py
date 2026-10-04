"""
The inbox folder (docs/ai/PHASE2.md §1.3): `AI_INGEST_INBOX_DIR/<group-slug>/<household-slug>/`, scanned by the
dispatcher. Files are taken once they've settled, claimed by an atomic rename, opened once with `O_NOFOLLOW` and passed
to intake, then moved to `processed/` (or `failed/` with the reason).

- **Off** unless `AI_INGEST_INBOX_DIR` is set and outside `DATA_DIR` and `/app` (`settings.inbox_root`), and on
  Windows (no directory-descriptor calls). Each scan creates every household's folder; unknown folders are logged once
  and ignored. Inbox jobs have no uploader.
- **A file is one card; a first-level subfolder is one multi-page card** (pages in name order). Skipped: anything that
  isn't a regular file or directory by `lstat` (symlinks included), names starting with `.` or `~`, partial-download
  suffixes, `Thumbs.db`, `desktop.ini`, and the reserved `processed/`, `failed/` and `.mealie-claimed/`.
- **Settled:** an entry is taken once its `(size, mtime_ns)` (every file's, for a subfolder) is unchanged since this
  process's previous scan and at least `INBOX_SETTLE` old: cameras, SMB and scanners write in place.
- **Claim:** `os.rename` into `.mealie-claimed/<claim_ms>__<uuid>__<name>` beside it, on the share's own filesystem.
  A scanner that loses the race gets `FileNotFoundError`. The claim time is in the name because a rename (and
  Syncthing, rsync, `cp -p`) keeps the file's old mtime.
- **Never through a link:** anyone who can write to the share can plant symbolic links, so every file operation is
  relative to a directory descriptor opened with `O_DIRECTORY | O_NOFOLLOW` from the root down (group, household,
  reserved folder, card folder). A group, household or reserved folder that is a link or not a directory is skipped
  and logged once; nothing in or behind it is created, claimed, moved or written.
- **Open once:** every page is opened with `O_NOFOLLOW`, checked with `fstat` to be a regular file whose real path is
  inside the inbox root, and that file object goes to intake; nothing reopens it by path. Intake confirms the claimed
  entry still exists just before its insert commits. A card folder's pages are its regular files (subfolders and links
  are skipped); one with more than `MAX_PAGES_PER_CARD` is refused before any is opened.
- **Then** a rename to a unique name in `processed/YYYY-MM/` (or an unlink with `AI_INGEST_INBOX_KEEP_PROCESSED=false`);
  a rejected card goes to `failed/` with `<name>.error.txt`, created with `O_EXCL | O_NOFOLLOW` under a name nothing
  in `failed/` has yet.
- **One bad entry or folder stops nothing else:** a file that can't be claimed and a folder that can't be scanned are
  logged once and skipped; names that aren't UTF-8 are taken like any other (shown with U+FFFD).
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
from collections.abc import Iterator
from contextlib import contextmanager
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

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_ROOT_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
"""The root is the administrator's setting: it may be reached through a link"""
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_NOTE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and {os.open, os.mkdir, os.rename, os.stat, os.unlink, os.rmdir} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
    and shutil.rmtree.avoids_symlink_attacks
)
"""Whether every inbox operation can be made relative to a directory descriptor (Linux and macOS; not Windows)"""

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
    """A claimed entry that can't be read safely (a symlink, a device, a path outside the inbox), or too many pages"""

    def __init__(self, message: str, reason: IngestRejectReason | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class _UnsafeFolder(OSError):
    """A folder of the inbox that is a symbolic link or not a directory: never used, nor anything behind it"""

    def __init__(self, label: str) -> None:
        super().__init__(f"{label} is a symbolic link or not a folder")
        self.label = label


@dataclass(frozen=True)
class HouseholdFolder:
    group_id: UUID
    household_id: UUID
    group_slug: str
    household_slug: str

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

    def first(self, key: str) -> bool:
        """Whether `key` is new since it was last forgotten (and marks it seen)"""
        with self.lock:
            if key in self.logged:
                return False
            self.logged.add(key)
            return True

    def forget(self, key: str) -> None:
        with self.lock:
            self.logged.discard(key)

    def log_once(self, key: str, message: str) -> None:
        if self.first(key):
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
    return "/" not in slug and "\\" not in slug and "\0" not in slug and not slug.startswith(".")


def _display_name(name: str) -> str:
    """A file name as text that can be stored and logged: bytes that aren't UTF-8 (NFS, scanners) become U+FFFD"""
    return os.fsencode(name).decode("utf-8", errors="replace")


def _open_dir(name: str, dir_fd: int, label: str, *, create: bool = False) -> int:
    """
    The directory `name` inside `dir_fd`, opened without following a link (and created first when asked). Raises
    `_UnsafeFolder` when it's a link or not a directory, `FileNotFoundError` when it's missing.
    """
    if create:
        try:
            os.mkdir(name, dir_fd=dir_fd)
        except FileExistsError:
            pass  # a link or a file in its place fails below
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _UnsafeFolder(label) from e
        raise


def _lexists(name: str, dir_fd: int) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


class _FolderDirs:
    """
    A household folder opened for one scan. Every file operation on it goes through these descriptors, opened from
    the root down without following links, so a link swapped in for a folder later can't redirect anything.
    """

    def __init__(self, folder: HouseholdFolder, fd: int) -> None:
        self.folder = folder
        self.fd = fd
        self._claims: int | None = None

    def claim_dir(self) -> int:
        """`.mealie-claimed/`, created if missing"""
        if self._claims is None:
            self._claims = _open_dir(CLAIM_DIR, self.fd, f"{self.folder.key}/{CLAIM_DIR}", create=True)
        return self._claims

    def existing_claim_dir(self) -> int | None:
        if self._claims is None:
            try:
                self._claims = _open_dir(CLAIM_DIR, self.fd, f"{self.folder.key}/{CLAIM_DIR}")
            except FileNotFoundError:
                return None
        return self._claims

    def subfolder(self, name: str) -> int:
        """`processed/` or `failed/`, created if missing; the caller closes it"""
        return _open_dir(name, self.fd, f"{self.folder.key}/{name}", create=True)

    def close(self) -> None:
        for fd in (self._claims, self.fd):
            if fd is not None:
                os.close(fd)
        self._claims = None


@contextmanager
def _open_folder(root_fd: int, folder: HouseholdFolder) -> Iterator[_FolderDirs]:
    """
    A household folder for one scan; raises `_UnsafeFolder` when it, its group's folder or one of its reserved
    folders is a link or not a directory
    """
    group_fd = _open_dir(folder.group_slug, root_fd, folder.group_slug)
    try:
        fd = _open_dir(folder.household_slug, group_fd, folder.key)
    finally:
        os.close(group_fd)

    dirs = _FolderDirs(folder, fd)
    try:
        for reserved in sorted(RESERVED_NAMES):
            try:
                st = os.stat(reserved, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                raise _UnsafeFolder(f"{folder.key}/{reserved}")
        yield dirs
    finally:
        dirs.close()


def household_folders(session: Session, root_fd: int) -> list[HouseholdFolder]:
    """Every household's inbox folder, created if missing (idempotent); one that's a link is logged, never followed"""
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
        folder = HouseholdFolder(group_id, household_id, group_slug, household_slug)
        try:
            group_fd = _open_dir(group_slug, root_fd, group_slug, create=True)
            try:
                os.close(_open_dir(household_slug, group_fd, folder.key, create=True))
            finally:
                os.close(group_fd)
        except _UnsafeFolder as e:
            _log_unsafe(e)  # still the household's: the scan skips it
        except OSError as e:
            _state.log_once(f"mkdir:{folder.key}", f"Couldn't create the recipe card inbox folder {folder.key}: {e}")
            continue
        folders.append(folder)
    return folders


def _log_unsafe(error: _UnsafeFolder) -> None:
    _state.log_once(
        f"unsafe:{error.label}",
        f"The recipe card inbox folder {_display_name(error.label)} is a symbolic link or not a folder: it's skipped",
    )


def _log_unknown_folders(root_fd: int, folders: list[HouseholdFolder]) -> None:
    known: dict[str, set[str]] = {}
    for folder in folders:
        known.setdefault(folder.group_slug, set()).add(folder.household_slug)

    try:
        groups = os.listdir(root_fd)
    except OSError:
        return
    for group_name in groups:
        if _ignored_name(group_name):
            continue
        if group_name not in known:
            _state.log_once(
                f"unknown:{group_name}",
                "The recipe card inbox has an entry that isn't a group's folder: "
                f"{_display_name(group_name)} (ignored)",
            )
            continue
        try:
            group_fd = _open_dir(group_name, root_fd, group_name)
        except OSError:
            continue
        try:
            households = os.listdir(group_fd)
        except OSError:
            continue
        finally:
            os.close(group_fd)
        for household_name in households:
            if _ignored_name(household_name) or household_name in known[group_name]:
                continue
            _state.log_once(
                f"unknown:{group_name}/{household_name}",
                "The recipe card inbox has an entry that isn't a household's folder: "
                f"{group_name}/{_display_name(household_name)} (ignored)",
            )


def _ignored_name(name: str) -> bool:
    lowered = name.lower()
    return (
        name.startswith((".", "~"))
        or lowered.endswith(PARTIAL_SUFFIXES)
        or lowered in IGNORED_NAMES
        or name in RESERVED_NAMES
    )


def _page_entries(dir_fd: int) -> list[tuple[str, os.stat_result]]:
    """A card folder's pages: its regular files by `lstat` (not subfolders or links) not ignored by name, by name"""
    pages = []
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            if _ignored_name(entry.name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                pages.append((entry.name, st))
    return sorted(pages, key=lambda page: page[0])


def _signature(entry: os.DirEntry, dir_fd: int) -> tuple | None:
    """
    What has to stay the same between two scans for an entry to count as settled, with its newest mtime; None when the
    entry isn't a card (not a regular file or directory by `lstat`, or a folder without pages)
    """
    st = entry.stat(follow_symlinks=False)
    if stat.S_ISREG(st.st_mode):
        return (("", st.st_size, st.st_mtime_ns),)
    if not stat.S_ISDIR(st.st_mode):
        return None

    card_fd = _open_dir(entry.name, dir_fd, entry.name)
    try:
        files = [(name, page.st_size, page.st_mtime_ns) for name, page in _page_entries(card_fd)]
    finally:
        os.close(card_fd)
    return tuple(files) or None


def _settled_entries(dirs: _FolderDirs, now: float) -> list[str]:
    """The folder's new cards that have settled since this process's previous scan, oldest first"""
    folder = dirs.folder
    current: dict[str, tuple] = {}
    newest: dict[str, int] = {}
    try:
        with os.scandir(dirs.fd) as entries:
            for entry in entries:
                if _ignored_name(entry.name):
                    continue
                try:
                    signature = _signature(entry, dirs.fd)
                except OSError:
                    continue  # gone, unreadable, or swapped for a link
                if signature is None:
                    continue
                current[entry.name] = signature
                newest[entry.name] = max(mtime for _, _, mtime in signature)
    except OSError as e:
        _state.log_once(f"scan:{folder.key}", f"Couldn't read the recipe card inbox folder {folder.key}: {e}")
        return []

    with _state.lock:
        previous = _state.seen.get(folder.key, {})
        _state.seen[folder.key] = current

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
    room = NAME_MAX_BYTES - len(prefix)
    encoded = os.fsencode(name)  # a name that isn't UTF-8 keeps its bytes
    if len(encoded) > room:
        name = os.fsdecode(encoded[:room])
    return prefix + name


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def claim(dirs: _FolderDirs, name: str) -> str | None:
    """
    Takes the folder's entry `name` by renaming it into `.mealie-claimed/`: its name there, or None when another
    scanner got there first (or it's gone). The rename stays on the share's filesystem.
    """
    claimed = _claim_name(_now_ms(), name)
    try:
        os.rename(name, claimed, src_dir_fd=dirs.fd, dst_dir_fd=dirs.claim_dir())
    except FileNotFoundError:
        return None
    return claimed


def _parse_claim(claimed_name: str) -> tuple[int, str] | None:
    match = _CLAIM_NAME.match(claimed_name)
    if not match:
        return None
    return int(match.group("ms")), match.group("name")


def stale_claims(dirs: _FolderDirs, now_ms: int) -> list[str]:
    """Claims older than `INBOX_CLAIM_RETRY` by the time in their names (never by mtime), oldest first"""
    claim_dir = dirs.existing_claim_dir()
    if claim_dir is None:
        return []
    stale = []
    for claimed_name in os.listdir(claim_dir):
        parsed = _parse_claim(claimed_name)
        if parsed is None:
            _state.log_once(
                f"claim:{dirs.folder.key}/{claimed_name}",
                f"An entry in the recipe card inbox's claim folder of {dirs.folder.key} isn't a claim (ignored)",
            )
            continue
        if now_ms - parsed[0] > limits.INBOX_CLAIM_RETRY * 1000:
            stale.append((parsed[0], claimed_name))
    return [claimed_name for _, claimed_name in sorted(stale)]


def reclaim(dirs: _FolderDirs, claimed_name: str) -> str | None:
    """Claims a stale claim again under a fresh claim time, so only one process retries it; None if one already did"""
    parsed = _parse_claim(claimed_name)
    if parsed is None:
        return None
    claim_dir = dirs.claim_dir()
    fresh = _claim_name(_now_ms(), parsed[1])
    try:
        os.rename(claimed_name, fresh, src_dir_fd=claim_dir, dst_dir_fd=claim_dir)
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


def open_page(path: str | Path, root: Path, *, dir_fd: int | None = None) -> BinaryIO:
    """
    Opens a claimed page once (`path` relative to `dir_fd` when given): with `O_NOFOLLOW` (a symlink fails),
    `O_NONBLOCK` (a FIFO can't hang the scan), then `fstat` must show a regular file whose real path is inside the
    inbox root. Raises `_Refused` otherwise, and `FileNotFoundError` when it's gone.
    """
    try:
        fd = os.open(path, _OPEN_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise _Refused("a symbolic link, which is never followed") from e
        raise _Refused(f"unreadable ({e.strerror})") from e

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Refused("not a regular file")
        # without /proc, a page opened through the descriptors is inside the root by how they were opened
        real = _fd_path(fd) or (os.path.realpath(path) if dir_fd is None else None)
        if real is not None and not _within(real, os.path.realpath(root)):
            raise _Refused("outside the inbox folder")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _open_card(dirs: _FolderDirs, claimed: str, root: Path) -> list[tuple[BinaryIO, str]]:
    """
    The claimed entry's pages, opened: the file itself, or a folder's regular files in name order, refused with
    `too_many_pages` before any is opened when there are more than a card can have
    """
    claim_dir = dirs.claim_dir()
    st = os.stat(claimed, dir_fd=claim_dir, follow_symlinks=False)
    if stat.S_ISREG(st.st_mode):
        return [(open_page(claimed, root, dir_fd=claim_dir), "")]
    if not stat.S_ISDIR(st.st_mode):
        raise _Refused("not a regular file or folder")

    try:
        card_fd = _open_dir(claimed, claim_dir, claimed)
    except _UnsafeFolder as e:
        raise _Refused("not a regular file or folder") from e  # swapped for a link since

    opened: list[tuple[BinaryIO, str]] = []
    try:
        pages = _page_entries(card_fd)
        if not pages:
            raise _Refused("the folder is empty")
        if len(pages) > limits.MAX_PAGES_PER_CARD:
            raise _Refused("too many pages", IngestRejectReason.too_many_pages)
        for name, _ in pages:
            opened.append((open_page(name, root, dir_fd=card_fd), name))
    except BaseException:
        for file, _ in opened:
            file.close()
        raise
    finally:
        os.close(card_fd)
    return opened


# ==================================================================================================================
# Moving claimed entries on


def _unique_name(dir_fd: int, name: str, *, with_note: bool = False) -> str:
    """`name`, or a unique variant of it when the directory has that name already (or, `with_note`, its note's)"""

    def taken(candidate: str) -> bool:
        return _lexists(candidate, dir_fd) or (with_note and _lexists(candidate + ERROR_SUFFIX, dir_fd))

    if not taken(name):
        return name
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        stem, suffix = name, ""
    else:
        suffix = "." + suffix
    return f"{stem}-{_now_ms()}-{uuid4().hex[:8]}{suffix}"


def _remove(dir_fd: int, name: str) -> None:
    if stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
        shutil.rmtree(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def finish(dirs: _FolderDirs, claimed: str, name: str) -> None:
    """A card that's in Mealie: to `processed/YYYY-MM/` under a unique name, or deleted when nothing is kept"""
    if not get_ingest_settings().INBOX_KEEP_PROCESSED:
        _remove(dirs.claim_dir(), claimed)
        return
    processed = dirs.subfolder(PROCESSED_DIR)
    month_name = datetime.now(UTC).strftime("%Y-%m")
    try:
        month = _open_dir(month_name, processed, f"{dirs.folder.key}/{PROCESSED_DIR}/{month_name}", create=True)
    finally:
        os.close(processed)
    try:
        os.rename(claimed, _unique_name(month, name), src_dir_fd=dirs.claim_dir(), dst_dir_fd=month)
    finally:
        os.close(month)


def fail(dirs: _FolderDirs, claimed: str, name: str, reason: str) -> None:
    """
    A card that can't be added: to `failed/` with `<name>.error.txt` saying why. The note is a new file under a name
    nothing in `failed/` has yet, never written through a link or over anything there.
    """
    failed = dirs.subfolder(FAILED_DIR)
    try:
        target = _unique_name(failed, name, with_note=True)
        os.rename(claimed, target, src_dir_fd=dirs.claim_dir(), dst_dir_fd=failed)
        try:
            fd = os.open(target + ERROR_SUFFIX, _NOTE_FLAGS, 0o666, dir_fd=failed)
        except OSError as e:
            # planted since the name was chosen: the card is in failed/ all the same
            _state.log_once(
                f"note:{dirs.folder.key}/{target}",
                f"Couldn't write the note of {_display_name(target)} in the recipe card inbox of {dirs.folder.key}: "
                f"{e}",
            )
            return
        with os.fdopen(fd, "w", encoding="utf-8") as note:
            note.write(f"{reason}\n")
    finally:
        os.close(failed)


def _rejection(reason: IngestRejectReason) -> str:
    return f"Not added ({reason.value}): {REJECTION_TEXT[reason]}"


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
    session: Session, root: Path, dirs: _FolderDirs, claimed: str, *, local_only: bool, recovered: bool
) -> bool:
    """Intake for one claimed entry, then where it goes; whether a job was created"""
    folder = dirs.folder
    parsed = _parse_claim(claimed)
    name = parsed[1] if parsed else claimed

    try:
        pages = _open_card(dirs, claimed, root)
    except FileNotFoundError:
        return False  # another scanner retried it
    except _Refused as e:
        fail(dirs, claimed, name, _rejection(e.reason) if e.reason else f"Not added: {e}.")
        return False

    try:
        card = IntakeCard(
            pages=[
                IntakePage(file, _display_name(page_name or name), index)
                for index, (file, page_name) in enumerate(pages)
            ],
            source_name=source_name(f"inbox/{folder.key}", _display_name(name)),
        )
        options = IntakeOptions(
            source=IngestSource.inbox,
            source_key=folder.key,
            local_only=local_only,
            locale=INBOX_LOCALE,
        )

        def still_claimed() -> bool:
            # a retry by another scanner would have renamed it
            return _lexists(claimed, dirs.claim_dir())

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
            finish(dirs, claimed, name)
        except OSError:
            # the job exists: when the claim is retried, the content hash finds it and the file is just moved
            logger.exception(f"Couldn't move an ingested file out of the inbox claim folder of {folder.key}")
        return True

    assert isinstance(outcome, IntakeRejected)
    if outcome.reason == IngestRejectReason.duplicate and recovered:
        # a retried claim whose card was inserted before a crash: the job exists, so the file is just moved
        finish(dirs, claimed, name)
        return False

    reason = _rejection(outcome.reason)
    if outcome.duplicate_of:
        reason += f" Recipe card job {outcome.duplicate_of}."
    fail(dirs, claimed, name, reason)
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
    session: Session, root: Path, dirs: _FolderDirs, gates: dict[UUID, _GroupGate], budget: int
) -> _FolderScan:
    """
    Retries the folder's stale claims, then takes its settled cards. An entry that can't be claimed or ingested is
    skipped (its claim, if any, is retried later); a reserved folder found to be a link stops the folder.
    """
    folder = dirs.folder
    result = _FolderScan()
    work: list[tuple[str, str]] = [("stale", claimed_name) for claimed_name in stale_claims(dirs, _now_ms())]
    work += [("new", name) for name in _settled_entries(dirs, time.time())]

    for kind, name in work:
        if result.claims >= budget:
            break
        if storage.is_paused():
            result.paused = True
            break
        gate = _gate(session, folder, gates)
        if not gate.open:
            break

        try:
            claimed = reclaim(dirs, name) if kind == "stale" else claim(dirs, name)
        except _UnsafeFolder:
            raise
        except OSError as e:
            # permissions, a name too long for a claim: the next entry is still taken
            _state.log_once(
                f"claim:{folder.key}/{name}",
                f"Couldn't take {_display_name(name)} from the recipe card inbox of {folder.key}: {e}",
            )
            continue
        if claimed is None:
            continue
        result.claims += 1
        try:
            if _ingest_claimed(
                session,
                root,
                dirs,
                claimed,
                local_only=gate.readiness.group_local_only,
                recovered=kind == "stale",
            ):
                result.created += 1
                gate.taken += 1
        except IngestPaused:
            result.paused = True  # the claim stays; it's retried after INBOX_CLAIM_RETRY
            break
        except _UnsafeFolder:
            raise  # failed/ or processed/ was swapped for a link: the claim stays
        except Exception:
            # the claim stays and is retried later; the scan goes on with the next card
            logger.exception(f"Couldn't take a recipe card from the inbox of {folder.key}")
    return result


def scan_once() -> int:
    """One scan of every household folder (skipped while paused): the number of files ingested"""
    root = inbox_root()
    if root is None or not get_ingest_settings().ENABLED or storage.is_paused():
        return 0
    if not _SUPPORTED:
        _state.log_once("unsupported", "The recipe card inbox needs Linux or macOS; it's off on this system")
        return 0
    try:
        root_fd = os.open(root, _ROOT_FLAGS)
    except OSError:
        _state.log_once("root", f"The recipe card inbox {root} doesn't exist or isn't a folder")
        return 0

    created = 0
    budget = limits.INBOX_FILES_PER_TICK
    try:
        with session_context() as session:
            folders = household_folders(session, root_fd)
            _log_unknown_folders(root_fd, folders)
            gates: dict[UUID, _GroupGate] = {}
            for folder in folders:
                if budget <= 0:
                    break
                try:
                    with _open_folder(root_fd, folder) as dirs:
                        scanned = _scan_folder(session, root, dirs, gates, budget)
                except _UnsafeFolder as e:
                    _log_unsafe(e)
                    continue
                except Exception:
                    # one folder's trouble never stops the others; logged when it starts
                    if session.in_transaction():
                        session.rollback()
                    if _state.first(f"failing:{folder.key}"):
                        logger.exception(f"Couldn't scan the recipe card inbox folder {folder.key}; it's retried")
                    continue
                _state.forget(f"failing:{folder.key}")
                budget -= scanned.claims
                created += scanned.created
                if scanned.paused:
                    break
    finally:
        os.close(root_fd)
    return created
