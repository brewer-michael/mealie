"""
Orientation inside a task (docs/ai/PHASE2.md §3.7, §4.4): a page Tesseract turns, or one the image reader says is
sideways, has its files and its stored metadata changed as one step. A task stopped during orientation (shutdown, the
reviewer's cancel), a later page that fails, a lease lost meanwhile or a killed process never leaves a turned page
described as it was before it turned: the turn is staged, stored, then swapped in, and the next task settles a turn
cut short.
"""

import asyncio
import hashlib
import io
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from ingest_runner_testing import Jobs, run, settle, wait_for
from PIL import Image
from sqlalchemy.orm import Session

from mealie.repos.repository_recipe_ingest import CancelOutcome, IngestQueue, cancel_task, utcnow
from mealie.schema.recipe_ingest import (
    CardDraft,
    ExtractionMeta,
    IngestErrorCode,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
    PageRotationSource,
)
from mealie.services import ocr
from mealie.services.ai.ingest import images, limits, storage, tasks
from mealie.services.ai.ingest.pipeline import CardExtraction
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.types import TaskContext, TaskFailed

SIDEWAYS = (1200, 800)
"""A card photographed sideways: intake leaves it landscape, Tesseract reads it best turned 90°"""

PAGE_FILES = (images.PAGE_FILE, images.VIEW_FILE, images.THUMB_FILE)


class SlowOCR:
    """
    `ocr.extract_text`: page 0 reads best turned 90°, the others upright, or a page's entry in `readings` raises.
    Each reading waits for `release` (Tesseract takes a while), and `entered` says one has started.
    """

    def __init__(self, readings: dict[int, int | Exception] | None = None) -> None:
        self.readings: dict[int, int | Exception] = {0: 90, **(readings or {})}
        self.scores: dict[int, dict[int, float]] = {}
        """A page's `rotation_scores`, when the test gives them"""
        self.entered = threading.Event()
        self.release = threading.Event()
        self.pages: list[int] = []
        self.before_answer: Any = None

    def __call__(self, path: Path, *, min_ratio: float = 1.0, **kwargs: Any) -> ocr.OCRResult:
        index = int(path.parent.name)
        self.pages.append(index)
        self.entered.set()
        assert self.release.wait(10), "the test never let the reading finish"
        if self.before_answer is not None:
            self.before_answer()
        reading = self.readings.get(index, 0)
        if isinstance(reading, Exception):
            raise reading
        return ocr.OCRResult(
            text="Banana Mug Cake", confidence=80, rotation=reading, rotation_scores=self.scores.get(index, {})
        )


@pytest.fixture()
def reader(monkeypatch: pytest.MonkeyPatch) -> SlowOCR:
    fake = SlowOCR()
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "binary_available", lambda: True)  # orientation's own check (Tesseract isn't in CI)
    monkeypatch.setattr(ocr, "extract_text", fake)
    return fake


@pytest.fixture(autouse=True)
def no_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only orientation runs: reading the card waits until the task is cancelled"""

    async def extract_card(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(tasks, "extract_card", extract_card)


@pytest.fixture()
def card_job(jobs: Jobs) -> Iterator[Any]:
    """Creates jobs with real page files (`card_job(pages)`), removed afterwards"""
    created: list[UUID] = []
    group_id = jobs.repos.group_id

    def create(pages: int = 1) -> UUID:
        job_id = uuid4()
        storage.create_job_dir(group_id, job_id, pages)
        metas = []
        for index in range(pages):
            buffer = io.BytesIO()
            Image.new("RGB", SIDEWAYS, "white").save(buffer, "JPEG")
            page_dir = storage.page_dir(group_id, job_id, index)
            metas.append(images.normalize_page(buffer, page_dir, index, original_filename="card.jpg"))
        created.append(jobs.create(id=job_id, pages=[meta.model_dump(mode="json") for meta in metas]))
        return job_id

    yield create
    for job_id in created:
        storage.remove_job_dir(group_id, job_id)


def _files(jobs: Jobs, job_id: UUID, index: int = 0) -> dict[str, bytes]:
    page_dir = storage.page_dir(jobs.repos.group_id, job_id, index)
    return {name: (page_dir / name).read_bytes() for name in PAGE_FILES}


def _stored_matches_disk(jobs: Jobs, job_id: UUID, index: int = 0) -> PageMeta:
    """The page's stored metadata, after checking it describes the files on disk"""
    meta = PageMeta.model_validate(jobs.row(job_id)["pages"][index])
    files = _files(jobs, job_id, index)
    with Image.open(io.BytesIO(files[images.PAGE_FILE])) as page:
        assert (meta.width, meta.height) == page.size
    with Image.open(io.BytesIO(files[images.VIEW_FILE])) as view:
        assert (meta.view_width, meta.view_height) == view.size
    assert meta.page_sha256 == hashlib.sha256(files[images.PAGE_FILE]).hexdigest()
    return meta


def _context(jobs: Jobs, job_id: UUID, token: UUID) -> TaskContext:
    async def report_progress(key: str) -> None:
        return None

    return TaskContext(
        job_id=job_id,
        group_id=jobs.repos.group_id,
        household_id=jobs.repos.household_id,  # type: ignore[arg-type]
        kind=IngestTaskKind.extract,
        payload=None,
        token=token,
        locale="en-US",
        local_only=False,
        report_progress=report_progress,
    )


def _claim(db: Session, job_id: UUID) -> UUID:
    token = uuid4()
    assert IngestQueue(db).claim(job_id, token=token, owner="test", now=utcnow())
    return token


def test_a_shutdown_during_orientation_keeps_a_turned_page_and_its_metadata_together(
    dispatcher: IngestDispatcher, jobs: Jobs, reader: SlowOCR, card_job: Any
):
    job_id = card_job()

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(reader.entered.is_set)
        stopping = asyncio.create_task(dispatcher.stop())  # a deploy, while Tesseract reads the card
        await asyncio.sleep(0.1)
        reader.release.set()
        await stopping

    run(scenario())
    meta = _stored_matches_disk(jobs, job_id)
    assert (meta.rotation, meta.rotation_source, meta.oriented) == (90, PageRotationSource.ocr, True)
    assert (meta.width, meta.height) == (SIDEWAYS[1], SIDEWAYS[0])
    row = jobs.row(job_id)
    assert (row["status"], row["task_state"], row["lease_token"], row["attempts"]) == (
        IngestStatus.processing,
        IngestTaskState.queued,
        None,
        0,
    )


def test_a_cancel_during_orientation_keeps_a_turned_page_and_its_metadata_together(
    dispatcher: IngestDispatcher,
    db: Session,
    jobs: Jobs,
    reader: SlowOCR,
    card_job: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    job_id = card_job()

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(reader.entered.is_set)
        assert cancel_task(db, job_id) == CancelOutcome.requested
        await dispatcher.run_once()  # the heartbeat passes the cancel on
        await asyncio.sleep(0.1)
        reader.release.set()
        await settle(dispatcher)

    run(scenario())
    meta = _stored_matches_disk(jobs, job_id)
    assert (meta.rotation, meta.oriented) == (90, True)
    row = jobs.row(job_id)
    assert (row["status"], row["error_code"], row["task_state"]) == (
        IngestStatus.failed,
        IngestErrorCode.cancelled,
        None,
    )


def test_a_page_that_fails_after_another_turned_keeps_the_turned_ones_metadata(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any
):
    job_id = card_job(pages=2)
    token = _claim(db, job_id)
    reader.readings[1] = RuntimeError("Tesseract crashed")
    reader.release.set()
    back_before = _files(jobs, job_id, 1)

    with pytest.raises(RuntimeError, match="Tesseract crashed"):
        run(tasks.handle_extract(_context(jobs, job_id, token)))

    assert reader.pages == [0, 1]
    assert _stored_matches_disk(jobs, job_id, 0).rotation == 90
    back = _stored_matches_disk(jobs, job_id, 1)
    assert (back.rotation, back.oriented) == (0, False)
    assert _files(jobs, job_id, 1) == back_before


def test_a_lease_lost_during_orientation_puts_the_page_back_as_it_was(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any
):
    job_id = card_job()
    token = _claim(db, job_id)
    files_before = _files(jobs, job_id)
    pages_before = jobs.row(job_id)["pages"]

    # swept and claimed by another process while Tesseract was reading
    reader.before_answer = lambda: jobs.update(job_id, lease_token=uuid4())
    reader.release.set()

    with pytest.raises(TaskFailed) as failed:
        run(tasks.handle_extract(_context(jobs, job_id, token)))

    assert failed.value.code == IngestErrorCode.interrupted
    assert reader.pages == [0]
    assert jobs.row(job_id)["pages"] == pages_before
    assert _files(jobs, job_id) == files_before  # byte for byte
    _stored_matches_disk(jobs, job_id)


def test_a_task_whose_lease_is_already_gone_doesnt_read_or_turn_the_page(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any
):
    job_id = card_job()
    _claim(db, job_id)
    files_before = _files(jobs, job_id)
    reader.release.set()

    with pytest.raises(TaskFailed) as failed:
        run(tasks.handle_extract(_context(jobs, job_id, uuid4())))

    assert failed.value.code == IngestErrorCode.interrupted
    assert reader.pages == []
    assert _files(jobs, job_id) == files_before


# ==========================================
# A turn is staged: a killed process never splits a page's files from its stored metadata


class Killed(BaseException):
    """The process dying at that point: nothing after it runs, no `except Exception` sees it"""


class ReadingStarted(Exception):
    """The task got past its pages (orientation and recovery) to reading the card"""


def _staged(jobs: Jobs, job_id: UUID, index: int = 0) -> list[str]:
    page_dir = storage.page_dir(jobs.repos.group_id, job_id, index)
    return sorted(name for name in images.STAGED_FILES.values() if (page_dir / name).exists())


def _next_task(jobs: Jobs, job_id: UUID, token: UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs the job's next task up to reading the card: everything it does with the pages first"""

    async def extract_card(*args: Any, **kwargs: Any) -> Any:
        raise ReadingStarted()

    monkeypatch.setattr(tasks, "extract_card", extract_card)
    with pytest.raises(ReadingStarted):
        run(tasks.handle_extract(_context(jobs, job_id, token)))


def test_a_kill_after_the_turn_is_stored_is_finished_by_the_next_task(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any, monkeypatch: pytest.MonkeyPatch
):
    job_id = card_job()
    token = _claim(db, job_id)
    reader.release.set()
    apply_staged = images.apply_staged

    def killed_before_the_swap(page_dir: Path) -> None:
        raise Killed()

    monkeypatch.setattr(images, "apply_staged", killed_before_the_swap)
    with pytest.raises(Killed):
        run(tasks.handle_extract(_context(jobs, job_id, token)))

    # the metadata naming the turned page is stored; the turned files wait beside the old ones
    stored = PageMeta.model_validate(jobs.row(job_id)["pages"][0])
    assert (stored.rotation, stored.rotation_source, stored.oriented) == (90, PageRotationSource.ocr, True)
    assert stored.ocr is not None and stored.ocr.text == "Banana Mug Cake"  # read at the new rotation, kept
    assert _staged(jobs, job_id) == sorted(images.STAGED_FILES.values())
    page_dir = storage.page_dir(jobs.repos.group_id, job_id, 0)
    assert hashlib.sha256((page_dir / images.PAGE_FILE).read_bytes()).hexdigest() != stored.page_sha256

    monkeypatch.setattr(images, "apply_staged", apply_staged)
    _next_task(jobs, job_id, token, monkeypatch)

    assert _staged(jobs, job_id) == []
    meta = _stored_matches_disk(jobs, job_id)  # the swap was finished: the files are the ones the row names
    assert (meta.rotation, meta.width, meta.height) == (90, SIDEWAYS[1], SIDEWAYS[0])
    assert reader.pages == [0]  # the page wasn't read or turned again


def test_a_kill_before_the_turn_is_stored_is_undone_by_the_next_task(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any, monkeypatch: pytest.MonkeyPatch
):
    job_id = card_job()
    token = _claim(db, job_id)
    reader.release.set()
    files_before = _files(jobs, job_id)
    pages_before = jobs.row(job_id)["pages"]
    store_page = tasks._store_page

    def killed_before_the_write(*args: Any, **kwargs: Any) -> bool:
        raise Killed()

    monkeypatch.setattr(tasks, "_store_page", killed_before_the_write)
    with pytest.raises(Killed):
        run(tasks.handle_extract(_context(jobs, job_id, token)))
    assert _staged(jobs, job_id) == sorted(images.STAGED_FILES.values())
    assert jobs.row(job_id)["pages"] == pages_before

    # the next task finds the staged files, which nothing stored names: they go, and the page is as stored
    monkeypatch.setattr(tasks, "_store_page", store_page)
    reader.readings[0] = 0  # read upright this time, so the page isn't turned again
    _next_task(jobs, job_id, token, monkeypatch)

    assert _staged(jobs, job_id) == []
    assert _files(jobs, job_id) == files_before  # byte for byte
    meta = _stored_matches_disk(jobs, job_id)
    assert (meta.rotation, meta.rotation_source, meta.oriented) == (0, PageRotationSource.none, True)


def test_a_lease_lost_while_the_turn_is_staged_leaves_no_staged_files(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any, monkeypatch: pytest.MonkeyPatch
):
    job_id = card_job()
    token = _claim(db, job_id)
    reader.release.set()
    files_before = _files(jobs, job_id)
    pages_before = jobs.row(job_id)["pages"]
    stage_rotation = images.stage_rotation

    def swept_meanwhile(*args: Any, **kwargs: Any) -> PageMeta:
        staged = stage_rotation(*args, **kwargs)
        jobs.update(job_id, lease_token=uuid4())  # swept and claimed by another process
        return staged

    monkeypatch.setattr(images, "stage_rotation", swept_meanwhile)
    with pytest.raises(TaskFailed) as failed:
        run(tasks.handle_extract(_context(jobs, job_id, token)))

    assert failed.value.code == IngestErrorCode.interrupted
    assert _staged(jobs, job_id) == []
    assert jobs.row(job_id)["pages"] == pages_before
    assert _files(jobs, job_id) == files_before
    _stored_matches_disk(jobs, job_id)


def test_an_error_while_storing_the_turn_settles_the_staged_files_at_once(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any, monkeypatch: pytest.MonkeyPatch
):
    """A database error (not a kill) while the metadata is written: the staged files follow what the row says"""
    job_id = card_job()
    token = _claim(db, job_id)
    reader.release.set()
    files_before = _files(jobs, job_id)

    def database_gone(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(tasks, "_store_page", database_gone)
    with pytest.raises(RuntimeError, match="connection lost"):
        run(tasks.handle_extract(_context(jobs, job_id, token)))

    assert _staged(jobs, job_id) == []
    assert _files(jobs, job_id) == files_before
    _stored_matches_disk(jobs, job_id)


# ==========================================
# A turn the image reader reports


def _reader_says(monkeypatch: pytest.MonkeyPatch, rotations: dict[int, int]) -> None:
    """`extract_card` as the pipeline answers when the image reader said how far each page must turn"""

    async def extract_card(pages: list[Any], **kwargs: Any) -> CardExtraction:
        return CardExtraction(
            draft=CardDraft(name="Banana Mug Cake"),
            flags=[],
            transcription="Banana Mug Cake",
            extraction=ExtractionMeta(),
            rotations=rotations,
        )

    monkeypatch.setattr(tasks, "extract_card", extract_card)


def test_a_page_the_image_reader_says_is_sideways_is_turned_without_tesseract(
    db: Session, jobs: Jobs, card_job: Any, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    monkeypatch.setattr(ocr, "binary_available", lambda: False)
    _reader_says(monkeypatch, {0: 90})
    job_id = card_job(pages=2)
    token = _claim(db, job_id)
    back_before = _files(jobs, job_id, 1)

    result = run(tasks.handle_extract(_context(jobs, job_id, token)))

    front = _stored_matches_disk(jobs, job_id, 0)
    assert (front.rotation, front.rotation_source, front.oriented) == (90, PageRotationSource.model, True)
    assert (front.width, front.height) == (SIDEWAYS[1], SIDEWAYS[0])
    assert result.pages[0] == front  # the result carries the turned page, as finalize stores it
    back = _stored_matches_disk(jobs, job_id, 1)
    assert (back.rotation, back.oriented) == (0, False)  # read as upright: left for Rotate
    assert _files(jobs, job_id, 1) == back_before
    assert _staged(jobs, job_id) == []


def test_a_page_tesseract_or_the_reviewer_oriented_ignores_the_image_reader(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any, monkeypatch: pytest.MonkeyPatch
):
    _reader_says(monkeypatch, {0: 90, 1: 180})
    reader.readings[0] = 0  # Tesseract reads the front upright: settled as it is
    reader.release.set()
    job_id = card_job(pages=2)
    pages = jobs.row(job_id)["pages"]
    pages[1] = {**pages[1], "oriented": True, "rotation_source": PageRotationSource.user.value}  # turned by hand
    jobs.update(job_id, pages=pages)
    token = _claim(db, job_id)
    files_before = [_files(jobs, job_id, 0), _files(jobs, job_id, 1)]

    run(tasks.handle_extract(_context(jobs, job_id, token)))

    front = _stored_matches_disk(jobs, job_id, 0)
    assert (front.rotation, front.rotation_source, front.oriented) == (0, PageRotationSource.none, True)
    back = _stored_matches_disk(jobs, job_id, 1)
    assert (back.rotation, back.rotation_source) == (0, PageRotationSource.user)
    assert [_files(jobs, job_id, 0), _files(jobs, job_id, 1)] == files_before


@pytest.mark.parametrize(
    ("scores", "turn"),
    [
        # a handwritten card: Tesseract kept it upright but read nothing there (its best score was turned a quarter,
        # too low to turn it)
        ({0: 0, 90: 135, 180: 83, 270: 0}, 90),
        # the sideways banana card blurred: upright won on noise, far under what a turn needs
        ({0: 116, 90: 68, 180: 0, 270: 112}, 270),
    ],
)
def test_a_turn_tesseract_couldnt_decide_is_left_to_the_image_reader(
    db: Session,
    jobs: Jobs,
    reader: SlowOCR,
    card_job: Any,
    monkeypatch: pytest.MonkeyPatch,
    scores: dict[int, float],
    turn: int,
):
    # its text is kept and the page isn't settled; the image reader's turn applies
    _reader_says(monkeypatch, {0: turn})
    reader.readings[0] = 0
    reader.scores[0] = scores
    reader.release.set()
    job_id = card_job(pages=1)
    token = _claim(db, job_id)

    run(tasks.handle_extract(_context(jobs, job_id, token)))

    front = _stored_matches_disk(jobs, job_id, 0)
    assert (front.rotation, front.rotation_source, front.oriented) == (turn, PageRotationSource.model, True)
    assert (front.width, front.height) == (SIDEWAYS[1], SIDEWAYS[0])


def test_a_manual_rotate_in_progress_and_the_tasks_turn_never_mix_their_staged_files(
    db: Session, jobs: Jobs, reader: SlowOCR, card_job: Any, monkeypatch: pytest.MonkeyPatch
):
    """
    A rotate that checked for a task just before this one was claimed stages its turn, is refused (the job has a task
    now) and discards its staged files. The task's turn waits for the page's turn lock meanwhile, so the rotate never
    discards the task's staged files, and the page ends turned as the task stored it.
    """
    from mealie.services.ai.ingest.review import page_turn_lock

    job_id = card_job()
    token = _claim(db, job_id)
    page_dir = storage.page_dir(jobs.repos.group_id, job_id, 0)
    stored = PageMeta.model_validate(jobs.row(job_id)["pages"][0])
    holding, done = threading.Event(), threading.Event()
    order: list[str] = []
    stage_rotation = images.stage_rotation

    def recorded(page_dir: Path, meta: PageMeta, degrees: int, source: PageRotationSource) -> PageMeta:
        order.append(f"stage {source.value}")
        return stage_rotation(page_dir, meta, degrees, source)

    monkeypatch.setattr(images, "stage_rotation", recorded)

    def rotate_refused() -> None:
        with storage.ingest_write(), page_turn_lock(page_dir):
            images.stage_rotation(page_dir, stored, 180, PageRotationSource.user)
            holding.set()
            time.sleep(0.3)  # the task's turn reaches the lock meanwhile
            images.discard_staged(page_dir)  # its write was refused
            order.append("rotate discarded")
        done.set()

    rotating = threading.Thread(target=rotate_refused, daemon=True)
    rotating.start()
    assert holding.wait(5)
    reader.release.set()
    _next_task(jobs, job_id, token, monkeypatch)
    rotating.join(5)

    assert done.is_set()
    assert order == ["stage user", "rotate discarded", "stage ocr"]  # the task's turn waited for the rotate's
    assert _staged(jobs, job_id) == []
    meta = _stored_matches_disk(jobs, job_id)
    assert (meta.rotation, meta.rotation_source) == (90, PageRotationSource.ocr)
