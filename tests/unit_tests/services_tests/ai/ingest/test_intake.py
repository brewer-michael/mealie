"""
Intake (docs/ai/PHASE2.md §2): a card's images become a queued job with its pages on disk, inside the ingest write
lock; duplicates, batches and positions in one transaction; nothing left in `DATA_DIR` when anything fails; and the
pre-upload readiness check. Runs on SQLite and PostgreSQL; the race test uses two sessions in two threads.
"""

import fcntl
import io
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.orm import Session

from mealie.core.exceptions import NoEntryFound
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestJobsRepo, IngestRepos, utcnow
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate
from mealie.schema.recipe_ingest import (
    IngestRejectReason,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
    RecipeIngestionSettingsUpdate,
)
from mealie.services import ocr
from mealie.services.ai.errors import AIProviderLimitReachedError, IngestPaused
from mealie.services.ai.ingest import batches, images, intake, limits, storage
from mealie.services.ai.ingest.intake import (
    ClaimLost,
    IntakeAccepted,
    IntakeCard,
    IntakeOptions,
    IntakePage,
    IntakeRejected,
    IntakeService,
    reading_readiness,
    source_sha256,
)
from mealie.services.ai.ingest.runner.dispatcher import dispatcher
from mealie.services.ai.runtime import AIRuntime
from tests.utils.fixture_schemas import TestUser

ORIENTATION = 0x0112
GPS_IFD = 0x8825
RED = (255, 0, 0)


@pytest.fixture()
def db() -> Iterator[Session]:
    with session_context() as session:
        yield session


@pytest.fixture()
def woken(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(dispatcher, "wake", lambda: calls.append(1))
    return calls


def _jpeg(size: tuple[int, int] = (90, 60), *, gps: bool = True) -> bytes:
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    exif = Image.Exif()
    if gps:
        exif[GPS_IFD] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def _card(*datas: bytes, names: list[str] | None = None) -> IntakeCard:
    names = names or [f"page-{n}.jpg" for n in range(len(datas))]
    pages = [
        IntakePage(io.BytesIO(data), name, index) for index, (data, name) in enumerate(zip(datas, names, strict=True))
    ]
    return IntakeCard(pages=pages, source_name=f"upload/{names[0]}")


def _options(user: TestUser, **values: Any) -> IntakeOptions:
    return IntakeOptions(**{"source": IngestSource.api, "created_by": user.user_id, "locale": "en-US", **values})


def _service(session: Session, user: TestUser) -> IntakeService:
    return IntakeService(session, UUID(user.group_id), UUID(user.household_id))


def _job(job_id: UUID) -> RecipeIngestionJob:
    with session_context() as session:
        job = session.get(RecipeIngestionJob, job_id)
        assert job is not None
        session.expunge(job)
        return job


def _dirs(user: TestUser) -> set[str]:
    root = storage.ingest_root(UUID(user.group_id))
    return {path.name for path in root.iterdir()} if root.exists() else set()


def _counts(user: TestUser) -> tuple[int, int]:
    """The household's jobs and batches"""
    with session_context() as session:
        household = UUID(user.household_id)
        jobs = session.execute(
            sa.select(sa.func.count())
            .select_from(RecipeIngestionJob)
            .where(RecipeIngestionJob.household_id == household)
        ).scalar_one()
        found = session.execute(
            sa.select(sa.func.count())
            .select_from(RecipeIngestionBatch)
            .where(RecipeIngestionBatch.household_id == household)
        ).scalar_one()
        return jobs, found


def _accepted(outcome: Any) -> IntakeAccepted:
    assert isinstance(outcome, IntakeAccepted), outcome
    return outcome


# ==========================================
# A card becomes a job


def test_a_card_becomes_a_queued_job_with_its_pages_on_disk(db: Session, unique_user: TestUser, woken: list[int]):
    front, back = _jpeg(), _jpeg((60, 90))
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spooled:
        # a multipart part (no path) for the front, decoded base64 for the back
        spooled.write(front)
        spooled.seek(0)
        card = IntakeCard(
            pages=[IntakePage(spooled, "IMG_0007.jpg", 0), IntakePage(io.BytesIO(back), "IMG_0008.jpg", 1)],  # type: ignore[arg-type]
            source_name="upload/IMG_0007.jpg",
        )
        outcome = _accepted(
            _service(db, unique_user).ingest(card, _options(unique_user, integration_id="shortcut", position=3))
        )

    assert outcome.page_count == 2
    assert woken == [1]
    job = _job(outcome.job_id)
    assert job.batch_id == outcome.batch_id
    assert job.status == IngestStatus.processing.value
    assert (job.task_kind, job.task_state, job.task_priority) == (
        IngestTaskKind.extract.value,
        IngestTaskState.queued.value,
        limits.PRIORITY_EXTRACT,
    )
    assert (job.position, job.source, job.source_name) == (3, "api", "upload/IMG_0007.jpg")
    assert (job.created_by, job.integration_id, job.locale, job.local_only) == (
        unique_user.user_id,
        "shortcut",
        "en-US",
        False,
    )
    pages = [PageMeta.model_validate(page) for page in job.pages]
    assert [(page.index, page.width, page.height) for page in pages] == [(0, 90, 60), (1, 60, 90)]
    assert job.source_sha256 == source_sha256(pages)

    # every file under the job is an EXIF-free page, and no uploaded byte was kept
    job_dir = storage.job_dir(UUID(unique_user.group_id), outcome.job_id)
    stored = sorted(path.relative_to(job_dir).as_posix() for path in job_dir.rglob("*") if path.is_file())
    assert stored == [f"pages/{n}/{name}" for n in (0, 1) for name in ("page.jpg", "thumb.webp", "view.jpg")]
    for path in job_dir.rglob("*.*"):
        data = path.read_bytes()
        assert b"GPS" not in data and b"Exif" not in data, path
        assert data not in (front, back)


def test_a_heic_card_with_orientation_6_comes_out_upright(db: Session, unique_user: TestUser, woken: list[int]):
    pytest.importorskip("pillow_heif")
    image = Image.new("RGB", (600, 300), "white")
    image.paste(RED, (0, 0, 200, 300))
    exif = Image.Exif()
    exif[ORIENTATION] = 6
    exif[GPS_IFD] = {1: "N", 2: (51.0, 30.0, 0.0)}
    buffer = io.BytesIO()
    image.save(buffer, format="HEIF", exif=exif.tobytes(), quality=90)

    outcome = _accepted(_service(db, unique_user).ingest(_card(buffer.getvalue()), _options(unique_user)))
    page_jpg = storage.page_dir(UUID(unique_user.group_id), outcome.job_id, 0) / images.PAGE_FILE
    with Image.open(page_jpg) as page:
        assert page.size == (300, 600)
        assert not page.getexif()
        red = page.getpixel((150, 40))
        assert red[0] > 200 and red[1] < 60  # the red left third is now the top


def test_a_rejected_page_leaves_nothing_behind(db: Session, unique_user: TestUser, woken: list[int]):
    before_dirs, before_counts = _dirs(unique_user), _counts(unique_user)
    outcome = _service(db, unique_user).ingest(
        _card(_jpeg(), b"%PDF-1.7 a menu", names=["front.jpg", "menu.pdf"]), _options(unique_user)
    )
    assert outcome == IntakeRejected(1, "menu.pdf", IngestRejectReason.pdf_not_supported)
    assert _dirs(unique_user) == before_dirs
    assert _counts(unique_user) == before_counts  # no job, and no empty batch either
    assert woken == []


def test_a_card_has_at_most_four_pages(db: Session, unique_user: TestUser):
    pages = [_jpeg((8, 8)) for _ in range(limits.MAX_PAGES_PER_CARD + 1)]
    outcome = _service(db, unique_user).ingest(_card(*pages), _options(unique_user))
    assert isinstance(outcome, IntakeRejected)
    assert (outcome.index, outcome.reason) == (4, IngestRejectReason.too_many_pages)


# ==========================================
# Duplicates


def test_duplicates_are_found_by_the_ordered_page_hashes(db: Session, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    service = _service(db, user)
    front, back = _jpeg(), _jpeg()

    first = _accepted(service.ingest(_card(front), _options(user)))
    before = _counts(user)
    again = service.ingest(_card(front), _options(user))
    assert again == IntakeRejected(0, "page-0.jpg", IngestRejectReason.duplicate, duplicate_of=first.job_id)
    assert _counts(user) == before

    # the front sent again with its back isn't one, nor the pages the other way round
    _accepted(service.ingest(_card(front, back), _options(user)))
    _accepted(service.ingest(_card(back, front), _options(user)))
    assert isinstance(service.ingest(_card(front, back), _options(user)), IntakeRejected)

    # committed cards count; allowDuplicate skips the check
    db.execute(
        sa.update(RecipeIngestionJob)
        .where(RecipeIngestionJob.id == first.job_id)
        .values(status=IngestStatus.committed.value)
    )
    db.commit()
    assert isinstance(service.ingest(_card(front), _options(user)), IntakeRejected)
    _accepted(service.ingest(_card(front), _options(user, allow_duplicate=True)))


def test_another_households_card_isnt_a_duplicate(db: Session, unique_user: TestUser, h2_user: TestUser):
    data = _jpeg()
    _accepted(_service(db, unique_user).ingest(_card(data), _options(unique_user)))
    _accepted(_service(db, h2_user).ingest(_card(data), _options(h2_user)))


# ==========================================
# Batches and positions


def test_cards_join_their_batch_in_order(db: Session, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    service = _service(db, user)
    first = _accepted(service.ingest(_card(_jpeg()), _options(user)))
    second = _accepted(service.ingest(_card(_jpeg()), _options(user)))
    assert first.batch_id == second.batch_id
    assert (_job(first.job_id).position, _job(second.job_id).position) == (0, 1)

    forced = _accepted(service.ingest(_card(_jpeg()), _options(user, batch_id="new")))
    assert forced.batch_id != first.batch_id
    assert _job(forced.job_id).position == 0


def test_a_sealed_app_batch_sends_the_card_to_a_new_one(db: Session, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    repos = IngestRepos(db, UUID(user.group_id), UUID(user.household_id))
    app_batch = repos.batches.create(source=IngestSource.app, created_by=user.user_id)
    service = _service(db, user)

    first = _accepted(service.ingest(_card(_jpeg()), _options(user, batch_id=app_batch, position=0)))
    assert first.batch_id == app_batch
    assert _job(first.job_id).source == "app"

    assert batches.seal(repos, app_batch, utcnow())
    late = _accepted(service.ingest(_card(_jpeg()), _options(user, batch_id=app_batch, position=1)))
    assert late.batch_id != app_batch
    late_job = _job(late.job_id)
    assert (late_job.source, late_job.position) == ("app", 1)
    assert len(repos.batches.jobs(app_batch)) == 1


def test_an_unknown_batch_writes_nothing(db: Session, unique_user: TestUser, h2_user: TestUser):
    theirs = IngestRepos(db, UUID(h2_user.group_id), UUID(h2_user.household_id)).batches.create(
        source=IngestSource.app, created_by=None
    )
    before = _dirs(unique_user)
    with pytest.raises(NoEntryFound):
        _service(db, unique_user).ingest(_card(_jpeg()), _options(unique_user, batch_id=theirs))
    assert _dirs(unique_user) == before


def test_a_seal_racing_an_insert_never_leaves_the_job_in_a_sealed_batch(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    with session_context() as setup:
        app_batch = IngestRepos(setup, UUID(user.group_id), UUID(user.household_id)).batches.create(
            source=IngestSource.app, created_by=user.user_id
        )

    seal_result: dict[str, Any] = {}

    def seal_from_another_session() -> None:
        with session_context() as session:
            repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
            seal_result["sealed"] = batches.seal(repos, app_batch, utcnow())
            seal_result["jobs"] = len(repos.batches.jobs(app_batch))
            session.commit()

    real_touch = batches.touch
    sealer = threading.Thread(target=seal_from_another_session, daemon=True)

    def touch_then_let_the_seal_start(repos: IngestRepos, batch_id: UUID, now: Any) -> bool:
        touched = real_touch(repos, batch_id, now)
        if not sealer.is_alive() and "sealed" not in seal_result:
            sealer.start()
            time.sleep(0.5)  # the seal is now waiting for this transaction
            assert sealer.is_alive()
        return touched

    monkeypatch.setattr(batches, "touch", touch_then_let_the_seal_start)
    with session_context() as session:
        first = _accepted(_service(session, user).ingest(_card(_jpeg()), _options(user, batch_id=app_batch)))
    sealer.join(10)

    # the insert won: the job is in the batch, which the seal then closed with the job in it
    assert first.batch_id == app_batch
    assert seal_result == {"sealed": True, "jobs": 1}

    # and the next card, after the seal, goes elsewhere
    monkeypatch.setattr(batches, "touch", real_touch)
    with session_context() as session:
        second = _accepted(_service(session, user).ingest(_card(_jpeg()), _options(user, batch_id=app_batch)))
        assert second.batch_id != app_batch
        assert len(IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).batches.jobs(app_batch)) == 1


def test_a_batch_sealed_between_choosing_and_touching_is_replaced(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    repos = IngestRepos(db, UUID(user.group_id), UUID(user.household_id))
    app_batch = repos.batches.create(source=IngestSource.app, created_by=user.user_id)
    real_select = batches.select_batch

    def select_then_seal(*args: Any, **kwargs: Any) -> UUID:
        chosen = real_select(*args, **kwargs)
        if chosen == app_batch:
            with session_context() as other:
                assert batches.seal(IngestRepos(other, UUID(user.group_id), UUID(user.household_id)), chosen, utcnow())
        return chosen

    monkeypatch.setattr(batches, "select_batch", select_then_seal)
    outcome = _accepted(_service(db, user).ingest(_card(_jpeg()), _options(user, batch_id=app_batch)))
    assert outcome.batch_id != app_batch
    assert repos.batches.jobs(app_batch) == []


# ==========================================
# Pauses and failures leave nothing in DATA_DIR


def test_a_pause_stops_intake_before_anything_is_written(db: Session, unique_user: TestUser):
    before = _dirs(unique_user), _counts(unique_user)
    storage.pause_marker_path().write_text(str(time.time()))
    try:
        with pytest.raises(IngestPaused):
            _service(db, unique_user).ingest(_card(_jpeg()), _options(unique_user))
    finally:
        storage.pause_marker_path().unlink(missing_ok=True)
    assert (_dirs(unique_user), _counts(unique_user)) == before


def test_a_write_lock_held_exclusively_by_another_thread_stops_intake(db: Session, unique_user: TestUser):
    held, release = threading.Event(), threading.Event()

    def restore() -> None:
        fd = os.open(storage.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.set()
            release.wait(10)
        finally:
            os.close(fd)

    thread = threading.Thread(target=restore, daemon=True)
    thread.start()
    assert held.wait(5)
    before = _dirs(unique_user)
    try:
        with pytest.raises(IngestPaused):
            _service(db, unique_user).ingest(_card(_jpeg()), _options(unique_user))
    finally:
        release.set()
        thread.join(5)
    assert _dirs(unique_user) == before


def test_a_lost_inbox_claim_writes_nothing(db: Session, unique_user: TestUser):
    before = _dirs(unique_user), _counts(unique_user)
    with pytest.raises(ClaimLost):
        _service(db, unique_user).ingest(_card(_jpeg()), _options(unique_user), confirm=lambda: False)
    assert (_dirs(unique_user), _counts(unique_user)) == before


def test_a_database_error_removes_the_jobs_directory(
    db: Session, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    def fail(*args: Any, **kwargs: Any) -> UUID:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(IngestJobsRepo, "create", fail)
    before = _dirs(unique_user), _counts(unique_user)
    with pytest.raises(RuntimeError):
        _service(db, unique_user).ingest(_card(_jpeg()), _options(unique_user))
    assert (_dirs(unique_user), _counts(unique_user)) == before


# ==========================================
# From async code


def test_async_intake_runs_in_worker_threads_two_at_a_time(
    db: Session, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    running = 0
    most = 0
    threads: set[int] = set()
    lock = threading.Lock()

    def ingest(self: IntakeService, card: IntakeCard, options: IntakeOptions, *, confirm: Any = None) -> Any:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
            threads.add(threading.get_ident())
        time.sleep(0.1)
        with lock:
            running -= 1
        return IntakeRejected(0, None, IngestRejectReason.duplicate)

    monkeypatch.setattr(IntakeService, "ingest", ingest)
    loop_thread: list[int] = []

    async def main() -> None:
        loop_thread.append(threading.get_ident())
        service = _service(db, unique_user)
        async with anyio.create_task_group() as group:
            for _ in range(5):
                group.start_soon(service.ingest_async, _card(b""), _options(unique_user))

    anyio.run(main)
    assert most == limits.INTAKE_CONCURRENCY
    assert loop_thread[0] not in threads


# ==========================================
# Can the group read cards?


def _providers(user: TestUser, *, image: bool) -> None:
    repos = user.repos
    default = repos.group_ai_providers.create(AIProviderCreate(name="Text", model="m", api_key="k"))
    vision = repos.group_ai_providers.create(AIProviderCreate(name="Vision", model="m", api_key="k")) if image else None
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(
            default_provider_id=default.id, image_provider_id=vision.id if vision else None, audio_provider_id=None
        ),
    )


def test_readiness_follows_upstreams_rule(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    monkeypatch.setattr(ocr, "is_available", lambda: False)

    nothing = reading_readiness(db, group_id, household_id)
    assert (nothing.can_read, nothing.local_ready, nothing.group_local_only, nothing.processing) == (
        False,
        False,
        False,
        0,
    )
    assert not db.in_transaction()

    _providers(user, image=False)
    assert not reading_readiness(db, group_id, household_id).can_read  # a default provider alone needs OCR
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    with_ocr = reading_readiness(db, group_id, household_id)
    assert with_ocr.can_read and not with_ocr.local_ready  # a provider without a base URL is never local

    IngestRepos(db, group_id, household_id).settings.upsert(RecipeIngestionSettingsUpdate(local_only=True))
    assert reading_readiness(db, group_id, household_id).group_local_only


def test_a_monthly_limit_doesnt_count_as_unable_to_read(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    _providers(user, image=True)
    monkeypatch.setattr(ocr, "is_available", lambda: False)

    def over_the_limit(self: AIRuntime, slot: Any) -> list:
        raise AIProviderLimitReachedError("every provider is over its monthly limit")

    monkeypatch.setattr(AIRuntime, "candidates", over_the_limit)
    assert reading_readiness(db, UUID(user.group_id), UUID(user.household_id)).can_read


def test_source_names_and_the_duplicate_key(tmp_path: Path):
    assert intake.source_name("upload", "IMG_0007.HEIC") == "upload/IMG_0007.HEIC"
    assert intake.source_name("upload", None) is None
    assert len(intake.source_name("inbox/g/h", "x" * 400) or "") == intake.SOURCE_NAME_MAX

    def meta(raw: str) -> PageMeta:
        return PageMeta(
            index=0,
            width=1,
            height=1,
            view_width=1,
            view_height=1,
            raw_sha256=raw,
            page_sha256="p",
            format="jpeg",
            raw_bytes=1,
        )

    a, b = meta("a" * 64), meta("b" * 64)
    assert source_sha256([a, b]) != source_sha256([b, a]) != source_sha256([a])
    assert len(source_sha256([a])) == 64
