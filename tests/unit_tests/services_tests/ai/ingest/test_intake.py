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
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from mealie.core.exceptions import NoEntryFound
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestBatchesRepo, IngestJobsRepo, IngestRepos, utcnow
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.schema.recipe.recipe_category import TagSave
from mealie.schema.recipe_ingest import (
    IngestLimitedFeature,
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
from mealie.services.ai.routing import AIProviderRouter
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


def _tiff(*sizes: tuple[int, int]) -> bytes:
    frames = [Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)) for size in sizes]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


def test_a_multi_page_files_pages_fill_the_card(db: Session, unique_user_fn_scoped: TestUser, woken: list[int]):
    user = unique_user_fn_scoped
    scan, note = _tiff((40, 30), (30, 40)), _jpeg((20, 10))
    outcome = _accepted(_service(db, user).ingest(_card(scan, note, names=["scan.tiff", "note.jpg"]), _options(user)))

    assert outcome.page_count == 3
    pages = [PageMeta.model_validate(page) for page in _job(outcome.job_id).pages]
    assert [(page.index, page.width, page.height, page.original_filename) for page in pages] == [
        (0, 40, 30, "scan.tiff (page 1)"),
        (1, 30, 40, "scan.tiff (page 2)"),
        (2, 20, 10, "note.jpg"),
    ]
    for page in pages:
        assert (storage.page_dir(UUID(user.group_id), outcome.job_id, page.index) / images.PAGE_FILE).is_file()

    # the same files again are the same card; the scan alone is another
    again = _service(db, user).ingest(_card(scan, note, names=["scan.tiff", "note.jpg"]), _options(user))
    assert again == IntakeRejected(0, "scan.tiff", IngestRejectReason.duplicate, duplicate_of=outcome.job_id)
    alone = _accepted(_service(db, user).ingest(_card(scan, names=["scan.tiff"]), _options(user)))
    assert _service(db, user).ingest(_card(scan, names=["copy.tiff"]), _options(user)) == IntakeRejected(
        0, "copy.tiff", IngestRejectReason.duplicate, duplicate_of=alone.job_id
    )


def test_a_card_whose_files_hold_more_than_four_pages_is_refused(db: Session, unique_user: TestUser):
    before_dirs, before_counts = _dirs(unique_user), _counts(unique_user)
    outcome = _service(db, unique_user).ingest(
        _card(_jpeg((8, 8)), _tiff((8, 8), (8, 8), (8, 8)), _jpeg((8, 8)), names=["a.jpg", "b.tiff", "c.jpg"]),
        _options(unique_user),
    )
    assert outcome == IntakeRejected(2, "c.jpg", IngestRejectReason.too_many_pages)
    assert _dirs(unique_user) == before_dirs
    assert _counts(unique_user) == before_counts


def test_a_pdfs_rendered_pages_are_closed_whatever_happens(
    db: Session, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    opened: list[images.DocumentPage] = []
    real_expand = images.expand_document

    def expand_document(raw: Any) -> list[images.DocumentPage]:
        pages = real_expand(raw)
        rendered = [replace(page, rendered=True, file=io.BytesIO(page.file.read())) for page in pages]
        opened.extend(rendered)
        return rendered

    monkeypatch.setattr(images, "expand_document", expand_document)
    service = _service(db, unique_user)
    _accepted(service.ingest(_card(_jpeg()), _options(unique_user)))
    service.ingest(_card(_jpeg(), b"not an image", names=["a.jpg", "b.bin"]), _options(unique_user))
    assert opened and all(page.file.closed for page in opened)


def test_a_group_switched_to_local_only_during_the_upload_gets_a_local_only_job(
    db: Session, unique_user_fn_scoped: TestUser
):
    # the upload read the group's setting before its body arrived; the insert reads it again
    user = unique_user_fn_scoped
    service = _service(db, user)
    assert not _job(_accepted(service.ingest(_card(_jpeg()), _options(user))).job_id).local_only

    IngestRepos(db, UUID(user.group_id), UUID(user.household_id)).settings.upsert(
        RecipeIngestionSettingsUpdate(local_only=True)
    )
    assert _job(_accepted(service.ingest(_card(_jpeg()), _options(user, local_only=False))).job_id).local_only

    IngestRepos(db, UUID(user.group_id), UUID(user.household_id)).settings.upsert(
        RecipeIngestionSettingsUpdate(local_only=False)
    )
    assert _job(_accepted(service.ingest(_card(_jpeg()), _options(user, local_only=True))).job_id).local_only
    assert not _job(_accepted(service.ingest(_card(_jpeg()), _options(user))).job_id).local_only


# ==========================================
# Duplicates


def test_duplicates_are_found_by_the_ordered_page_hashes(
    db: Session, unique_user_fn_scoped: TestUser, api_client: TestClient
):
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

    # committed cards count (while their recipe exists); allowDuplicate skips the check
    slug = api_client.post("/api/recipes", json={"name": f"card {first.job_id}"}, headers=user.token).json()
    recipe_id = api_client.get(f"/api/recipes/{slug}", headers=user.token).json()["id"]
    db.execute(
        sa.update(RecipeIngestionJob)
        .where(RecipeIngestionJob.id == first.job_id)
        .values(status=IngestStatus.committed.value, recipe_id=UUID(recipe_id))
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


@pytest.mark.parametrize("sealer", ["another_session", "same_transaction"])
def test_a_batch_sealed_between_choosing_and_touching_is_replaced(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, sealer: str
):
    if sealer == "another_session" and db.get_bind().dialect.name != "postgresql":
        pytest.skip("On SQLite intake holds the database's write lock before it chooses: no seal can come between")
    user = unique_user_fn_scoped
    repos = IngestRepos(db, UUID(user.group_id), UUID(user.household_id))
    app_batch = repos.batches.create(source=IngestSource.app, created_by=user.user_id)
    real_select = batches.select_batch

    def select_then_seal(*args: Any, **kwargs: Any) -> UUID:
        chosen = real_select(*args, **kwargs)
        if chosen == app_batch and sealer == "another_session":
            # the seal doesn't take the household's intake lock (PostgreSQL's is an advisory lock)
            with session_context() as other:
                assert batches.seal(IngestRepos(other, UUID(user.group_id), UUID(user.household_id)), chosen, utcnow())
        elif chosen == app_batch:
            db.execute(
                sa.update(RecipeIngestionBatch).where(RecipeIngestionBatch.id == chosen).values(sealed_at=utcnow())
            )
        return chosen

    monkeypatch.setattr(batches, "select_batch", select_then_seal)
    outcome = _accepted(_service(db, user).ingest(_card(_jpeg()), _options(user, batch_id=app_batch)))
    assert outcome.batch_id != app_batch
    assert repos.batches.jobs(app_batch) == []


# ==========================================
# Simultaneous uploads (one household's intakes take turns)


def _race(user: TestUser, cards: list[IntakeCard], monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """
    Ingests `cards` at the same moment from one thread and session each, none naming a batch. Choosing a batch is
    slowed down, so without the household's intake lock both would look for an open batch before either made one.
    """
    barrier = threading.Barrier(len(cards))
    real_insert = IntakeService._insert
    real_find_open = IngestBatchesRepo.find_open

    def insert_together(self: IntakeService, *args: Any) -> Any:
        barrier.wait(10)
        return real_insert(self, *args)

    def slow_find_open(self: IngestBatchesRepo, **kwargs: Any) -> UUID | None:
        found = real_find_open(self, **kwargs)
        time.sleep(0.3)
        return found

    monkeypatch.setattr(IntakeService, "_insert", insert_together)
    monkeypatch.setattr(IngestBatchesRepo, "find_open", slow_find_open)

    outcomes: list[Any] = [None] * len(cards)

    def upload(index: int) -> None:
        with session_context() as session:
            try:
                outcomes[index] = _service(session, user).ingest(cards[index], _options(user))
            except Exception as e:
                outcomes[index] = e

    threads = [threading.Thread(target=upload, args=(index,), daemon=True) for index in range(len(cards))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return outcomes


def test_the_same_card_sent_twice_at_once_is_one_job(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, woken: list[int]
):
    # a Shortcut retried on a timeout, or the phone and Home Assistant at once: on PostgreSQL both used to see no
    # duplicate, each in a batch of its own
    user = unique_user_fn_scoped
    data = _jpeg()
    outcomes = _race(user, [_card(data), _card(data)], monkeypatch)

    accepted = [outcome for outcome in outcomes if isinstance(outcome, IntakeAccepted)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, IntakeRejected)]
    assert len(accepted) == 1, outcomes
    assert len(rejected) == 1, outcomes
    assert rejected[0].reason == IngestRejectReason.duplicate
    assert rejected[0].duplicate_of == accepted[0].job_id
    assert _counts(user) == (1, 1)


def test_two_cards_sent_at_once_share_one_batch(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, woken: list[int]
):
    # two simultaneous first uploads used to start two batches, and so send two notifications
    user = unique_user_fn_scoped
    outcomes = _race(user, [_card(_jpeg()), _card(_jpeg())], monkeypatch)

    accepted = [_accepted(outcome) for outcome in outcomes]
    assert accepted[0].batch_id == accepted[1].batch_id
    assert _counts(user) == (2, 1)
    assert sorted(_job(outcome.job_id).position for outcome in accepted) == [0, 1]


def test_duplicates_allowed_still_share_one_batch(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, woken: list[int]
):
    user = unique_user_fn_scoped
    real_options = _options
    monkeypatch.setattr(
        f"{__name__}._options", lambda user, **values: real_options(user, **{"allow_duplicate": True, **values})
    )
    data = _jpeg()
    outcomes = _race(user, [_card(data), _card(data)], monkeypatch)

    accepted = [_accepted(outcome) for outcome in outcomes]
    assert accepted[0].batch_id == accepted[1].batch_id
    assert _counts(user) == (2, 1)


def test_the_intake_lock_makes_a_second_transaction_wait(unique_user: TestUser, h2_user: TestUser):
    # the database half of the lock, without the process's own: another worker process waits for the household's
    # intake rather than failing (SQLite's busy timeout, PostgreSQL's advisory lock)
    household = UUID(unique_user.household_id)
    held, done = threading.Event(), threading.Event()
    waited: dict[str, Any] = {}

    def second() -> None:
        assert held.wait(10)
        with session_context() as session:
            started = time.monotonic()
            try:
                intake.lock_household_intake(session, household)
                waited["seconds"] = time.monotonic() - started
                session.commit()
            except Exception as e:
                waited["error"] = e
        done.set()

    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    with session_context() as session:
        intake.lock_household_intake(session, household)
        held.set()
        assert not done.wait(0.6)  # still waiting while this transaction holds the lock
        session.commit()
    thread.join(10)
    assert "error" not in waited, waited
    assert waited["seconds"] >= 0.5


def test_intake_locks_are_per_household_on_postgres(unique_user: TestUser, h2_user: TestUser):
    with session_context() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("SQLite has one write lock for the whole database")
        intake.lock_household_intake(session, UUID(unique_user.household_id))
        with session_context() as other:
            other.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
            intake.lock_household_intake(other, UUID(h2_user.household_id))  # doesn't wait
            other.commit()
        session.commit()


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


def test_intake_runs_two_cards_at_a_time_whoever_calls_it(
    db: Session, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    # the inbox's scan calls `ingest` directly, beside uploads that went through `ingest_async`'s limiter: together
    # they expand (a PDF is rendered then) and normalize at most INTAKE_CONCURRENCY files at once (each can take
    # hundreds of megabytes)
    running = {"expand": 0, "normalize": 0}
    most = {"expand": 0, "normalize": 0}
    lock = threading.Lock()
    real_expand = images.expand_document

    @contextmanager
    def counted(phase: str) -> Iterator[None]:
        with lock:
            running[phase] += 1
            most[phase] = max(most[phase], running[phase])
        time.sleep(0.2)
        yield
        with lock:
            running[phase] -= 1

    def expand_document(raw: Any) -> Any:
        with counted("expand"):
            return real_expand(raw)

    def normalize_and_insert(self: IntakeService, *args: Any) -> Any:
        with counted("normalize"):
            return IntakeRejected(0, None, IngestRejectReason.unreadable_image)

    monkeypatch.setattr(images, "expand_document", expand_document)
    monkeypatch.setattr(IntakeService, "_normalize_and_insert", normalize_and_insert)
    service = _service(db, unique_user)
    threads = [
        threading.Thread(target=service.ingest, args=(_card(_jpeg()), _options(unique_user)))
        for _ in range(limits.INTAKE_CONCURRENCY + 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert most == {"expand": limits.INTAKE_CONCURRENCY, "normalize": limits.INTAKE_CONCURRENCY}


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


def test_cloud_providers_over_their_limit_dont_make_local_only_cards_readable(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    # the router finds every provider over its limit before the local-only policy filters them
    user = unique_user_fn_scoped
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    _providers(user, image=True)  # cloud providers
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    monkeypatch.setattr(AIProviderRouter, "_within_limits", lambda self, providers: [])

    over = reading_readiness(db, group_id, household_id)
    assert over.can_read and not over.local_ready

    # a local provider over its limit still counts: the card fails `limit_reached` when it's read, if it still is
    repos = user.repos
    local = repos.group_ai_providers.create(
        AIProviderCreate(name="Ollama", model="m", api_key="k", base_url="http://127.0.0.1:11434/v1", runs_locally=True)
    )
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=local.id, image_provider_id=local.id, audio_provider_id=None),
    )
    local_over = reading_readiness(db, group_id, household_id)
    assert local_over.can_read and local_over.local_ready


@pytest.mark.parametrize("cloud_fallback", [False, True])
def test_a_local_provider_over_its_limit_still_reads_local_only_cards(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, cloud_fallback: bool
):
    # the router drops the local provider (over its limit) before the policy drops the cloud fallback (within its
    # limit): that's still a month's limit, not "no provider on your network", whether or not a fallback is set
    user = unique_user_fn_scoped
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    repos = user.repos
    local = repos.group_ai_providers.create(
        AIProviderCreate(
            name="Ollama",
            model="m",
            api_key="k",
            base_url="http://127.0.0.1:11434/v1",
            runs_locally=True,
            monthly_token_limit=100,
        )
    )
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=local.id, image_provider_id=local.id, audio_provider_id=None),
    )
    if cloud_fallback:
        cloud = repos.group_ai_providers.create(AIProviderCreate(name="Cloud", model="m", api_key="k"))
        repos.group_ai_provider_routes.replace_routes(
            {AIProviderSlot.default: [local.id, cloud.id], AIProviderSlot.image: [local.id, cloud.id]}
        )
    IngestRepos(db, group_id, household_id).settings.upsert(RecipeIngestionSettingsUpdate(local_only=True))

    within = reading_readiness(db, group_id, household_id)
    assert (within.local_ready, within.limit_reached) == (True, False)

    repos.group_ai_usage.create(
        AIUsageLogCreate(
            provider_id=local.id,
            provider_name="Ollama",
            model="m",
            protocol=local.protocol,
            slot=AIProviderSlot.default,
            prompt_tokens=400,
            completion_tokens=100,
            success=True,
        )
    )
    over = reading_readiness(db, group_id, household_id)
    assert (over.can_read, over.local_ready, over.limit_reached) == (True, True, True)


def _provider_over_its_limit(user: TestUser, name: str, *, local: bool = False) -> UUID:
    """A provider whose monthly token limit this month's usage already passed"""
    repos = user.repos
    provider = repos.group_ai_providers.create(
        AIProviderCreate(
            name=name,
            model="m",
            api_key="k",
            monthly_token_limit=100,
            **({"base_url": "http://127.0.0.1:11434/v1", "runs_locally": True} if local else {}),
        )
    )
    repos.group_ai_usage.create(
        AIUsageLogCreate(
            provider_id=provider.id,
            provider_name=name,
            model="m",
            protocol=provider.protocol,
            slot=AIProviderSlot.default,
            prompt_tokens=400,
            completion_tokens=100,
            success=True,
        )
    )
    return provider.id


def test_a_fast_slot_over_its_limit_limits_suggestions(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    _providers(user, image=True)
    user.repos.group_ai_provider_routes.replace_routes({AIProviderSlot.fast: [_provider_over_its_limit(user, "Fast")]})

    # nothing to suggest: no tags, categories or tools, so nothing is missed
    nothing = reading_readiness(db, group_id, household_id)
    assert (nothing.can_read, nothing.limit_reached, nothing.limited_features) == (True, False, ())

    user.repos.tags.create(TagSave(name="Dessert", group_id=user.repos.group_id))
    limited = reading_readiness(db, group_id, household_id)
    assert (limited.can_read, limited.limit_reached) == (True, False)
    assert limited.limited_features == (IngestLimitedFeature.suggestions,)
    assert not db.in_transaction()


def test_an_image_slot_over_its_limit_limits_the_cross_read_when_ocr_reads(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    repos = user.repos
    default = repos.group_ai_providers.create(AIProviderCreate(name="Text", model="m", api_key="k"))
    image = _provider_over_its_limit(user, "Vision")
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=default.id, image_provider_id=image, audio_provider_id=None),
    )
    monkeypatch.setattr(ocr, "is_available", lambda: True)

    # OCR reads the card; without the second reading, nothing else is missed
    assert reading_readiness(db, group_id, household_id).limited_features == ()

    IngestRepos(db, group_id, household_id).settings.upsert(RecipeIngestionSettingsUpdate(cross_read=True))
    limited = reading_readiness(db, group_id, household_id)
    assert (limited.can_read, limited.limit_reached) == (True, False)
    assert limited.limited_features == (IngestLimitedFeature.cross_read,)

    # without OCR the card can't be read at all: that's `limit_reached`, not a missed extra
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    stopped = reading_readiness(db, group_id, household_id)
    assert (stopped.limit_reached, stopped.limited_features) == (True, ())


def test_local_only_limits_count_the_local_providers(
    db: Session, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    # under "local only" a cloud fast provider within its limit doesn't count: the local one over its limit does
    user = unique_user_fn_scoped
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    repos = user.repos
    local = repos.group_ai_providers.create(
        AIProviderCreate(name="Ollama", model="m", api_key="k", base_url="http://127.0.0.1:11434/v1", runs_locally=True)
    )
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=local.id, image_provider_id=local.id, audio_provider_id=None),
    )
    cloud = repos.group_ai_providers.create(AIProviderCreate(name="Cloud", model="m", api_key="k"))
    repos.group_ai_provider_routes.replace_routes(
        {AIProviderSlot.fast: [_provider_over_its_limit(user, "Small", local=True), cloud.id]}
    )
    repos.tags.create(TagSave(name="Dessert", group_id=repos.group_id))

    assert reading_readiness(db, group_id, household_id).limited_features == ()  # the cloud one answers
    IngestRepos(db, group_id, household_id).settings.upsert(RecipeIngestionSettingsUpdate(local_only=True))
    local_only = reading_readiness(db, group_id, household_id)
    assert (local_only.local_ready, local_only.limit_reached) == (True, False)
    assert local_only.limited_features == (IngestLimitedFeature.suggestions,)


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
