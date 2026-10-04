"""
Orientation inside a task (docs/ai/PHASE2.md §3.7, §4.4): a page Tesseract turns has its files and its stored
metadata changed as one step. A task stopped during orientation (shutdown, the reviewer's cancel), a later page that
fails, or a lease lost meanwhile never leaves a turned page described as it was before it turned.
"""

import asyncio
import hashlib
import io
import threading
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
    IngestErrorCode,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
    PageRotationSource,
)
from mealie.services import ocr
from mealie.services.ai.ingest import images, limits, storage, tasks
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
        self.entered = threading.Event()
        self.release = threading.Event()
        self.pages: list[int] = []
        self.before_answer: Any = None

    def __call__(self, path: Path, *, min_ratio: float = 1.0) -> ocr.OCRResult:
        index = int(path.parent.name)
        self.pages.append(index)
        self.entered.set()
        assert self.release.wait(10), "the test never let the reading finish"
        if self.before_answer is not None:
            self.before_answer()
        reading = self.readings.get(index, 0)
        if isinstance(reading, Exception):
            raise reading
        return ocr.OCRResult(text="Banana Mug Cake", confidence=80, rotation=reading)


@pytest.fixture()
def reader(monkeypatch: pytest.MonkeyPatch) -> SlowOCR:
    fake = SlowOCR()
    monkeypatch.setattr(ocr, "is_available", lambda: True)
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
