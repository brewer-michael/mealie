"""
The inbox folder (docs/ai/PHASE2.md §1.3): per-household folders, settling, the rename claim, opening each file once
without following links, `processed/` and `failed/`, retrying stale claims, crash recovery through the content hash,
and the pause. Runs on SQLite and PostgreSQL.
"""

import calendar
import errno
import io
import os
import stat
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from PIL import Image

from mealie.core.config import get_app_dirs
from mealie.db.db_setup import session_context
from mealie.db.models.group import Group
from mealie.db.models.household import Household
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate
from mealie.schema.recipe_ingest import (
    InboxWaitingReason,
    IngestRejectReason,
    IngestSource,
    PageMeta,
    RecipeIngestionSettingsUpdate,
)
from mealie.services import ocr
from mealie.services.ai.ingest import images, inbox, limits, storage
from mealie.services.ai.ingest import settings as ingest_settings
from mealie.services.ai.ingest.i18n import translator_for
from mealie.services.ai.ingest.settings import IngestSettings
from tests.integration_tests.ai_tests.ingest.card_flow_testing import (
    Notified,
    apprise_sent,  # noqa: F401  (the fixture)
    make_notifier,
    sent_to,
)
from tests.utils.fixture_schemas import TestUser

OLD = 60
"""Seconds: well past the settle time"""


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "is_available", lambda: False)


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "inbox"
    path.mkdir()
    monkeypatch.setattr(inbox, "inbox_root", lambda: path)
    inbox.reset_state()
    yield path
    inbox.reset_state()


def _configure(user: TestUser) -> None:
    repos = user.repos
    default = repos.group_ai_providers.create(AIProviderCreate(name="Text", model="m", api_key="k"))
    vision = repos.group_ai_providers.create(AIProviderCreate(name="Vision", model="m", api_key="k"))
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=default.id, image_provider_id=vision.id, audio_provider_id=None),
    )


@pytest.fixture(scope="module")
def reader(unique_user: TestUser) -> TestUser:
    _configure(unique_user)
    return unique_user


def _slugs(user: TestUser) -> tuple[str, str]:
    with session_context() as session:
        group_slug = session.execute(sa.select(Group.slug).where(Group.id == UUID(user.group_id))).scalar_one()
        household_slug = session.execute(
            sa.select(Household.slug).where(Household.id == UUID(user.household_id))
        ).scalar_one()
    return group_slug, household_slug


def _folder(root: Path, user: TestUser) -> Path:
    group_slug, household_slug = _slugs(user)
    folder = root / group_slug / household_slug
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _jpeg(size: tuple[int, int] = (80, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _age(path: Path, seconds: float = OLD) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then), follow_symlinks=False)


def _drop(folder: Path, name: str, data: bytes | None = None, *, age: float = OLD) -> Path:
    path = folder / name
    path.write_bytes(_jpeg() if data is None else data)
    _age(path, age)
    return path


def _jobs(user: TestUser) -> list[RecipeIngestionJob]:
    with session_context() as session:
        jobs = list(
            session.execute(
                sa.select(RecipeIngestionJob)
                .where(RecipeIngestionJob.household_id == UUID(user.household_id))
                .order_by(RecipeIngestionJob.created_at)
            ).scalars()
        )
        session.expunge_all()
        return jobs


def _month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _claimed(folder: Path) -> list[str]:
    claim_dir = folder / inbox.CLAIM_DIR
    return sorted(os.listdir(claim_dir)) if claim_dir.exists() else []


def _scan_twice() -> int:
    """The first scan sees the files, the second takes the ones that didn't change"""
    return inbox.scan_once() + inbox.scan_once()


def _tree(path: Path) -> dict[str, bytes | str]:
    """Everything under `path`, links not followed: each entry's relative path, a file's bytes or a link's target"""
    tree: dict[str, bytes | str] = {}
    for entry in sorted(path.rglob("*")):
        key = entry.relative_to(path).as_posix()
        if entry.is_symlink():
            tree[key] = f"-> {os.readlink(entry)}"
        elif entry.is_file():
            tree[key] = entry.read_bytes()
        else:
            tree[key] = "dir"
    return tree


def _data_dir(tmp_path: Path) -> Path:
    """A directory outside the inbox, standing in for DATA_DIR: a database and a backup, both long settled"""
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    _drop(data, "mealie.db", b"SQLite format 3\x00 precious")
    _drop(data / "backups", "mealie_2026.10.01.zip", b"PK\x03\x04 backup")
    return data


@pytest.fixture()
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    logged: list[str] = []
    monkeypatch.setattr(inbox.logger, "warning", logged.append)
    return logged


# ==========================================
# Folders


def test_every_household_gets_a_folder_and_unknown_folders_are_logged_once(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    warnings: list[str] = []
    monkeypatch.setattr(inbox.logger, "warning", warnings.append)
    group_slug, household_slug = _slugs(reader)
    (root / "not-a-group").mkdir()
    (root / group_slug / "not-a-household").mkdir(parents=True)

    assert _scan_twice() == 0
    assert inbox.scan_once() == 0
    assert (root / group_slug / household_slug).is_dir()
    assert len([message for message in warnings if "not-a-group" in message]) == 1
    assert len([message for message in warnings if "not-a-household" in message]) == 1


def test_the_inbox_is_refused_inside_the_data_directory(monkeypatch: pytest.MonkeyPatch):
    inside = get_app_dirs().DATA_DIR / "inbox"
    monkeypatch.setattr(ingest_settings, "get_ingest_settings", lambda: IngestSettings(INBOX_DIR=inside))
    ingest_settings.inbox_root.cache_clear()
    try:
        assert ingest_settings.inbox_root() is None
        assert inbox.scan_once() == 0
    finally:
        ingest_settings.inbox_root.cache_clear()
    assert not inside.exists()


# ==========================================
# Taking files


def test_a_settled_file_becomes_an_inbox_job_and_moves_to_processed(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    before = len(_jobs(reader))
    _drop(folder, "card_20261003_101500.jpg")

    assert inbox.scan_once() == 0  # seen once: not settled yet
    assert (folder / "card_20261003_101500.jpg").exists()
    assert inbox.scan_once() == 1

    assert not (folder / "card_20261003_101500.jpg").exists()
    assert (folder / "processed" / _month() / "card_20261003_101500.jpg").is_file()
    assert _claimed(folder) == []

    [job] = _jobs(reader)[before:]
    group_slug, household_slug = _slugs(reader)
    assert job.source == "inbox"
    assert job.created_by is None
    assert job.source_name == f"inbox/{group_slug}/{household_slug}/card_20261003_101500.jpg"
    assert job.locale == "en-US"
    assert [PageMeta.model_validate(page).original_filename for page in job.pages] == ["card_20261003_101500.jpg"]
    with session_context() as session:
        batch = session.get(RecipeIngestionBatch, job.batch_id)
        assert batch is not None
        assert (batch.source, batch.source_key, batch.created_by) == ("inbox", f"{group_slug}/{household_slug}", None)


def test_inbox_cards_of_one_folder_join_one_batch(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    before = len(_jobs(reader))
    _drop(folder, "a.jpg")
    _drop(folder, "b.jpg")
    assert _scan_twice() == 2
    first, second = _jobs(reader)[before:]
    assert first.batch_id == second.batch_id
    assert abs(first.position - second.position) == 1  # arrival order (the batch may hold earlier cards)


def test_files_still_being_written_wait(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    growing = _drop(folder, "growing.jpg")
    fresh = _drop(folder, "fresh.jpg", age=0)

    assert inbox.scan_once() == 0
    with growing.open("ab") as file:
        file.write(b"\0" * 10)
    _age(growing)
    assert inbox.scan_once() == 0  # its size changed
    assert growing.exists() and fresh.exists()  # fresh is unchanged, but younger than the settle time

    assert inbox.scan_once() == 1
    assert not growing.exists()
    assert fresh.exists()


def test_symlinks_hidden_files_and_partial_downloads_are_skipped(
    root: Path, reader: TestUser, tmp_path: Path, warnings: list[str]
):
    folder = _folder(root, reader)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(_jpeg())
    names = [".hidden.jpg", "~lock.jpg", "a.jpg.part", "b.crdownload", "c.tmp", "Thumbs.db", "desktop.ini"]
    for name in names:
        _drop(folder, name)
    (folder / "link.jpg").symlink_to(outside)
    (folder / "inner-link.jpg").symlink_to(folder / ".hidden.jpg")
    os.mkfifo(folder / "pipe.jpg")
    before = len(_jobs(reader))

    assert _scan_twice() == 0
    assert inbox.scan_once() == 0
    assert len(_jobs(reader)) == before
    for name in [*names, "link.jpg", "inner-link.jpg", "pipe.jpg"]:
        assert os.path.lexists(folder / name), name
    assert outside.exists()
    assert (folder / "link.jpg").is_symlink() and (folder / "inner-link.jpg").is_symlink()

    # a link stays where it is, so it's logged, once per name, rather than skipped silently forever
    group_slug, household_slug = _slugs(reader)
    assert sorted(message for message in warnings if "link" in message) == [
        f"Skipped {group_slug}/{household_slug}/{name} in the recipe card inbox: links aren't followed. Put the "
        "photo itself in the folder."
        for name in ("inner-link.jpg", "link.jpg")
    ]


def test_a_subfolder_is_one_card_with_its_pages_in_name_order(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    card = folder / "grandmas-pie"
    card.mkdir()
    back = _drop(card, "2-back.jpg", _jpeg((60, 80)))
    _drop(card, "1-front.jpg", _jpeg((80, 60)))
    _drop(card, ".DS_Store", b"junk")
    before = len(_jobs(reader))

    assert inbox.scan_once() == 0
    back.write_bytes(_jpeg((60, 80)))  # still being copied
    _age(back)
    assert inbox.scan_once() == 0
    assert inbox.scan_once() == 1

    [job] = _jobs(reader)[before:]
    pages = [PageMeta.model_validate(page) for page in job.pages]
    assert [(page.original_filename, page.width) for page in pages] == [("1-front.jpg", 80), ("2-back.jpg", 60)]
    assert job.source_name.endswith("/grandmas-pie")
    assert (folder / "processed" / _month() / "grandmas-pie" / "2-back.jpg").is_file()


def _pdf(*sizes: tuple[int, int]) -> bytes:
    pages = [Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)) for size in sizes]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:], resolution=72)
    return buffer.getvalue()


def test_a_scanners_pdf_is_one_card_with_its_pages(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 500)  # rendered small: quicker
    folder = _folder(root, reader)
    _drop(folder, "scan.pdf", _pdf((300, 200), (200, 300)))
    before = len(_jobs(reader))

    assert _scan_twice() == 1

    [job] = _jobs(reader)[before:]
    pages = [PageMeta.model_validate(page) for page in job.pages]
    assert [(page.original_filename, page.width, page.height, page.format) for page in pages] == [
        ("scan.pdf (page 1)", 500, 334, "pdf"),
        ("scan.pdf (page 2)", 334, 500, "pdf"),
    ]
    assert job.source_name.endswith("/scan.pdf")
    assert (folder / "processed" / _month() / "scan.pdf").is_file()


def test_a_rejected_file_goes_to_failed_with_the_reason(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    _drop(folder, "menu.pdf", b"%PDF-1.7 a menu")
    before = len(_jobs(reader))
    assert _scan_twice() == 0
    assert len(_jobs(reader)) == before
    assert (folder / "failed" / "menu.pdf").read_bytes() == b"%PDF-1.7 a menu"
    assert "pdf_not_supported" in (folder / "failed" / "menu.pdf.error.txt").read_text()
    assert _claimed(folder) == []


def test_a_second_file_with_the_same_name_gets_a_unique_name(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    _drop(folder, "snapshot.jpg")
    assert _scan_twice() == 1
    _drop(folder, "snapshot.jpg")
    assert _scan_twice() == 1
    processed = sorted(os.listdir(folder / "processed" / _month()))
    assert "snapshot.jpg" in processed
    assert len([name for name in processed if name.startswith("snapshot")]) == 2


def test_processed_files_can_be_deleted_instead(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox, "get_ingest_settings", lambda: IngestSettings(INBOX_KEEP_PROCESSED=False, WORKER=False))
    folder = _folder(root, reader)
    _drop(folder, "gone.jpg")
    assert _scan_twice() == 1
    assert not (folder / "gone.jpg").exists()
    assert not (folder / "processed" / _month() / "gone.jpg").exists()
    assert _claimed(folder) == []


def test_a_group_that_cant_read_cards_keeps_its_files(root: Path, unique_user_fn_scoped: TestUser):
    folder = _folder(root, unique_user_fn_scoped)
    _drop(folder, "waiting.jpg")
    assert _scan_twice() == 0
    assert (folder / "waiting.jpg").exists()
    assert _claimed(folder) == []

    _configure(unique_user_fn_scoped)
    assert inbox.scan_once() == 1


def test_a_local_only_group_without_local_readers_keeps_its_files(root: Path, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    _configure(user)  # cloud providers only
    with session_context() as session:
        IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).settings.upsert(
            RecipeIngestionSettingsUpdate(local_only=True)
        )
    folder = _folder(root, user)
    _drop(folder, "private.jpg")
    assert _scan_twice() == 0
    assert (folder / "private.jpg").exists()


def test_a_group_at_its_quota_keeps_its_files(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "MAX_PROCESSING_JOBS_PER_GROUP", 0)
    folder = _folder(root, reader)
    _drop(folder, "later.jpg")
    assert _scan_twice() == 0
    assert (folder / "later.jpg").exists()


def test_the_per_user_cap_leaves_inbox_cards_alone(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    # inbox cards have no uploader: only the group's quota holds them back
    monkeypatch.setattr(inbox, "get_ingest_settings", lambda: IngestSettings(MAX_PROCESSING_PER_USER=1, WORKER=False))
    folder = _folder(root, reader)
    before = len(_jobs(reader))
    _drop(folder, "a.jpg")
    _drop(folder, "b.jpg")
    assert _scan_twice() == 2
    assert len(_jobs(reader)) == before + 2


def test_at_most_twenty_files_a_scan(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "INBOX_FILES_PER_TICK", 2)
    folder = _folder(root, reader)
    for n in range(3):
        _drop(folder, f"{n}.jpg", _jpeg((8, 8)))
    assert _scan_twice() == 2
    assert inbox.scan_once() == 1


def test_a_card_folder_skips_nested_folders_and_links(
    root: Path, reader: TestUser, tmp_path: Path, warnings: list[str]
):
    # a NAS's media indexer adds `@eaDir/` to every folder; neither it nor a link is a page
    folder = _folder(root, reader)
    card = folder / "grandmas-pie"
    (card / "@eaDir" / "x").mkdir(parents=True)
    _drop(card, "1-front.jpg", _jpeg((80, 60)))
    _drop(card, "2-back.jpg", _jpeg((60, 80)))
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(_jpeg())
    (card / "3-link.jpg").symlink_to(outside)
    before = len(_jobs(reader))

    assert _scan_twice() == 1
    [job] = _jobs(reader)[before:]
    assert [PageMeta.model_validate(page).original_filename for page in job.pages] == ["1-front.jpg", "2-back.jpg"]
    assert (folder / "processed" / _month() / "grandmas-pie" / "@eaDir").is_dir()
    assert outside.exists()
    group_slug, household_slug = _slugs(reader)
    assert [message for message in warnings if "link" in message] == [
        f"Skipped {group_slug}/{household_slug}/grandmas-pie/3-link.jpg in the recipe card inbox: links aren't "
        "followed. Put the photo itself in the folder."
    ]


def test_a_card_folder_with_too_many_pages_is_refused_before_any_is_opened(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    folder = _folder(root, reader)
    card = folder / "stack"
    card.mkdir()
    for n in range(limits.MAX_PAGES_PER_CARD + 1):
        _drop(card, f"{n}.jpg", _jpeg((8, 8)))
    opened: list[Any] = []
    real_open = inbox.open_page
    monkeypatch.setattr(inbox, "open_page", lambda *args, **kwargs: opened.append(args) or real_open(*args, **kwargs))
    before = len(_jobs(reader))

    assert _scan_twice() == 0
    assert len(_jobs(reader)) == before
    assert opened == []  # a folder of thousands of files would hold thousands of descriptors
    assert sorted(os.listdir(folder / "failed" / "stack")) == [f"{n}.jpg" for n in range(limits.MAX_PAGES_PER_CARD + 1)]
    assert "too_many_pages" in (folder / "failed" / "stack.error.txt").read_text()


def test_a_file_name_that_isnt_utf8_is_taken_like_any_other(root: Path, reader: TestUser):
    # NFS shares and network scanners write Latin-1 names: they must neither stop the scan nor reach the database
    folder = _folder(root, reader)
    latin1 = os.path.join(os.fsencode(folder), b"r\xe9cipe.jpg")
    with open(latin1, "wb") as file:
        file.write(_jpeg())
    _age(Path(os.fsdecode(latin1)))
    _drop(folder, "good.jpg")
    before = len(_jobs(reader))

    assert _scan_twice() == 2
    jobs = _jobs(reader)[before:]
    group_slug, household_slug = _slugs(reader)
    assert sorted(job.source_name for job in jobs) == [
        f"inbox/{group_slug}/{household_slug}/good.jpg",
        f"inbox/{group_slug}/{household_slug}/r\ufffdcipe.jpg",
    ]
    assert os.path.exists(os.path.join(os.fsencode(folder / "processed" / _month()), b"r\xe9cipe.jpg"))


def test_an_entry_that_cant_be_claimed_is_skipped_and_logged_once(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch, warnings: list[str]
):
    folder = _folder(root, reader)
    _drop(folder, "a-locked.jpg", age=OLD + 10)  # the oldest: tried first
    _drop(folder, "b-fine.jpg")
    real_claim = inbox.claim

    def claim(dirs: Any, name: str) -> str | None:
        if name == "a-locked.jpg":
            raise PermissionError(13, "Permission denied")
        return real_claim(dirs, name)

    monkeypatch.setattr(inbox, "claim", claim)
    assert _scan_twice() == 1
    assert inbox.scan_once() == 0
    assert (folder / "a-locked.jpg").exists()
    assert len([message for message in warnings if "a-locked.jpg" in message]) == 1


# ==========================================
# Links and other things a share writer can plant


def test_a_household_folder_that_is_a_link_is_never_used(
    root: Path, reader: TestUser, tmp_path: Path, warnings: list[str]
):
    data = _data_dir(tmp_path)
    folder = _folder(root, reader)
    folder.rmdir()
    folder.symlink_to(data, target_is_directory=True)
    before = _tree(data)

    assert _scan_twice() == 0
    assert inbox.scan_once() == 0
    assert _tree(data) == before  # nothing claimed, moved, failed or created
    assert folder.is_symlink()
    household_slug = _slugs(reader)[1]
    assert len([message for message in warnings if household_slug in message and "link" in message]) == 1
    assert not [message for message in warnings if household_slug in message and "isn't a household" in message]


def test_a_group_folder_that_is_a_link_is_never_used(
    root: Path, reader: TestUser, h2_user: TestUser, tmp_path: Path, warnings: list[str]
):
    data = _data_dir(tmp_path)
    group_slug, household_slug = _slugs(reader)
    (data / household_slug).mkdir()
    _drop(data / household_slug, "card.jpg")
    (root / group_slug).symlink_to(data, target_is_directory=True)
    before = _tree(data)

    assert _scan_twice() == 0
    assert _tree(data) == before  # no household folder made in it, and its card left alone
    assert len([message for message in warnings if f"{group_slug} " in message and "link" in message]) == 1


@pytest.mark.parametrize("reserved", [inbox.CLAIM_DIR, inbox.FAILED_DIR, inbox.PROCESSED_DIR])
def test_a_reserved_folder_that_is_a_link_or_a_file_stops_only_its_household(
    root: Path, reader: TestUser, h2_user: TestUser, tmp_path: Path, warnings: list[str], reserved: str
):
    data = _data_dir(tmp_path)
    folders = sorted([_folder(root, reader), _folder(root, h2_user)], key=lambda path: (path.parent.name, path.name))
    broken, working = folders  # the broken one is scanned first
    (broken / reserved).symlink_to(data, target_is_directory=True)
    _drop(broken, "card.jpg")
    _drop(broken, "menu.pdf", b"%PDF-1.7 a menu")
    _drop(working, "card.jpg")
    before = _tree(data)

    assert _scan_twice() == 1
    assert _tree(data) == before
    assert sorted(os.listdir(broken)) == sorted(["card.jpg", "menu.pdf", reserved])
    assert (working / "processed" / _month() / "card.jpg").exists()
    assert len([message for message in warnings if reserved in message]) == 1

    # a stray file in its place is refused the same way
    (broken / reserved).unlink()
    (broken / reserved).write_text("x")
    inbox.reset_state()
    assert _scan_twice() == 0
    assert sorted(os.listdir(broken)) == sorted(["card.jpg", "menu.pdf", reserved])


@pytest.mark.parametrize("dangling", [False, True])
def test_a_planted_error_note_is_never_written_through(root: Path, reader: TestUser, tmp_path: Path, dangling: bool):
    folder = _folder(root, reader)
    secret = tmp_path / "data" / ".secret"
    secret.parent.mkdir()
    if not dangling:
        secret.write_bytes(b"SECRETKEY-abc123")
    (folder / "failed").mkdir()
    (folder / "failed" / "card.jpg.error.txt").symlink_to(secret)
    _drop(folder, "card.jpg", b"not an image at all")

    assert _scan_twice() == 0
    if dangling:
        assert not os.path.lexists(secret)
    else:
        assert secret.read_bytes() == b"SECRETKEY-abc123"
    assert (folder / "failed" / "card.jpg.error.txt").is_symlink()
    [card] = [name for name in os.listdir(folder / "failed") if not name.endswith(".error.txt")]
    assert card != "card.jpg" and card.startswith("card-")  # beside a note of its own
    note = folder / "failed" / f"{card}.error.txt"
    assert not note.is_symlink() and "unsupported_format" in note.read_text()


def test_a_folder_swapped_for_a_link_during_the_scan_is_never_followed(
    root: Path, reader: TestUser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings: list[str]
):
    data = _data_dir(tmp_path)
    folder = _folder(root, reader)
    (folder / "failed").mkdir()
    _drop(folder, "menu.pdf", b"%PDF-1.7 a menu")
    real_claim = inbox.claim

    def claim_then_swap(*args: Any) -> str | None:
        claimed = real_claim(*args)
        (folder / "failed").rmdir()
        (folder / "failed").symlink_to(data, target_is_directory=True)
        return claimed

    monkeypatch.setattr(inbox, "claim", claim_then_swap)
    before = _tree(data)
    assert _scan_twice() == 0
    assert _tree(data) == before
    assert len(_claimed(folder)) == 1  # retried once the folder is fixed
    assert len([message for message in warnings if "failed" in message and "link" in message]) == 1


# ==========================================
# Races and crashes


def test_two_scanners_racing_on_one_file_ingest_it_once(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    folder = _folder(root, reader)
    _drop(folder, "contested.jpg")
    assert inbox.scan_once() == 0
    before = len(_jobs(reader))

    both_ready = threading.Barrier(2, timeout=10)
    real_claim = inbox.claim

    def claim_together(*args: Any) -> Path | None:
        both_ready.wait()  # both scanners rename at the same moment
        return real_claim(*args)

    monkeypatch.setattr(inbox, "claim", claim_together)
    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(inbox.scan_once())) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(results) == [0, 1]
    assert len(_jobs(reader)) == before + 1
    assert (folder / "processed" / _month() / "contested.jpg").exists()


def test_a_symlink_swapped_in_after_the_claim_is_refused(
    root: Path, reader: TestUser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    folder = _folder(root, reader)
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(_jpeg())
    _drop(folder, "swapped.jpg")
    real_claim = inbox.claim

    def claim_then_swap(*args: Any) -> str | None:
        claimed = real_claim(*args)
        assert claimed is not None
        path = folder / inbox.CLAIM_DIR / claimed
        path.unlink()
        path.symlink_to(secret)
        return claimed

    monkeypatch.setattr(inbox, "claim", claim_then_swap)
    before = len(_jobs(reader))
    assert _scan_twice() == 0
    assert len(_jobs(reader)) == before
    assert (folder / "failed" / "swapped.jpg").is_symlink()
    assert "Not added" in (folder / "failed" / "swapped.jpg.error.txt").read_text()
    assert secret.exists()


def test_a_page_outside_the_inbox_is_refused(root: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    page = outside / "page.jpg"
    page.write_bytes(_jpeg())
    with pytest.raises(inbox._Refused):
        inbox.open_page(page, root)
    with pytest.raises(inbox._Refused):
        inbox.open_page(root, root)  # not a regular file
    inside = root / "page.jpg"
    inside.write_bytes(b"x")
    with inbox.open_page(inside, root) as file:
        assert file.read() == b"x"


def test_a_fresh_claim_of_an_old_file_isnt_retried_by_another_scanner(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    claim_dir = folder / inbox.CLAIM_DIR
    claim_dir.mkdir(exist_ok=True)
    name = f"{inbox._now_ms()}__{uuid4().hex}__old-photo.jpg"
    claimed = _drop(claim_dir, name, age=30 * 24 * 3600)  # rename and Syncthing keep the old mtime
    before = len(_jobs(reader))

    assert _scan_twice() == 0
    assert claimed.exists()
    assert len(_jobs(reader)) == before


def test_a_stale_claim_is_claimed_again_and_retried(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    claim_dir = folder / inbox.CLAIM_DIR
    claim_dir.mkdir(exist_ok=True)
    stale_ms = inbox._now_ms() - (limits.INBOX_CLAIM_RETRY + 60) * 1000
    _drop(claim_dir, f"{stale_ms}__{uuid4().hex}__abandoned.jpg", age=0)
    before = len(_jobs(reader))

    assert inbox.scan_once() == 1
    assert len(_jobs(reader)) == before + 1
    assert _claimed(folder) == []
    assert (folder / "processed" / _month() / "abandoned.jpg").exists()


def test_a_crash_after_the_insert_is_recovered_without_a_second_job(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    folder = _folder(root, reader)
    _drop(folder, "crash.jpg")
    real_finish = inbox.finish

    def crash(*args: Any) -> None:
        raise OSError("the process died here")

    monkeypatch.setattr(inbox, "finish", crash)
    before = len(_jobs(reader))
    assert _scan_twice() == 1
    [claimed] = _claimed(folder)  # inserted, but never moved on

    # ten minutes later, the next scan retries the claim; the content hash finds the job
    monkeypatch.setattr(inbox, "finish", real_finish)
    assert inbox.scan_once() == 0  # not stale yet
    old_ms = inbox._now_ms() - (limits.INBOX_CLAIM_RETRY + 1) * 1000
    os.rename(folder / inbox.CLAIM_DIR / claimed, folder / inbox.CLAIM_DIR / f"{old_ms}__{uuid4().hex}__crash.jpg")
    assert inbox.scan_once() == 0

    assert len(_jobs(reader)) == before + 1
    assert _claimed(folder) == []
    assert (folder / "processed" / _month() / "crash.jpg").exists()
    assert not (folder / "failed").exists() or not os.listdir(folder / "failed")


def test_a_claim_lost_before_the_insert_creates_nothing(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    folder = _folder(root, reader)
    _drop(folder, "retried-elsewhere.jpg")
    real_open = inbox._open_card

    def open_then_lose_the_claim(dirs: Any, claimed: str, inbox_root: Path) -> Any:
        pages = real_open(dirs, claimed, inbox_root)
        claim_dir = folder / inbox.CLAIM_DIR
        os.rename(claim_dir / claimed, claim_dir / f"{inbox._now_ms()}__{uuid4().hex}__retried-elsewhere.jpg")
        return pages

    monkeypatch.setattr(inbox, "_open_card", open_then_lose_the_claim)
    before = len(_jobs(reader))
    job_dirs = storage.ingest_root(UUID(reader.group_id))
    group_dirs = set(os.listdir(job_dirs)) if job_dirs.exists() else set()
    assert _scan_twice() == 0
    assert len(_jobs(reader)) == before
    assert (set(os.listdir(job_dirs)) if job_dirs.exists() else set()) == group_dirs
    assert len(_claimed(folder)) == 1  # the other scanner's claim, untouched


# ==========================================
# The pause


def test_nothing_is_scanned_while_paused(root: Path, reader: TestUser):
    folder = _folder(root, reader)
    _drop(folder, "paused.jpg")
    inbox.scan_once()
    storage.pause_marker_path().write_text(str(time.time()))
    try:
        assert inbox.scan_once() == 0
    finally:
        storage.pause_marker_path().unlink(missing_ok=True)
    assert (folder / "paused.jpg").exists()
    assert inbox.scan_once() == 1


def test_the_pause_is_checked_before_each_file(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    folder = _folder(root, reader)
    _drop(folder, "first.jpg")
    _drop(folder, "second.jpg")
    inbox.scan_once()
    real_ingest = inbox._ingest_claimed

    def ingest_then_pause(*args: Any, **kwargs: Any) -> bool:
        created = real_ingest(*args, **kwargs)
        storage.pause_marker_path().write_text(str(time.time()))
        return created

    monkeypatch.setattr(inbox, "_ingest_claimed", ingest_then_pause)
    try:
        assert inbox.scan_once() == 1
    finally:
        storage.pause_marker_path().unlink(missing_ok=True)
    assert [os.path.exists(folder / name) for name in ("first.jpg", "second.jpg")].count(True) == 1
    assert _claimed(folder) == []


# ==========================================
# The folders Mealie creates are writable by its group


@pytest.fixture()
def umask_022() -> Iterator[None]:
    """The container's usual umask, which strips group write from a plain `mkdir`"""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _created_folders(root: Path, user: TestUser) -> list[Path]:
    """Lets the scan create every folder it makes: the group's, the household's, the claim folder, `processed/` (with
    its month) and `failed/`"""
    group_slug, household_slug = _slugs(user)
    assert inbox.scan_once() == 0
    folder = root / group_slug / household_slug
    _drop(folder, "card.jpg")
    _drop(folder, "menu.pdf", b"%PDF-1.7 a menu")
    _scan_twice()
    return [
        root / group_slug,
        folder,
        folder / inbox.CLAIM_DIR,
        folder / "processed",
        folder / "processed" / _month(),
        folder / "failed",
    ]


def test_created_folders_are_setgid_and_group_writable(root: Path, reader: TestUser, umask_022: None):
    for path in _created_folders(root, reader):
        assert _mode(path) == 0o2775, path


def test_the_folder_mode_is_a_setting(root: Path, reader: TestUser, umask_022: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox, "get_ingest_settings", lambda: IngestSettings(INBOX_DIR_MODE="755", WORKER=False))
    for path in _created_folders(root, reader):
        assert _mode(path) == 0o755, path


def test_existing_folders_keep_their_mode(root: Path, reader: TestUser, umask_022: None):
    group_slug, household_slug = _slugs(reader)
    folder = root / group_slug / household_slug
    for path, mode in ((root / group_slug, 0o750), (folder, 0o700), (folder / "processed", 0o711)):
        path.mkdir(exist_ok=True)
        os.chmod(path, mode)

    _drop(folder, "card.jpg")
    assert _scan_twice() == 1
    assert (_mode(root / group_slug), _mode(folder), _mode(folder / "processed")) == (0o750, 0o700, 0o711)
    assert _mode(folder / "processed" / _month()) == 0o2775  # the one Mealie made


# ==========================================
# processed/ is purged after AI_INGEST_INBOX_PROCESSED_DAYS


DAY = 86400


def _keep_days(monkeypatch: pytest.MonkeyPatch, days: int | None) -> None:
    monkeypatch.setattr(inbox, "get_ingest_settings", lambda: IngestSettings(INBOX_PROCESSED_DAYS=days, WORKER=False))


def _processed(root: Path, user: TestUser, month: str) -> Path:
    path = _folder(root, user) / "processed" / month
    path.mkdir(parents=True, exist_ok=True)
    return path


def _purge(root: Path, user: TestUser, days: int, now: float) -> Any:
    """One purge of `user`'s household, `now` being seconds since the epoch"""
    group_slug, household_slug = _slugs(user)
    folder = inbox.HouseholdFolder(UUID(user.group_id), UUID(user.household_id), group_slug, household_slug)
    root_fd = os.open(root, os.O_RDONLY)
    try:
        return inbox.purge_processed(root_fd, [folder], days, now)
    finally:
        os.close(root_fd)


def test_files_processed_before_the_cutoff_are_removed(root: Path, reader: TestUser, tmp_path: Path):
    # 40 days from now, with a 30-day setting: what's in processed/ today is ten days past it
    later = time.time() + 40 * DAY
    month = _processed(root, reader, _month())
    old = _drop(month, "old.jpg")
    recent = _drop(month, "recent.jpg")
    os.utime(recent, (later - DAY, later - DAY))  # changed since: kept by its mtime
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not the inbox's")
    os.utime(outside, (0, 0))
    (month / "link.jpg").symlink_to(outside)
    (month / "nested").mkdir()
    _drop(month / "nested", "deep.jpg")

    purged = _purge(root, reader, 30, later)
    assert purged.files == 1
    assert not old.exists()
    assert recent.exists()
    assert (month / "link.jpg").is_symlink() and outside.read_bytes() == b"not the inbox's"
    assert (month / "nested" / "deep.jpg").exists()


def test_an_old_photo_moved_in_today_is_kept(root: Path, reader: TestUser):
    # cp -p, rsync and Syncthing keep a photo's old mtime: the move into processed/ (its ctime) is what counts
    month = _processed(root, reader, "2019-01")
    photo = _drop(month, "from-2019.jpg")
    os.utime(photo, (1_546_300_800, 1_546_300_800))
    assert _purge(root, reader, 30, time.time()).files == 0
    assert photo.exists()


def test_an_emptied_month_folder_is_removed_but_not_this_months(root: Path, reader: TestUser):
    later = float(calendar.timegm((2030, 1, 25, 12, 0, 0)))
    old_month = _processed(root, reader, "2001-02")
    _drop(old_month, "a.jpg")
    current = _processed(root, reader, "2030-01")  # empty, and the month new cards go into
    other = _processed(root, reader, "not-a-month")
    _drop(other, "c.jpg")

    purged = _purge(root, reader, 1, later)
    assert not old_month.exists()
    assert current.is_dir()
    assert (other / "c.jpg").exists()  # only YYYY-MM folders are Mealie's
    assert (purged.files, purged.folders) == (1, 1)


def test_a_purge_looks_at_most_at_its_budget(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox, "PURGE_ENTRIES", 3)
    later = time.time() + 40 * DAY
    month = _processed(root, reader, "2002-03")
    for n in range(5):
        _drop(month, f"{n}.jpg")
    first = _purge(root, reader, 30, later)
    assert first.files == 3 and first.exhausted
    assert len(os.listdir(month)) == 2
    second = _purge(root, reader, 30, later)
    assert second.files == 2 and not month.exists()


def test_a_processed_folder_that_is_a_link_is_never_followed(root: Path, reader: TestUser, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere" / "2003-04"
    elsewhere.mkdir(parents=True)
    _drop(elsewhere, "keep.jpg")
    folder = _folder(root, reader)
    (folder / "processed").symlink_to(tmp_path / "elsewhere")
    assert _purge(root, reader, 30, time.time() + 40 * DAY).files == 0
    assert (elsewhere / "keep.jpg").exists()


def test_the_scan_purges_once_a_day_starting_ten_minutes_in(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    _keep_days(monkeypatch, 1)
    clock = [1000.0]
    monkeypatch.setattr(inbox, "_monotonic", lambda: clock[0])
    runs: list[float] = []
    real = inbox.purge_processed

    def purge(*args: Any) -> Any:
        runs.append(clock[0])
        return real(*args)

    monkeypatch.setattr(inbox, "purge_processed", purge)
    inbox.scan_once()
    assert runs == []
    clock[0] += limits.PURGE_FIRST_DELAY - 1
    inbox.scan_once()
    assert runs == []
    clock[0] += 1
    inbox.scan_once()
    inbox.scan_once()
    assert runs == [1000.0 + limits.PURGE_FIRST_DELAY]
    clock[0] += limits.PURGE_INTERVAL
    inbox.scan_once()
    assert len(runs) == 2


def test_unset_keeps_everything(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    _keep_days(monkeypatch, None)
    month = _processed(root, reader, "2004-05")
    photo = _drop(month, "kept.jpg")
    monkeypatch.setattr(inbox, "_monotonic", lambda: 10.0**9)  # long past the first delay
    inbox.reset_state()
    calls: list[Any] = []
    monkeypatch.setattr(inbox, "purge_processed", lambda *args: calls.append(args))
    inbox.scan_once()
    inbox.scan_once()
    assert calls == []
    assert photo.exists()


def test_a_purge_logs_one_summary(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    logged: list[str] = []
    monkeypatch.setattr(inbox.logger, "info", logged.append)
    month = _processed(root, reader, "2005-06")
    _drop(month, "a.jpg")
    _drop(month, "b.jpg")
    _purge(root, reader, 30, time.time() + 40 * DAY)
    assert logged == [
        "Removed 2 files processed more than 30 days ago (and 1 empty month folders) from the recipe card inbox"
    ]


def test_a_purge_that_stops_at_its_budget_goes_on_at_the_next_scan(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    _keep_days(monkeypatch, 30)
    monkeypatch.setattr(inbox, "PURGE_ENTRIES", 1)
    monkeypatch.setattr(inbox, "_wall_clock", lambda: time.time() + 40 * DAY)
    clock = [5000.0]
    monkeypatch.setattr(inbox, "_monotonic", lambda: clock[0])
    month = _processed(root, reader, "2006-07")
    _drop(month, "a.jpg")
    _drop(month, "b.jpg")

    inbox.scan_once()
    clock[0] += limits.PURGE_FIRST_DELAY
    inbox.scan_once()
    assert len(os.listdir(month)) == 1
    inbox.scan_once()  # no day's wait: there's more to look at
    inbox.scan_once()
    assert not month.exists()


# ==========================================
# Telling about refusals, and the status the app shows


@pytest.fixture()
def told(monkeypatch: pytest.MonkeyPatch) -> list[tuple[UUID, UUID, list[Any]]]:
    """The "not added" notifications the scan asks for (household, reasons), instead of sending them"""
    calls: list[tuple[UUID, UUID, list[Any]]] = []

    def notify(group_id: UUID, household_id: UUID, reasons: Any, **kwargs: Any) -> bool:
        calls.append((group_id, household_id, sorted(str(reason) for reason in reasons)))
        return True

    monkeypatch.setattr(inbox.events, "notify_inbox_rejections", notify)
    return calls


@pytest.fixture()
def info(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    logged: list[str] = []
    monkeypatch.setattr(inbox.logger, "info", logged.append)
    return logged


def test_each_refusal_logs_its_code_and_nothing_of_the_file(root: Path, reader: TestUser, info: list[str], told: list):
    folder = _folder(root, reader)
    _drop(folder, "grandmas-secret-fudge.txt", b"not an image at all")
    (folder / "empty-card").mkdir()
    _drop(folder / "empty-card", "notes.txt", b"also not an image")
    assert _scan_twice() == 0

    group_slug, household_slug = _slugs(reader)
    key = f"{group_slug}/{household_slug}"
    assert sorted(message for message in info if "wasn't added" in message) == [
        f"A file in the recipe card inbox of {key} wasn't added (unsupported_format)",
        f"A file in the recipe card inbox of {key} wasn't added (unsupported_format)",
    ]
    assert not any("fudge" in message or "empty-card" in message for message in info)


def test_a_burst_of_refusals_is_told_once(root: Path, reader: TestUser, told: list):
    folder = _folder(root, reader)
    _drop(folder, "notes.txt", b"not an image")
    _drop(folder, "menu.pdf", b"%PDF-1.7 a menu")
    _drop(folder, "card.jpg")

    assert _scan_twice() == 1
    assert told == [(UUID(reader.group_id), UUID(reader.household_id), ["pdf_not_supported", "unsupported_format"])]
    assert _scan_twice() == 0
    assert len(told) == 1  # nothing new: nothing more to tell

    _drop(folder, "again.txt", b"still not an image")
    _scan_twice()
    assert [reasons for _, _, reasons in told] == [["pdf_not_supported", "unsupported_format"], ["unsupported_format"]]


def test_a_burst_over_several_scans_is_told_when_its_last_file_is_taken(
    root: Path, reader: TestUser, told: list, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "INBOX_FILES_PER_TICK", 1)
    folder = _folder(root, reader)
    for name in ("a.txt", "b.txt", "c.txt"):
        _drop(folder, name, b"not an image")

    inbox.scan_once()  # seen
    inbox.scan_once()  # one taken, two left
    inbox.scan_once()  # one taken, one left
    assert told == []
    inbox.scan_once()  # the last one: the burst is over
    assert [reasons for _, _, reasons in told] == [["unsupported_format"] * 3]


def test_a_long_burst_is_told_after_two_minutes(
    root: Path, reader: TestUser, told: list, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "INBOX_FILES_PER_TICK", 1)
    clock = [1000.0]
    monkeypatch.setattr(inbox, "_monotonic", lambda: clock[0])
    folder = _folder(root, reader)
    for name in ("a.txt", "b.txt", "c.txt"):
        _drop(folder, name, b"not an image")

    inbox.scan_once()
    inbox.scan_once()  # the first refusal
    clock[0] += inbox.REFUSALS_BURST
    inbox.scan_once()  # the second, and the burst is two minutes old
    assert [reasons for _, _, reasons in told] == [["unsupported_format"] * 2]
    inbox.scan_once()
    assert [reasons for _, _, reasons in told] == [["unsupported_format"] * 2, ["unsupported_format"]]


def test_refusals_arent_told_while_paused(root: Path, reader: TestUser, told: list, monkeypatch: pytest.MonkeyPatch):
    folder = _folder(root, reader)
    _drop(folder, "a.txt", b"not an image")
    _drop(folder, "b.txt", b"not an image")
    inbox.scan_once()

    checks = [0]
    real_is_paused = storage.is_paused

    def paused_after_the_first_file() -> bool:
        checks[0] += 1
        return checks[0] > 2 or real_is_paused()  # scan_once's check, then the first file's

    monkeypatch.setattr(storage, "is_paused", paused_after_the_first_file)
    inbox.scan_once()
    assert told == []
    assert len(os.listdir(folder / "failed")) == 2  # one file and its note

    monkeypatch.setattr(storage, "is_paused", real_is_paused)
    inbox.scan_once()
    assert [reasons for _, _, reasons in told] == [["unsupported_format"] * 2]


def _status(user: TestUser, readiness: Any = None) -> inbox.InboxStatus:
    group_slug, household_slug = _slugs(user)
    return inbox.household_status(group_slug, household_slug, readiness)


def _readiness(user: TestUser) -> Any:
    with session_context() as session:
        return inbox.reading_readiness(session, UUID(user.group_id), UUID(user.household_id))


def test_the_status_counts_waiting_cards_and_says_why(root: Path, unique_user_fn_scoped: TestUser, tmp_path: Path):
    user = unique_user_fn_scoped
    folder = _folder(root, user)
    _drop(folder, "a.jpg")
    _drop(folder, "b.png", b"anything: it's taken and refused later")
    _drop(folder, "fresh.jpg", age=0)  # still being written: not yet waiting
    _drop(folder, ".hidden.jpg")
    _drop(folder, "c.jpg.part")
    card = folder / "card"
    card.mkdir()
    _drop(card, "front.jpg")
    (folder / "empty").mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(_jpeg())
    (folder / "link.jpg").symlink_to(outside)
    before = _tree(folder)

    assert _scan_twice() == 0  # the group can't read cards: they wait
    status = _status(user, _readiness(user))
    assert (status.waiting, status.waiting_reason, status.rejections) == (3, InboxWaitingReason.cannot_read, [])
    assert _status(user).waiting_reason is None  # no readiness given, no reason

    _configure(user)
    with session_context() as session:
        IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).settings.upsert(
            RecipeIngestionSettingsUpdate(local_only=True)
        )
    assert _status(user, _readiness(user)).waiting_reason == InboxWaitingReason.local_only_unavailable

    with session_context() as session:
        IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).settings.upsert(
            RecipeIngestionSettingsUpdate(local_only=False)
        )
    readable = _status(user, _readiness(user))
    assert (readable.waiting, readable.waiting_reason) == (3, None)  # the next scan takes them
    assert _tree(folder) == before  # the status wrote nothing


def test_the_status_says_quota_when_the_group_is_at_it(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    folder = _folder(root, reader)
    _drop(folder, "a.jpg")
    monkeypatch.setattr(limits, "MAX_PROCESSING_JOBS_PER_GROUP", 0)
    assert _scan_twice() == 0
    assert (_status(reader, _readiness(reader)).waiting_reason) == InboxWaitingReason.quota


def test_waiting_cards_are_counted_up_to_a_limit(root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox, "STATUS_MAX_WAITING", 2)
    folder = _folder(root, reader)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        _drop(folder, name)
    assert _status(reader).waiting == 2


def test_the_status_lists_the_newest_refusals_with_their_reasons(root: Path, reader: TestUser, told: list):
    folder = _folder(root, reader)
    photo = _jpeg()
    _drop(folder, "card.jpg", photo)
    assert _scan_twice() == 1
    _drop(folder, "card again.jpg", photo)
    _drop(folder, "notes.txt", b"not an image")
    assert _scan_twice() == 0

    failed = folder / "failed"
    old = time.time() - 3600
    os.utime(failed / "notes.txt.error.txt", (old, old))  # refused an hour before the duplicate
    status = _status(reader)
    assert [(item.name, item.reason) for item in status.rejections] == [
        ("card again.jpg", IngestRejectReason.duplicate),
        ("notes.txt", IngestRejectReason.unsupported_format),
    ]
    assert abs(status.rejections[1].at.timestamp() - old) < 2
    assert status.rejections[0].at.tzinfo is not None

    # older than the retention: no longer listed
    ancient = time.time() - 15 * 86400
    os.utime(failed / "notes.txt.error.txt", (ancient, ancient))
    os.utime(failed / "notes.txt", (ancient, ancient))
    assert [item.name for item in _status(reader).rejections] == ["card again.jpg"]


def test_a_refusal_without_a_code_is_listed_without_a_reason(root: Path, reader: TestUser, told: list):
    folder = _folder(root, reader)
    (folder / "empty").mkdir()
    _drop(folder / "empty", ".DS_Store", b"junk")  # ignored: the folder has no pages
    failed = folder / "failed"
    failed.mkdir()
    (failed / "odd.jpg").write_bytes(b"x")
    (failed / "odd.jpg.error.txt").write_text("Not added: a symbolic link, which is never followed.\n")
    (failed / "made-up.jpg").write_bytes(b"x")
    (failed / "made-up.jpg.error.txt").write_text("Not added (no_such_reason): who knows.\n")
    assert {item.name: item.reason for item in _status(reader).rejections} == {"odd.jpg": None, "made-up.jpg": None}


def test_at_most_ten_refusals_are_listed_newest_first(root: Path, reader: TestUser):
    failed = _folder(root, reader) / "failed"
    failed.mkdir()
    for number in range(12):
        (failed / f"{number:02}.jpg").write_bytes(b"x")
        note = failed / f"{number:02}.jpg.error.txt"
        note.write_text("Not added (too_large): The file is larger than 30 MB.\n")
        _age(note, 1000 - number)
    names = [item.name for item in _status(reader).rejections]
    assert names == [f"{number:02}.jpg" for number in range(11, 1, -1)]


def test_a_failed_folder_that_is_a_link_is_never_followed(root: Path, reader: TestUser, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "private.jpg").write_bytes(b"x")
    (elsewhere / "private.jpg.error.txt").write_text("Not added (duplicate): This card was already scanned.\n")
    folder = _folder(root, reader)
    (folder / "failed").symlink_to(elsewhere)
    _drop(folder, "a.jpg")

    status = _status(reader)
    assert status.rejections == []
    assert status.waiting == 1


def test_the_status_is_empty_when_the_inbox_is_off_or_the_folder_missing(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch
):
    group_slug, household_slug = _slugs(reader)
    assert inbox.household_status(group_slug, household_slug) == inbox.InboxStatus()  # not created yet
    assert inbox.household_status("../etc", household_slug) == inbox.InboxStatus()

    _drop(_folder(root, reader), "a.jpg")
    assert inbox.household_status(group_slug, household_slug).waiting == 1
    monkeypatch.setattr(inbox, "inbox_root", lambda: None)
    assert inbox.household_status(group_slug, household_slug) == inbox.InboxStatus()


def test_a_card_folder_mealie_may_not_move_is_listed_until_it_can(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch, warnings: list[str]
):
    # another user's card folder made under umask 022 (2755): moving a folder needs write access to the folder itself
    folder = _folder(root, reader)
    card = folder / "card"
    card.mkdir()
    _drop(card, "front.jpg")
    _drop(card, "back.jpg")
    _drop(folder, "single.jpg")
    real_rename = os.rename

    def rename(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if src == "card":
            raise PermissionError(errno.EACCES, "Permission denied", "card")
        real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    assert _scan_twice() == 1  # the photo is taken; the folder stays where it is
    assert inbox.scan_once() == 0
    assert sorted(path.name for path in card.iterdir()) == ["back.jpg", "front.jpg"]
    [logged] = [message for message in warnings if message.startswith("Couldn't take card ")]
    assert "write access" in logged and "umask 002" in logged

    status = _status(reader, _readiness(reader))
    assert [(item.name, item.reason) for item in status.rejections] == [("card", IngestRejectReason.no_permission)]
    assert status.rejections[0].at is not None and status.rejections[0].at.tzinfo is not None
    assert (status.waiting, status.waiting_reason) == (0, None)  # listed as stuck, not as waiting

    # once Mealie may move it, the next scan takes it and it's no longer listed
    monkeypatch.setattr(os, "rename", real_rename)
    assert inbox.scan_once() == 1
    assert not card.exists()
    assert _status(reader).rejections == []


def test_a_stuck_entry_that_is_gone_is_no_longer_listed(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch, warnings: list[str]
):
    folder = _folder(root, reader)
    photo = _drop(folder, "locked.jpg")
    real_rename = os.rename

    def rename(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if src == "locked.jpg":
            raise PermissionError(errno.EPERM, "Operation not permitted", "locked.jpg")
        real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    assert _scan_twice() == 0
    assert [item.reason for item in _status(reader).rejections] == [IngestRejectReason.no_permission]

    photo.unlink()  # whoever wrote it took it back
    assert _status(reader).rejections == []  # the status checks it's still there
    assert inbox.scan_once() == 0
    _drop(folder, "locked.jpg")  # a new one by the same name is tried afresh
    assert _status(reader).rejections == []


def test_a_burst_of_refusals_reaches_the_households_notifier_once(
    root: Path,
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
    apprise_sent: list[Notified],  # noqa: F811
):
    user = unique_user_fn_scoped
    _configure(user)
    ha = make_notifier(api_client, user, cards_ready=True)
    folder = _folder(root, user)
    _drop(folder, "notes.txt", b"not an image")
    _drop(folder, "menu.pdf", b"%PDF-1.7 a menu")

    assert _scan_twice() == 0

    [sent] = sent_to(apprise_sent, ha, "recipe_ingestion_rejected")
    assert sent.title == "Recipe cards not added"
    assert sent.received_document()["count"] == 2
    assert sent.received_document()["reasons"] == {"unsupported_format": 1, "pdf_not_supported": 1}
    assert "notes" not in sent.body and "menu" not in sent.body  # no file names


# ==========================================
# The household's language, and what the notes say


def _batch(user: TestUser, source: IngestSource, locale: str | None, *, minutes_ago: float = 0) -> UUID:
    with session_context() as session:
        return IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).batches.create(
            source=source,
            created_by=None if source == IngestSource.inbox else user.user_id,
            source_key="somewhere/else" if source == IngestSource.inbox else None,
            locale=locale,
            now=utcnow() - timedelta(minutes=minutes_ago),
        )


def test_inbox_cards_take_the_language_of_the_households_latest_app_or_api_batch(
    root: Path, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    _configure(user)
    _batch(user, IngestSource.app, "fr-FR", minutes_ago=30)
    _batch(user, IngestSource.app, "de-DE", minutes_ago=20)
    _batch(user, IngestSource.inbox, "nl-NL", minutes_ago=10)  # an inbox batch never says what the household speaks
    told: list[str | None] = []
    monkeypatch.setattr(
        inbox.events, "notify_inbox_rejections", lambda *args, locale=None, **kwargs: told.append(locale) or True
    )
    folder = _folder(root, user)
    _drop(folder, "card.jpg")
    _drop(folder, "notes.txt", b"not an image")

    assert _scan_twice() == 1
    [job] = _jobs(user)
    assert job.locale == "de-DE"
    with session_context() as session:
        batch = session.get(RecipeIngestionBatch, job.batch_id)
        assert batch is not None
        assert (batch.source, batch.locale) == ("inbox", "de-DE")  # its "ready" notification is in German
    assert told == ["de-DE"]
    # German has no text for the note yet: it falls back to English, never to the key
    assert (folder / "failed" / "notes.txt.error.txt").read_text().startswith("Not added (unsupported_format): ")

    _batch(user, IngestSource.api, "pt-BR")  # e.g. a Shortcut on a phone set to Portuguese
    _drop(folder, "next.jpg")
    assert _scan_twice() == 1
    assert _jobs(user)[-1].locale == "pt-BR"


def test_inbox_cards_are_en_us_when_the_household_never_said(root: Path, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    _configure(user)
    _batch(user, IngestSource.app, None)
    _drop(_folder(root, user), "card.jpg")
    assert _scan_twice() == 1
    assert [job.locale for job in _jobs(user)] == ["en-US"]


def test_the_note_is_in_the_households_language(
    root: Path, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    _configure(user)
    _batch(user, IngestSource.app, "de-DE")
    asked: list[str | None] = []
    real = inbox.translator_for

    def translator_for(locale: str | None) -> Any:
        asked.append(locale)
        return real(locale)

    monkeypatch.setattr(inbox, "translator_for", translator_for)
    _drop(_folder(root, user), "notes.txt", b"not an image")
    _scan_twice()
    assert asked and set(asked) == {"de-DE"}


def test_every_refusal_reason_has_a_note(root: Path):
    translator = translator_for("en-US")
    notes = {reason: inbox.rejection_note(translator, reason) for reason in IngestRejectReason}
    for reason, note in notes.items():
        assert note.startswith(f"Not added ({reason.value}): ")
        assert "recipe-ingest" not in note and "{" not in note
    assert len(set(notes.values())) == len(notes)


def test_the_notes_take_their_numbers_from_the_limits(
    root: Path, reader: TestUser, monkeypatch: pytest.MonkeyPatch, told: list
):
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 10 * limits.MIB)
    monkeypatch.setattr(limits, "MAX_PAGES_PER_CARD", 3)
    folder = _folder(root, reader)
    _drop(folder, "huge.jpg", b"\xff\xd8\xff" + b"\0" * (10 * limits.MIB))
    stack = folder / "stack"
    stack.mkdir()
    for n in range(4):
        _drop(stack, f"{n}.jpg", _jpeg((8, 8)))

    assert _scan_twice() == 0
    assert (folder / "failed" / "huge.jpg.error.txt").read_text() == (
        "Not added (too_large): The file is larger than 10 MB.\n"
    )
    assert "at most 3 pages" in (folder / "failed" / "stack.error.txt").read_text()
    # a refused card folder is listed like a refused file
    assert {item.name: item.reason for item in _status(reader).rejections} == {
        "huge.jpg": IngestRejectReason.too_large,
        "stack": IngestRejectReason.too_many_pages,
    }

    translator = translator_for("en-US")
    monkeypatch.setattr(limits, "MAX_PIXELS", 50_000_000)
    pixels = inbox.rejection_note(translator, IngestRejectReason.too_many_pixels)
    assert "50 megapixels" in pixels
    assert f"{images.MAX_JPEG_SOURCE_PIXELS // 1_000_000} megapixels" in pixels


def test_a_duplicate_note_names_the_earlier_card(root: Path, reader: TestUser, told: list):
    folder = _folder(root, reader)
    photo = _jpeg()
    _drop(folder, "card.jpg", photo)
    assert _scan_twice() == 1
    earlier = _jobs(reader)[-1].id
    _drop(folder, "card again.jpg", photo)
    assert _scan_twice() == 0

    note = (folder / "failed" / "card again.jpg.error.txt").read_text()
    assert note.startswith("Not added (duplicate): ")
    assert str(earlier) in note
    assert _status(reader).rejections[0].reason == IngestRejectReason.duplicate


def test_a_refusal_without_a_code_has_a_note_from_the_texts(
    root: Path, reader: TestUser, told: list, monkeypatch: pytest.MonkeyPatch
):
    folder = _folder(root, reader)
    card = folder / "emptied"
    card.mkdir()
    _drop(card, "front.jpg")
    real = inbox._page_entries

    def emptied_once_claimed(dir_fd: int, label: str | None = None) -> list[tuple[str, os.stat_result]]:
        # the scan saw the folder's page (with a label); it's gone when the claimed folder is opened (none)
        return real(dir_fd, label) if label is not None else []

    monkeypatch.setattr(inbox, "_page_entries", emptied_once_claimed)
    assert _scan_twice() == 0
    assert (folder / "failed" / "emptied.error.txt").read_text() == "Not added: The folder has no pages.\n"
    assert _status(reader).rejections[0].reason is None


@pytest.mark.parametrize(
    "note, reason",
    [
        ("Not added (too_large): The file is larger than 30 MB.\n", IngestRejectReason.too_large),
        ("Nicht hinzugefügt (too_large): Die Datei ist zu groß.\n", IngestRejectReason.too_large),
        ("(duplicate) 追加されませんでした: (page 2)\n", IngestRejectReason.duplicate),
        ("追加されませんでした（too_many_pages）：\n", IngestRejectReason.too_many_pages),
        ("Pas ajouté : (page 2) (unsupported_format).\n", IngestRejectReason.unsupported_format),
        ("Not added: It isn't a regular file.\n", None),
        ("Not added (no_such_reason): who knows.\n", None),
        ("Not added\n(too_large): on the second line\n", None),
    ],
)
def test_a_notes_reason_is_read_in_any_language(root: Path, reader: TestUser, note: str, reason: Any):
    failed = _folder(root, reader) / "failed"
    failed.mkdir()
    (failed / "card.jpg").write_bytes(b"x")
    (failed / "card.jpg.error.txt").write_text(note)
    [rejection] = _status(reader).rejections
    assert rejection.reason == reason
