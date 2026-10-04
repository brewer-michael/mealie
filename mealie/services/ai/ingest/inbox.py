"""
The inbox folder (docs/ai/PHASE2.md §1.3): `AI_INGEST_INBOX_DIR/<group-slug>/<household-slug>/`, scanned by the
dispatcher. Files are taken once they've settled, claimed by an atomic rename, opened once with `O_NOFOLLOW` and passed
to intake, then moved to `processed/` (or `failed/` with the reason).

- **Off** unless `AI_INGEST_INBOX_DIR` is set and outside `DATA_DIR` and `/app` (`settings.inbox_root`), and on
  Windows (no directory-descriptor calls). Each scan creates every household's folder; unknown folders are logged once
  and ignored. Inbox jobs have no uploader.
- **In the household's language** (`household_locale`): an inbox card, its batch's "ready" notification, the "not
  added" notification and the notes in `failed/` take the language of the household's latest app or API batch (the
  `Accept-Language` its people capture and upload with), else en-US; a text that language lacks is in English.
- **A file is one card; a first-level subfolder is one multi-page card** (pages in name order); a multi-page TIFF or a
  PDF gives the card all its pages (`images.expand_document`, through intake). Skipped: anything that isn't a regular
  file or directory by `lstat` (symlinks included: each is logged once, since it stays for good), names starting with
  `.` or `~`, partial-download suffixes, `Thumbs.db`, `desktop.ini`, and the reserved `processed/`, `failed/` and
  `.mealie-claimed/`.
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
  in `failed/` has yet. The note is `recipe-ingest.inbox-rejected.*` (`rejection_note`): `Not added (<code>): …` in
  the household's language, its numbers from `limits`.
- **One bad entry or folder stops nothing else:** a file that can't be claimed and a folder that can't be scanned are
  logged once and skipped; names that aren't UTF-8 are taken like any other (shown with U+FFFD).
- **No write access** (`EACCES`/`EPERM` on the claim): moving a card folder needs write access to the folder itself,
  so one another user made under umask 022 (mode 2755) stays where it is. The log names the fix (Mealie's group needs
  write access: umask 002), and the app's inbox status lists it as `no_permission` until it's taken or gone
  (`household_status`). Every scanning process records it in `DATA_DIR/.ai-ingest-inbox/blocked.json` (with when it
  was first found), so a process that doesn't scan (a web process beside a worker), or has just started, lists it too.
- **Crash safety:** a claim older than `INBOX_CLAIM_RETRY` (by the time in its name) is claimed again by a second
  rename to a fresh claim time, so only one process retries it. A card already inserted is then found by its content
  hash, and the file is just moved to `processed/`.
- **Paused** for a restore: the scan stops before each file while the marker is set. A group that can't read cards, or
  is at its processing quota, keeps its files where they are until it can.
- **Folders Mealie creates** get `AI_INGEST_INBOX_DIR_MODE` (2775 by default: setgid and group-writable, so whatever
  writes the photos as a member of Mealie's group can write there), set on the open folder so the umask can't strip
  it. Folders that already exist are never changed.
- **`processed/` is purged** when `AI_INGEST_INBOX_PROCESSED_DAYS` is set: once a day per process (first 10 minutes
  after the first scan), each household's `processed/YYYY-MM/` loses the regular files processed longer ago than
  that, through the same descriptors and never through a link, at most `PURGE_ENTRIES` a run; a month folder left
  empty is removed. A file's processing time is the later of its mtime and its ctime (the move into `processed/` sets
  it; a photo copied with its old date keeps its mtime), and never before its month folder's first day.
- **Refusals are told:** each refused file logs one INFO line (its folder and the reason code, nothing of the file),
  and a burst of them sends the household one "Recipe cards not added" notification
  (`events.notify_inbox_rejections`): once a scan of the folder took everything it found, or two minutes after the
  burst's first refusal, whichever comes first (a long burst spans scans: `INBOX_FILES_PER_TICK`).
- **The app sees the folder** through `household_status`: how many photos wait and why (the scan's own gate), the
  photos the scan may not move, and the newest refusals in `failed/`, read through the same descriptors without
  following a link and without writing.
"""

import calendar
import errno
import fcntl
import json
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core.config import get_app_dirs
from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.db.models.group import Group
from mealie.db.models.household import Household
from mealie.lang.providers import Translator
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import (
    InboxWaitingReason,
    IngestInboxRejection,
    IngestRejectReason,
    IngestSource,
)
from mealie.services.ai.errors import IngestPaused

from . import events, images, limits, storage
from .i18n import DEFAULT_LOCALE, translator_for
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
OTHER_CODE = events.OTHER_REASON
"""How the log names a refusal without a reason code (a link, a device, an empty folder)"""
LOCALE_SOURCES = (IngestSource.app, IngestSource.api)
"""The batches whose language the household's inbox cards take: the ones people capture and upload with"""

NO_PERMISSION_HINT = "Mealie's group needs write access to the household folder, and to a card folder itself: umask 002"
"""What the log adds when a claim is refused for permissions (`IngestRejectReason.no_permission`)"""
_PERMISSION_ERRNOS = (errno.EACCES, errno.EPERM)

NAME_MAX_BYTES = 255
_CLAIM_NAME = re.compile(r"^(?P<ms>\d+)__(?P<token>[0-9a-f]{32})__(?P<name>.+)$", re.DOTALL)
_MONTH_NAME = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})$")

PURGE_ENTRIES = 5000
"""At most this many entries of `processed/` are looked at in one purge; a purge that stops there resumes next scan"""

_monotonic = time.monotonic
_wall_clock = time.time
"""The purge's clocks (tests move them)"""

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

NOTE_TEXTS = "recipe-ingest.inbox-rejected"
"""
The notes' texts: `.prefix` (`Not added ({code}):`) then `.<code>` (every reason has one; `.duplicate-of` names the
earlier card), or `.prefix-other` then `.other.<detail>` for a refusal without a code (`_Refused.detail`)
"""


class _Refused(Exception):
    """
    A claimed entry that can't be read safely (a symlink, a device, a path outside the inbox), or too many pages:
    `detail` names its note's text (`recipe-ingest.inbox-rejected.other.<detail>`, filled with `params`) when there's
    no reason code
    """

    def __init__(self, detail: str, reason: IngestRejectReason | None = None, **params: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.reason = reason
        self.params = params


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
class _Refusals:
    """A folder's refusals waiting to be told in one notification"""

    folder: HouseholdFolder
    since: float
    """When the first of them happened (`time.monotonic()`)"""
    reasons: list[IngestRejectReason | None] = field(default_factory=list)


@dataclass
class _ScanState:
    """What this process remembers between scans: each folder's entries as last seen, and what it already logged"""

    lock: threading.Lock = field(default_factory=threading.Lock)
    seen: dict[str, dict[str, tuple]] = field(default_factory=dict)
    logged: set[str] = field(default_factory=set)
    next_purge: float | None = None
    """When `processed/` is purged next (`time.monotonic()`); None until the first scan"""
    refusals: dict[str, _Refusals] = field(default_factory=dict)
    """Each folder's refusals not yet told (`notify_refusals`)"""
    blocked: dict[str, dict[str, float]] = field(default_factory=dict)
    """
    Each folder's entries its claim was refused for permissions (`no_permission`), with when that first happened
    (`time.time()`): listed by `household_status` until they're taken or gone
    """

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
            self.next_purge = None
            self.refusals.clear()
            self.blocked.clear()


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
    The directory `name` inside `dir_fd`, opened without following a link (and created first when asked, with
    `AI_INGEST_INBOX_DIR_MODE`). Raises `_UnsafeFolder` when it's a link or not a directory, `FileNotFoundError` when
    it's missing.
    """
    created = False
    mode = get_ingest_settings().inbox_dir_mode if create else 0
    if create:
        try:
            os.mkdir(name, mode, dir_fd=dir_fd)
            created = True
        except FileExistsError:
            pass  # a link or a file in its place fails below; an existing folder keeps its mode
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _UnsafeFolder(label) from e
        raise
    if created:
        _set_mode(fd, mode, label)
    return fd


def _set_mode(fd: int, mode: int, label: str) -> None:
    """A folder Mealie just created gets the configured mode in full: `mkdir` applied the umask (022: no group write)"""
    try:
        os.fchmod(fd, mode)
    except OSError as e:
        _state.log_once(f"mode:{label}", f"Couldn't set the mode of the recipe card inbox folder {label}: {e}")


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


def _log_link(label: str) -> None:
    """A link stays in the folder for good (it's never followed, moved or claimed), so it's logged once per name"""
    _state.log_once(
        f"link:{label}",
        f"Skipped {label} in the recipe card inbox: links aren't followed. Put the photo itself in the folder.",
    )


def _page_entries(dir_fd: int, label: str | None = None) -> list[tuple[str, os.stat_result]]:
    """
    A card folder's pages: its regular files by `lstat` (not subfolders or links) not ignored by name, by name. With
    the folder's `label`, a link among them is logged.
    """
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
            elif stat.S_ISLNK(st.st_mode) and label is not None:
                _log_link(f"{label}/{_display_name(entry.name)}")
    return sorted(pages, key=lambda page: page[0])


def _signature(entry: os.DirEntry, dir_fd: int, label: str) -> tuple | None:
    """
    What has to stay the same between two scans for an entry to count as settled, with its newest mtime; None when the
    entry isn't a card (not a regular file or directory by `lstat`, or a folder without pages). `label` names the
    entry in the logs; a link is logged.
    """
    st = entry.stat(follow_symlinks=False)
    if stat.S_ISREG(st.st_mode):
        return (("", st.st_size, st.st_mtime_ns),)
    if stat.S_ISLNK(st.st_mode):
        _log_link(label)
    if not stat.S_ISDIR(st.st_mode):
        return None

    card_fd = _open_dir(entry.name, dir_fd, entry.name)
    try:
        files = [(name, page.st_size, page.st_mtime_ns) for name, page in _page_entries(card_fd, label)]
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
                    signature = _signature(entry, dirs.fd, f"{folder.key}/{_display_name(entry.name)}")
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
        blocked = _state.blocked.get(folder.key, {})
        for name in blocked.keys() - current.keys():
            del blocked[name]  # gone, or no longer a card: no longer listed
    _forget_shared_blocked(folder, _shared_blocked().get(folder.key, {}).keys() - current.keys())

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
            raise _Refused("symbolic-link") from e
        raise _Refused("unreadable", error=e.strerror or str(e)) from e

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Refused("not-a-file")
        # without /proc, a page opened through the descriptors is inside the root by how they were opened
        real = _fd_path(fd) or (os.path.realpath(path) if dir_fd is None else None)
        if real is not None and not _within(real, os.path.realpath(root)):
            raise _Refused("outside-inbox")
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
        raise _Refused("not-a-file-or-folder")

    try:
        card_fd = _open_dir(claimed, claim_dir, claimed)
    except _UnsafeFolder as e:
        raise _Refused("not-a-file-or-folder") from e  # swapped for a link since

    opened: list[tuple[BinaryIO, str]] = []
    try:
        pages = _page_entries(card_fd)
        if not pages:
            raise _Refused("empty-folder")
        if len(pages) > limits.MAX_PAGES_PER_CARD:
            raise _Refused("too-many-pages", IngestRejectReason.too_many_pages)
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


def fail(dirs: _FolderDirs, claimed: str, name: str, reason: str, code: IngestRejectReason | None = None) -> None:
    """
    A card that can't be added: to `failed/` with `<name>.error.txt` saying why (`reason`). The note is a new file
    under a name nothing in `failed/` has yet, never written through a link or over anything there. One INFO line is
    logged with the folder and the reason `code` (`other` without one), nothing of the file: its name can be card text.
    """
    logger.info(
        f"A file in the recipe card inbox of {dirs.folder.key} wasn't added ({code.value if code else OTHER_CODE})"
    )
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


def _limit_params() -> dict[str, int]:
    """The numbers the notes name, from the limits as they are now"""
    return {
        "mib": limits.MAX_FILE_BYTES // limits.MIB,
        "megapixels": limits.MAX_PIXELS // 1_000_000,
        "jpeg_megapixels": images.MAX_JPEG_SOURCE_PIXELS // 1_000_000,
        "pages": limits.MAX_PAGES_PER_CARD,
    }


def rejection_note(translator: Translator, reason: IngestRejectReason, duplicate_of: UUID | None = None) -> str:
    """
    A refused file's note: `Not added (<code>): <why>`, in the translator's language. The code stays in it whatever
    the language: the status reads it back (`_note_reason`). A duplicate names the card it duplicates.
    """
    prefix = translator.t(f"{NOTE_TEXTS}.prefix", code=reason.value)
    if reason == IngestRejectReason.duplicate and duplicate_of is not None:
        text = translator.t(f"{NOTE_TEXTS}.duplicate-of", job=str(duplicate_of))
    else:
        text = translator.t(f"{NOTE_TEXTS}.{reason.value}", **_limit_params())
    return f"{prefix} {text}"


def _refusal_note(translator: Translator, refused: _Refused) -> str:
    """The note of a refusal `_open_card` raised: with its code when it has one, else `Not added: <what it is>`"""
    if refused.reason is not None:
        return rejection_note(translator, refused.reason)
    prefix = translator.t(f"{NOTE_TEXTS}.prefix-other")
    return f"{prefix} {translator.t(f'{NOTE_TEXTS}.other.{refused.detail}', **refused.params)}"


# ==================================================================================================================
# Purging processed/


@dataclass
class _Purge:
    cutoff: float
    """Files processed before this (seconds since the epoch) are removed"""
    current_month: str
    budget: int
    """Entries still to look at"""
    files: int = 0
    folders: int = 0

    @property
    def exhausted(self) -> bool:
        return self.budget <= 0


def _month_start(name: str) -> float | None:
    """The first instant (UTC) of a `YYYY-MM` folder's month; None for any other name"""
    match = _MONTH_NAME.match(name)
    if not match or not 1 <= int(match.group("month")) <= 12:
        return None
    return float(calendar.timegm((int(match.group("year")), int(match.group("month")), 1, 0, 0, 0)))


def _purge_month(processed: int, month_name: str, start: float, purge: _Purge, label: str) -> None:
    """Removes the month folder's regular files processed before the cutoff, then the folder if that emptied it"""
    month = _open_dir(month_name, processed, label)
    try:
        with os.scandir(month) as entries:
            for entry in entries:
                if purge.exhausted:
                    return
                purge.budget -= 1
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue  # a link, a folder: never followed or removed
                processed_at = max(st.st_mtime, st.st_ctime, start)
                if processed_at >= purge.cutoff:
                    continue
                try:
                    os.unlink(entry.name, dir_fd=month)
                except FileNotFoundError:
                    continue  # another process's purge
                purge.files += 1
        if month_name != purge.current_month and not os.listdir(month):
            try:
                os.rmdir(month_name, dir_fd=processed)
                purge.folders += 1
            except OSError:
                pass  # something arrived meanwhile, or another process removed it
    finally:
        os.close(month)


def _purge_folder(root_fd: int, folder: HouseholdFolder, purge: _Purge) -> None:
    """One household's `processed/`, oldest month first: only months begun before the cutoff can hold old files"""
    with _open_folder(root_fd, folder) as dirs:
        label = f"{folder.key}/{PROCESSED_DIR}"
        try:
            processed = _open_dir(PROCESSED_DIR, dirs.fd, label)
        except FileNotFoundError:
            return
        try:
            months = sorted(
                (start, name) for name in os.listdir(processed) if (start := _month_start(name)) is not None
            )
            for start, name in months:
                if purge.exhausted or start >= purge.cutoff:
                    return
                try:
                    _purge_month(processed, name, start, purge, f"{label}/{name}")
                except _UnsafeFolder as e:
                    _log_unsafe(e)
                except FileNotFoundError:
                    continue
        finally:
            os.close(processed)


def purge_processed(root_fd: int, folders: list[HouseholdFolder], days: int, now: float) -> _Purge:
    """
    Removes the files every household's `processed/YYYY-MM/` folders received more than `days` days before `now`
    (seconds since the epoch), at most `PURGE_ENTRIES` entries looked at; logs what it removed. One folder's trouble
    never stops the others.
    """
    purge = _Purge(
        cutoff=now - days * 86400,
        current_month=datetime.fromtimestamp(now, UTC).strftime("%Y-%m"),
        budget=PURGE_ENTRIES,
    )
    for folder in folders:
        if purge.exhausted:
            break
        try:
            _purge_folder(root_fd, folder, purge)
        except _UnsafeFolder as e:
            _log_unsafe(e)
        except OSError as e:
            _state.log_once(f"purge:{folder.key}", f"Couldn't clean up the recipe card inbox of {folder.key}: {e}")
    if purge.files or purge.folders:
        logger.info(
            f"Removed {purge.files} files processed more than {days} days ago (and {purge.folders} empty month "
            "folders) from the recipe card inbox"
        )
    return purge


def _purge_if_due(root_fd: int, folders: list[HouseholdFolder]) -> None:
    """
    `purge_processed` once a day per process, the first time `PURGE_FIRST_DELAY` after the first scan; again at the
    next scan when it stopped at `PURGE_ENTRIES`. Nothing while `AI_INGEST_INBOX_PROCESSED_DAYS` is unset.
    """
    days = get_ingest_settings().INBOX_PROCESSED_DAYS
    now = _monotonic()
    with _state.lock:
        if _state.next_purge is None:
            _state.next_purge = now + limits.PURGE_FIRST_DELAY
        if days is None or now < _state.next_purge:
            return
        _state.next_purge = now + limits.PURGE_INTERVAL
    purge = purge_processed(root_fd, folders, days, _wall_clock())
    if purge.exhausted:
        with _state.lock:
            _state.next_purge = now  # more to look at: the next scan goes on


# ==================================================================================================================
# What the app shows


STATUS_MAX_WAITING = 1000
"""Waiting photos are counted up to this many"""
STATUS_MAX_ENTRIES = 5000
"""Entries looked at in the household folder, and in its `failed/`, for one status"""
STATUS_REJECTIONS = 10
"""The newest refusals listed"""
_NOTE_READ_BYTES = 512
_NOTE_CODE = re.compile(r"[(\uff08](?P<code>[a-z_]+)[)\uff09]")
"""
A reason code in a note's first line, however its language words the rest (`Not added (too_large): …`), in ASCII or
full-width parentheses
"""


@dataclass(frozen=True)
class InboxStatus:
    """A household's inbox folder, as the cards page and the settings card show it"""

    waiting: int = 0
    """Settled photos (and card folders) in the folder, not yet taken; counting stops at `STATUS_MAX_WAITING`"""
    waiting_reason: InboxWaitingReason | None = None
    """Why they can't be taken now (`waiting_reason`); None when they can, or nothing waits"""
    rejections: list[IngestInboxRejection] = field(default_factory=list)
    """
    The entries the scan may not move (`no_permission`, still in the folder), then the newest files and card folders
    in `failed/` refused in the last `AI_INGEST_RETENTION_DAYS`, newest first: `STATUS_REJECTIONS` in all, unless more
    are stuck
    """


def household_status(
    group_slug: str | None, household_slug: str | None, readiness: ReadingReadiness | None = None
) -> InboxStatus:
    """
    What waits in a household's inbox folder, what the scan may not move (`no_permission`: what any process's scans
    found, from the shared record) and what it refused lately, read without writing anything or following a link
    (through the scan's descriptors, from the root down); an empty status when the inbox is off or the folder doesn't
    exist (yet).
    `readiness` is the group's (`intake.reading_readiness`), which says why photos wait; without it no reason is given.
    Blocking (file system): call it from a worker thread.
    """
    root = inbox_root()
    if root is None or not get_ingest_settings().ENABLED or not _SUPPORTED:
        return InboxStatus()
    if not (group_slug and household_slug and _safe_slug(group_slug) and _safe_slug(household_slug)):
        return InboxStatus()

    try:
        root_fd = os.open(root, _ROOT_FLAGS)
    except OSError:
        return InboxStatus()
    try:
        fd = _open_household(root_fd, group_slug, household_slug)
        if fd is None:
            return InboxStatus()
        try:
            blocked = _still_there(fd, _blocked(f"{group_slug}/{household_slug}"))  # every scanning process's
            waiting = _count_waiting(fd, time.time(), skip=blocked.keys())
            rejections = _recent_rejections(fd, time.time())
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)

    # what the scan may not move comes first: it stays in the folder until someone gives Mealie's group write access
    stuck = [
        IngestInboxRejection(
            name=_display_name(name), reason=IngestRejectReason.no_permission, at=datetime.fromtimestamp(at, UTC)
        )
        for name, at in sorted(blocked.items(), key=lambda item: (-item[1], item[0]))
    ]
    reason = waiting_reason(readiness) if waiting and readiness is not None else None
    return InboxStatus(
        waiting=waiting, waiting_reason=reason, rejections=(stuck + rejections)[: max(STATUS_REJECTIONS, len(stuck))]
    )


def _still_there(fd: int, blocked: dict[str, float]) -> dict[str, float]:
    """The entries of `blocked` the folder still has, as a file or a folder (never a link)"""
    there = {}
    for name, at in blocked.items():
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode):
            there[name] = at
    return there


def _open_household(root_fd: int, group_slug: str, household_slug: str) -> int | None:
    """The household's folder, opened without creating it or following a link; None when that can't be done"""
    try:
        group_fd = _open_dir(group_slug, root_fd, group_slug)
    except OSError:
        return None
    try:
        return _open_dir(household_slug, group_fd, f"{group_slug}/{household_slug}")
    except OSError:
        return None
    finally:
        os.close(group_fd)


def _count_waiting(fd: int, now: float, skip: Collection[str] = ()) -> int:
    """
    The folder's settled cards, as the scan would take them: regular files, and folders holding pages; not those in
    `skip` (the ones the scan may not move, listed instead)
    """
    settle = limits.INBOX_SETTLE
    waiting = 0
    looked = 0
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                looked += 1
                if waiting >= STATUS_MAX_WAITING or looked > STATUS_MAX_ENTRIES:
                    break
                if _ignored_name(entry.name) or entry.name in skip:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        newest = st.st_mtime
                    elif stat.S_ISDIR(st.st_mode):
                        card_fd = _open_dir(entry.name, fd, entry.name)
                        try:
                            pages = _page_entries(card_fd)
                        finally:
                            os.close(card_fd)
                        if not pages:
                            continue
                        newest = max(page.st_mtime for _, page in pages)
                    else:
                        continue  # a link or a device: never taken
                except OSError:
                    continue  # gone, unreadable, or swapped for a link
                if now - newest >= settle:
                    waiting += 1
    except OSError:
        return 0
    return waiting


def _recent_rejections(fd: int, now: float) -> list[IngestInboxRejection]:
    """
    The newest files and card folders in `failed/` (not their notes) refused within `AI_INGEST_RETENTION_DAYS`, newest
    first. A refusal's time is its note's (a move keeps the photo's own mtime), else the entry's ctime; its reason is
    the note's code.
    """
    try:
        failed = _open_dir(FAILED_DIR, fd, FAILED_DIR)
    except OSError:
        return []  # none yet, or a link: never followed
    try:
        found: dict[str, os.stat_result] = {}
        with os.scandir(failed) as entries:
            for looked, entry in enumerate(entries, start=1):
                if looked > STATUS_MAX_ENTRIES:
                    break
                try:
                    found[entry.name] = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

        cutoff = now - get_ingest_settings().RETENTION_DAYS * 86400
        refused: list[tuple[float, str]] = []
        for name, st in found.items():
            # a refused file, or a refused card folder (never a link)
            if name.endswith(ERROR_SUFFIX) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                continue
            note = found.get(name + ERROR_SUFFIX)
            if note is not None and stat.S_ISREG(note.st_mode):
                at = note.st_mtime
            else:
                at = max(st.st_mtime, st.st_ctime)
            if at >= cutoff:
                refused.append((at, name))

        newest = sorted(refused, reverse=True)[:STATUS_REJECTIONS]
        return [
            IngestInboxRejection(
                name=_display_name(name),
                reason=_note_reason(failed, name + ERROR_SUFFIX),
                at=datetime.fromtimestamp(at, UTC),
            )
            for at, name in newest
        ]
    except OSError:
        return []
    finally:
        os.close(failed)


def _note_reason(failed: int, note: str) -> IngestRejectReason | None:
    """
    The reason code a refusal's note gives in its first line (`Not added (<code>): …`, the first code in parentheses
    in whatever language it's written); None without a readable one
    """
    try:
        fd = os.open(note, _OPEN_FLAGS, dir_fd=failed)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        head = os.read(fd, _NOTE_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)
    first_line = head.split("\n", 1)[0]
    for match in _NOTE_CODE.finditer(first_line):
        try:
            return IngestRejectReason(match.group("code"))
        except ValueError:
            continue
    return None


# ==================================================================================================================
# The scan


def household_locale(session: Session, group_id: UUID, household_id: UUID) -> str:
    """
    The language a household's inbox cards take (they have no uploader): that of its latest app or API batch (the
    `Accept-Language` its people capture and upload with, `LOCALE_SOURCES`), else en-US. Ends the session's
    transaction. Never raises: a failed lookup is logged and gives en-US (the cards and notes are then in English).
    """
    try:
        locale = IngestRepos(session, group_id, household_id).batches.latest_locale(LOCALE_SOURCES)
    except Exception:
        logger.exception(f"Couldn't look up the language of household {household_id} for its recipe card inbox")
        if session.in_transaction():
            session.rollback()
        return DEFAULT_LOCALE
    if session.in_transaction():
        session.commit()
    return locale or DEFAULT_LOCALE


def waiting_reason(readiness: ReadingReadiness, taken: int = 0) -> InboxWaitingReason | None:
    """
    Why a group's inbox cards can't be taken now, by the upload API's checks 3 and 4: it can't read cards at all, or
    keeps them local with nothing local to read them, or is at its processing quota (counting `taken`, this scan's
    cards); None when they can be
    """
    if not readiness.can_read:
        return InboxWaitingReason.cannot_read
    if readiness.group_local_only and not readiness.local_ready:
        return InboxWaitingReason.local_only_unavailable
    if readiness.processing + taken >= limits.MAX_PROCESSING_JOBS_PER_GROUP:
        return InboxWaitingReason.quota
    return None


@dataclass
class _GroupGate:
    """Whether a group's inbox cards may be taken in this scan (`waiting_reason`); otherwise the files wait"""

    readiness: ReadingReadiness
    taken: int = 0

    @property
    def readable(self) -> bool:
        return waiting_reason(self.readiness) in (None, InboxWaitingReason.quota)

    @property
    def open(self) -> bool:
        return waiting_reason(self.readiness, self.taken) is None


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


@dataclass(frozen=True)
class _Taken:
    """What became of one claimed entry"""

    created: bool = False
    """A job was created"""
    refused: bool = False
    """It went to `failed/`"""
    reason: IngestRejectReason | None = None
    """Why it was refused, when there's a code for it"""


def _ingest_claimed(
    session: Session,
    root: Path,
    dirs: _FolderDirs,
    claimed: str,
    *,
    local_only: bool,
    recovered: bool,
    locale: str,
) -> _Taken:
    """Intake for one claimed entry, then where it goes; `locale` is the household's (`household_locale`)"""
    folder = dirs.folder
    parsed = _parse_claim(claimed)
    name = parsed[1] if parsed else claimed

    try:
        pages = _open_card(dirs, claimed, root)
    except FileNotFoundError:
        return _Taken()  # another scanner retried it
    except _Refused as e:
        fail(dirs, claimed, name, _refusal_note(translator_for(locale), e), e.reason)
        return _Taken(refused=True, reason=e.reason)

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
            locale=locale,
        )

        def still_claimed() -> bool:
            # a retry by another scanner would have renamed it
            return _lexists(claimed, dirs.claim_dir())

        outcome = IntakeService(session, folder.group_id, folder.household_id).ingest(
            card, options, confirm=still_claimed
        )
    except ClaimLost:
        return _Taken()
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
        return _Taken(created=True)

    assert isinstance(outcome, IntakeRejected)
    if outcome.reason == IngestRejectReason.duplicate and recovered:
        # a retried claim whose card was inserted before a crash: the job exists, so the file is just moved
        finish(dirs, claimed, name)
        return _Taken()

    note = rejection_note(translator_for(locale), outcome.reason, outcome.duplicate_of)
    fail(dirs, claimed, name, note, outcome.reason)
    return _Taken(refused=True, reason=outcome.reason)


@dataclass
class _FolderScan:
    claims: int = 0
    """Entries claimed (the scan's budget counts these)"""
    created: int = 0
    """Jobs created"""
    refused: int = 0
    """Entries refused (to `failed/`), recorded for the folder's next notification"""
    paused: bool = False
    """A restore paused ingestion: the scan stops"""
    complete: bool = False
    """Everything the folder had to take was taken: the scan wasn't stopped by its budget, the pause or the gate"""


def _scan_folder(
    session: Session, root: Path, dirs: _FolderDirs, gates: dict[UUID, _GroupGate], budget: int
) -> _FolderScan:
    """
    Retries the folder's stale claims, then takes its settled cards. An entry that can't be claimed or ingested is
    skipped (its claim, if any, is retried later); a reserved folder found to be a link stops the folder.
    """
    folder = dirs.folder
    result = _FolderScan()
    locale: str | None = None  # looked up once a card is taken
    work: list[tuple[str, str]] = [("stale", claimed_name) for claimed_name in stale_claims(dirs, _now_ms())]
    work += [("new", name) for name in _settled_entries(dirs, time.time())]

    for kind, name in work:
        if result.claims >= budget:
            return result
        if storage.is_paused():
            result.paused = True
            return result
        gate = _gate(session, folder, gates)
        if not gate.open:
            return result

        try:
            claimed = reclaim(dirs, name) if kind == "stale" else claim(dirs, name)
        except _UnsafeFolder:
            raise
        except OSError as e:
            # permissions, a name too long for a claim: the next entry is still taken
            hint = ""
            if kind == "new" and e.errno in _PERMISSION_ERRNOS:
                # moving a card folder needs write access to the folder itself: one another user made under umask
                # 022 stays where it is, so the app lists it until it's taken (`household_status`)
                _block(folder, name)
                hint = f" ({NO_PERMISSION_HINT})"
            _state.log_once(
                f"claim:{folder.key}/{name}",
                f"Couldn't take {_display_name(name)} from the recipe card inbox of {folder.key}: {e}{hint}",
            )
            continue
        if kind == "new":
            _unblock(folder, name)
        if claimed is None:
            continue
        result.claims += 1
        try:
            if locale is None:
                locale = household_locale(session, folder.group_id, folder.household_id)
            taken = _ingest_claimed(
                session,
                root,
                dirs,
                claimed,
                local_only=gate.readiness.group_local_only,
                recovered=kind == "stale",
                locale=locale,
            )
        except IngestPaused:
            result.paused = True  # the claim stays; it's retried after INBOX_CLAIM_RETRY
            return result
        except _UnsafeFolder:
            raise  # failed/ or processed/ was swapped for a link: the claim stays
        except Exception:
            # the claim stays and is retried later; the scan goes on with the next card
            logger.exception(f"Couldn't take a recipe card from the inbox of {folder.key}")
            continue
        if taken.created:
            result.created += 1
            gate.taken += 1
        if taken.refused:
            result.refused += 1
            _record_refusals(folder, [taken.reason])  # at once: a later error in this scan doesn't lose it
    result.complete = True
    return result


def _block(folder: HouseholdFolder, name: str) -> None:
    with _state.lock:
        first_found = _state.blocked.setdefault(folder.key, {}).setdefault(name, time.time())

    def record(folders: dict[str, dict[str, float]]) -> None:
        folders.setdefault(folder.key, {}).setdefault(name, first_found)  # another process may have found it first

    _update_shared_blocked(record)


def _unblock(folder: HouseholdFolder, name: str) -> None:
    with _state.lock:
        _state.blocked.get(folder.key, {}).pop(name, None)
    _forget_shared_blocked(folder, {name})


def _blocked(key: str) -> dict[str, float]:
    """
    The folder's entries the scan may not move (`no_permission`), with when a scan first found each: this process's,
    and what every scanning process recorded
    """
    with _state.lock:
        found = dict(_state.blocked.get(key, {}))
    for name, at in _shared_blocked().get(key, {}).items():
        found[name] = min(at, found.get(name, at))
    return found


# ==========================================
# The shared record of what the scans may not move


STATE_DIR_NAME = ".ai-ingest-inbox"
"""
`DATA_DIR/.ai-ingest-inbox/`: what the inbox's scans found that every process shows, a runtime folder backups leave
out. `blocked.json` holds each household folder's entries the scan may not move, with when one was first found; it's
rewritten atomically under `blocked.lock`, so scans in several processes don't lose each other's entries.
"""
BLOCKED_FILE = "blocked.json"
BLOCKED_LOCK_FILE = "blocked.lock"
BLOCKED_MAX_BYTES = 1024 * 1024
"""A larger record isn't read: the scanning process still lists what it found itself"""


def _blocked_path() -> Path:
    return get_app_dirs().DATA_DIR / STATE_DIR_NAME / BLOCKED_FILE


def _shared_blocked() -> dict[str, dict[str, float]]:
    """
    Every folder's entries the scans may not move, as the scanning processes recorded them for this inbox; empty
    when nothing was recorded, the record is another inbox folder's (it moved), or it can't be read
    """
    root = inbox_root()
    if root is None:
        return {}
    try:
        with _blocked_path().open("rb") as file:
            data = file.read(BLOCKED_MAX_BYTES + 1)
        record = json.loads(data) if len(data) <= BLOCKED_MAX_BYTES else None
    except OSError, ValueError:
        return {}
    if not isinstance(record, dict) or record.get("root") != str(root) or not isinstance(record.get("folders"), dict):
        return {}
    folders: dict[str, dict[str, float]] = {}
    for key, entries in record["folders"].items():
        if isinstance(entries, dict):
            folders[key] = {
                name: float(at)
                for name, at in entries.items()
                if isinstance(at, int | float) and not isinstance(at, bool)
            }
    return folders


def _update_shared_blocked(change: Callable[[dict[str, dict[str, float]]], None]) -> None:
    """
    `change` applied to the shared record, which is read and, when that changed it, written back atomically, all
    under its lock file. A record that can't be kept is logged once: this process still lists what it found.
    """
    root = inbox_root()
    if root is None:
        return
    path = _blocked_path()
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        lock = os.open(path.parent / BLOCKED_LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
            except OSError:
                pass  # a filesystem without locks: what another scan's write loses, its next scan records again
            folders = _shared_blocked()
            before = json.dumps(folders, sort_keys=True)
            change(folders)
            folders = {key: entries for key, entries in folders.items() if entries}
            if json.dumps(folders, sort_keys=True) != before:
                # ASCII, a name that isn't UTF-8 included (its stand-in characters are escaped)
                record = json.dumps({"root": str(root), "folders": folders}, sort_keys=True)
                storage.atomic_write_bytes(path, record.encode("ascii"))
        finally:
            os.close(lock)  # and the lock with it
    except OSError as e:
        _state.log_once(
            "blocked-record", f"Couldn't record what the recipe card inbox may not move in {path.parent}: {e}"
        )


def _forget_shared_blocked(folder: HouseholdFolder, names: Collection[str]) -> None:
    """Takes `names` out of the folder's shared record: taken, gone or no longer a card. Locks only when it has them."""
    recorded = _shared_blocked().get(folder.key, {})
    if not any(name in recorded for name in names):
        return

    def forget(folders: dict[str, dict[str, float]]) -> None:
        entries = folders.get(folder.key, {})
        for name in names:
            entries.pop(name, None)

    _update_shared_blocked(forget)


REFUSALS_BURST = limits.AUTO_BATCH_IDLE
"""A burst of refusals is told at the latest this long after its first, even while more keep coming"""


def _record_refusals(folder: HouseholdFolder, reasons: list[IngestRejectReason | None]) -> None:
    if not reasons:
        return
    with _state.lock:
        pending = _state.refusals.get(folder.key)
        if pending is None:
            pending = _state.refusals[folder.key] = _Refusals(folder, _monotonic())
        pending.reasons.extend(reasons)


def _due_refusals(complete: set[str]) -> list[HouseholdFolder]:
    """
    The folders whose refusals are told now: those whose scan took everything they had (the burst is over), and any
    whose burst began `REFUSALS_BURST` ago (it goes on over several scans, or the folder's scan keeps failing)
    """
    now = _monotonic()
    with _state.lock:
        return [
            pending.folder
            for key, pending in _state.refusals.items()
            if key in complete or now - pending.since >= REFUSALS_BURST
        ]


def notify_refusals(session: Session, folder: HouseholdFolder) -> bool:
    """
    One "Recipe cards not added" notification for the folder's refusals not yet told, with their counts by reason;
    whether one went out. Never raises (`events.notify_inbox_rejections`); the refusals are forgotten either way, and
    stay listed in `failed/` (`household_status`).
    """
    with _state.lock:
        pending = _state.refusals.pop(folder.key, None)
    if pending is None:
        return False
    locale = household_locale(session, folder.group_id, folder.household_id)
    return events.notify_inbox_rejections(
        folder.group_id, folder.household_id, pending.reasons, locale=locale, session=session
    )


def scan_once() -> int:
    """
    One scan of every household folder (skipped while paused), then `processed/`'s purge when it's due: the number of
    files ingested
    """
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
    complete: set[str] = set()  # folders whose scan took everything they had
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
                if scanned.complete:
                    complete.add(folder.key)
            if not storage.is_paused():
                for pending in _due_refusals(complete):
                    notify_refusals(session, pending)
        if not storage.is_paused():
            _purge_if_due(root_fd, folders)
    finally:
        os.close(root_fd)
    return created
