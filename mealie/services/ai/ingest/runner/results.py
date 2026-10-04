"""
Results a backup restore cut off (docs/ai/PHASE2.md §3.9): a restore queues every running task again (its lease is
gone), so a task that was reading a card then has its result refused by the fence. Rather than pay for the reading
again, the result is kept, and the card's next task applies it through the normal fenced finalize without calling a
provider. A task the restore stopped part way (its next step met the dropped tables) keeps the provider answers it
got instead (`answers.KeptAnswers`), which the next task replays rather than asking for them again.

- **Kept:** when a task's outcome is refused or given back while a restore pauses ingestion, or after one ended since
  the task began (`storage.restored_since`), the worker keeps its result, or else its provider answers, in
  `DATA_DIR/.ai-ingest-results/<job_id>.<kind>.json` (written atomically, owner only), with what they were computed
  from: the task's kind, a hash of its `task_payload` and the page hashes (an extraction's own `pages`, which
  orientation may have turned; else the job's pages as the task read them).
- **Applied:** the job's next task of the same kind, payload and page hashes, within `KEPT_RESULT_TTL`, applies the
  result (or replays the answers, each only for a request with the same inputs) and removes it once its own outcome is
  stored. Anything else (other pages, as a restore from an older backup brings back; another kind or payload; too
  old; unreadable) removes it, and the card is read as usual.
- **Still running:** a task the restore cut off may still be waiting for its provider when the card's next task
  starts (the dispatcher doesn't stop a task whose lease a restore took). Each task marks itself in flight
  (`<job_id>.<token>.inflight`) while it runs, so the next task waits for the result of one that began before the
  restore and is still alive (for at most `KEPT_RESULT_WAIT`) instead of reading the card a second time.

The purge removes kept results and in-flight marks older than `KEPT_RESULT_TTL` (`purge`). Backups leave the folder out
and restores leave it alone. Nothing here raises: a kept result is an optimization, never a reason for a task to fail.
"""

import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from mealie.core.root_logger import get_logger
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardFlag,
    CardProposal,
    CardProposalOrigin,
    ExtractionMeta,
    PageMeta,
)

from .. import limits, storage
from .answers import KeptAnswers
from .types import ExtractResult, ParseLinesResult, RereadResult

logger = get_logger(__name__)

KeptResult = ExtractResult | RereadResult | ParseLinesResult
"""The results a restore can cut off and the next task applies"""

KEPT_SUFFIX = ".json"
INFLIGHT_SUFFIX = ".inflight"
FORMAT = 1


@dataclass(frozen=True)
class TaskKey:
    """What a kept result was computed from; the next task's must be the same for it to apply"""

    job_id: UUID
    kind: str
    payload_hash: str
    pages: tuple[str, ...]
    """The pages' `page_sha256`, in order"""

    @classmethod
    def of(cls, job_id: UUID, kind: str, payload: Any, pages: Sequence[str]) -> TaskKey:
        return cls(job_id=job_id, kind=str(kind), payload_hash=payload_hash(payload), pages=tuple(pages))

    def for_result(self, result: KeptResult) -> TaskKey:
        """The key a result is kept under: an extraction describes its own pages (orientation may have turned them)"""
        if isinstance(result, ExtractResult):
            return TaskKey(self.job_id, self.kind, self.payload_hash, tuple(page.page_sha256 for page in result.pages))
        return self


def payload_hash(payload: Any) -> str:
    """A task payload's hash, independent of key order"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _kept_path(job_id: UUID, kind: str) -> Path:
    return storage.results_dir() / f"{job_id}.{kind}{KEPT_SUFFIX}"


def _write_private(path: Path, text: str) -> None:
    """Writes `text` to `path` atomically, readable by the owner only"""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _dump(result: KeptResult) -> dict[str, Any]:
    if isinstance(result, ExtractResult):
        return {
            "type": "extract",
            "draft": result.draft.model_dump(mode="json"),
            "flags": [flag.model_dump(mode="json") for flag in result.flags],
            "transcription": result.transcription,
            "extraction": result.extraction.model_dump(mode="json"),
            "pages": [page.model_dump(mode="json") for page in result.pages],
            "origin": result.origin.value,
        }
    if isinstance(result, ParseLinesResult):
        return {
            "type": "parse_lines",
            "ingredients": [ingredient.model_dump(mode="json") for ingredient in result.ingredients],
            "sent": result.sent,
            "units": result.units,
            "linked": {str(ref): names for ref, names in result.linked.items()} if result.linked is not None else None,
        }
    return {"type": "reread", "proposal": result.proposal.model_dump(mode="json")}


def _load(data: dict[str, Any]) -> KeptResult:
    if data["type"] == "extract":
        return ExtractResult(
            draft=CardDraft.model_validate(data["draft"]),
            flags=[CardFlag.model_validate(flag) for flag in data["flags"]],
            transcription=data["transcription"],
            extraction=ExtractionMeta.model_validate(data["extraction"]),
            pages=[PageMeta.model_validate(page) for page in data["pages"]],
            origin=CardProposalOrigin(data.get("origin") or CardProposalOrigin.reextract.value),
        )
    if data["type"] == "parse_lines":
        sent, linked = data["sent"], data.get("linked")
        if not isinstance(sent, dict) or not isinstance(linked, dict | None):
            raise TypeError("sent")
        return ParseLinesResult(
            ingredients=[CardDraftIngredient.model_validate(line) for line in data["ingredients"]],
            sent={str(ref): str(text) for ref, text in sent.items()},
            units=[str(unit) for unit in data.get("units") or []],
            linked={UUID(ref): [str(name) for name in names] for ref, names in linked.items()}
            if linked is not None
            else None,
        )
    if data["type"] == "reread":
        return RereadResult(proposal=CardProposal.model_validate(data["proposal"]))
    raise ValueError(f"Unknown kept result: {data['type']}")


@dataclass
class Kept:
    """What a task a restore cut off left for the card's next task"""

    result: KeptResult | None = None
    """Its result, to apply instead of reading the card"""
    answers: KeptAnswers = field(default_factory=KeptAnswers)
    """Else the provider answers it got, to replay (empty when there are none)"""


def keep(key: TaskKey, *, result: KeptResult | None = None, answers: KeptAnswers | None = None) -> bool:
    """
    Keeps what a task a restore cut off got for the card's next task: its result, or else its provider answers.
    Whether anything was kept (nothing is when there's neither; a failure is logged).
    """
    if result is None and not answers:
        return False
    kept = key.for_result(result) if result is not None else key
    content = {
        "format": FORMAT,
        "job_id": str(kept.job_id),
        "kind": kept.kind,
        "payload_hash": kept.payload_hash,
        "pages": list(kept.pages),
        "created_at": time.time(),
        "result": _dump(result) if result is not None else None,
        "answers": answers.entries if result is None and answers else {},
    }
    try:
        storage.ensure_results_dir()
        _write_private(_kept_path(kept.job_id, kept.kind), json.dumps(content))
    except (OSError, TypeError, ValueError) as e:
        logger.warning(
            f"Recipe card job {key.job_id}: couldn't keep the reading a restore cut off ({type(e).__name__})"
        )
        return False
    what = "reading" if result is not None else f"{len(answers or ())} provider answer(s)"
    logger.info(f"Recipe card job {key.job_id}: kept the {what} a backup restore cut off, for its next task")
    return True


def forget(job_id: UUID, kind: str) -> None:
    """Removes a job's kept result or answers of that kind (once the next task's outcome is stored)"""
    try:
        _kept_path(job_id, str(kind)).unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Recipe card job {job_id}: couldn't remove its kept reading ({type(e).__name__})")


def take(key: TaskKey) -> Kept | None:
    """
    What the job's last cut-off task of this kind left, when it was computed from what `key` names (the same kind,
    payload and page hashes) less than `KEPT_RESULT_TTL` ago. Anything else is removed. The caller removes a matching
    one once its own outcome is stored (`forget`).
    """
    path = _kept_path(key.job_id, key.kind)
    try:
        content = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except OSError, ValueError:
        forget(key.job_id, key.kind)
        return None

    kept: Kept | None = None
    try:
        same = (
            content.get("format") == FORMAT
            and content.get("payload_hash") == key.payload_hash
            and tuple(content.get("pages") or ()) == key.pages
        )
        fresh = 0 <= time.time() - float(content["created_at"]) < limits.KEPT_RESULT_TTL
        if same and fresh:
            result = content.get("result")
            answers = content.get("answers") or {}
            if not isinstance(answers, dict):
                raise TypeError("answers")
            kept = Kept(result=_load(result) if result else None, answers=KeptAnswers(answers))
    except KeyError, TypeError, ValueError, ValidationError:
        kept = None
    if kept is None or (kept.result is None and not kept.answers):
        logger.info(f"Recipe card job {key.job_id}: its kept reading is for other pages or too old; reading again")
        forget(key.job_id, key.kind)
        return None
    return kept


# ==========================================
# Tasks in flight


@dataclass(frozen=True)
class InFlight:
    """A task running on a job, as its mark says"""

    path: Path
    token: str
    began: float
    """When it began (a Unix time)"""
    host: str
    pid: int
    started: int | None

    def gone(self) -> bool:
        """Whether its process is gone (or, on another host, it's past any deadline it could have)"""
        gone = storage.process_gone(self.host, self.pid, self.started)
        if gone is None:
            return time.time() - self.began > limits.TASK_DEADLINE + limits.LEASE
        return gone


def begin(job_id: UUID, token: UUID) -> Path | None:
    """Marks a task in flight on the job until `end`; the path of its mark, or None if it couldn't be written"""
    host, pid, started = storage.process_identity()
    content = {"token": str(token), "began": time.time(), "host": host, "pid": pid, "started": started}
    path = storage.results_dir() / f"{job_id}.{token}{INFLIGHT_SUFFIX}"
    try:
        storage.ensure_results_dir()
        _write_private(path, json.dumps(content))
    except OSError as e:
        logger.warning(f"Recipe card job {job_id}: couldn't mark its task in flight ({type(e).__name__})")
        return None
    return path


def end(mark: Path | None) -> None:
    """Removes a task's in-flight mark"""
    if mark is None:
        return
    try:
        mark.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Couldn't remove a recipe card task's in-flight mark ({type(e).__name__})")


def _read_mark(path: Path) -> InFlight | None:
    try:
        content = json.loads(path.read_text())
        return InFlight(
            path=path,
            token=str(content["token"]),
            began=float(content["began"]),
            host=str(content["host"]),
            pid=int(content["pid"]),
            started=int(content["started"]) if content.get("started") is not None else None,
        )
    except FileNotFoundError:
        return None
    except OSError, KeyError, TypeError, ValueError:
        return None


def cut_off_in_flight(job_id: UUID, token: UUID) -> list[InFlight]:
    """
    The job's other tasks still running that a backup restore cut off (they began before the last restore ended):
    their results will be kept. Marks whose process is gone are removed.
    """
    folder = storage.results_dir()
    if not folder.is_dir():
        return []
    found: list[InFlight] = []
    for path in folder.glob(f"{job_id}.*{INFLIGHT_SUFFIX}"):
        mark = _read_mark(path)
        if mark is None or mark.token == str(token):
            continue
        if mark.gone():
            end(path)
            continue
        if storage.restored_since(mark.began):
            found.append(mark)
    return found


async def wait_for_kept(key: TaskKey, token: UUID, *, deadline: float, stopping: Callable[[], bool]) -> Kept | None:
    """
    What to use instead of reading the card from scratch: what a task on the same job that a restore cut off left
    (`take`), waiting for one that is still running (polling every `KEPT_RESULT_POLL`) until it ends, for at most
    `KEPT_RESULT_WAIT`, within the `time.monotonic()` `deadline` and until `stopping()` says so. None when there's
    nothing: read the card as usual.
    """
    deadline = min(deadline, time.monotonic() + limits.KEPT_RESULT_WAIT)
    try:
        while True:
            kept = await asyncio.to_thread(take, key)
            if kept is not None and kept.result is not None:
                return kept
            running = await asyncio.to_thread(cut_off_in_flight, key.job_id, token)
            if not running:
                # it may have kept what it got between the two looks
                return await asyncio.to_thread(take, key)
            if time.monotonic() >= deadline or stopping():
                return kept
            await asyncio.sleep(limits.KEPT_RESULT_POLL)
    except OSError as e:
        logger.warning(f"Recipe card job {key.job_id}: couldn't look for a kept reading ({type(e).__name__})")
        return None


def purge(now: float | None = None) -> int:
    """Removes kept results and in-flight marks older than `KEPT_RESULT_TTL`; how many"""
    folder = storage.results_dir()
    if not folder.is_dir():
        return 0
    oldest = (time.time() if now is None else now) - limits.KEPT_RESULT_TTL
    removed = 0
    for path in folder.iterdir():
        if path.name == storage.RESTORED_NAME or not (
            path.name.endswith(KEPT_SUFFIX) or path.name.endswith(INFLIGHT_SUFFIX) or path.name.endswith(".tmp")
        ):
            continue
        try:
            if path.stat().st_mtime < oldest:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
