"""
The inbox folder (docs/ai/PHASE2.md §1.3): per-household folders, settling, the rename claim, opening each file once
without following links, `processed/` and `failed/`, retrying stale claims, crash recovery through the content hash,
and the pause. Runs on SQLite and PostgreSQL.
"""

import io
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from PIL import Image

from mealie.core.config import get_app_dirs
from mealie.db.db_setup import session_context
from mealie.db.models.group import Group
from mealie.db.models.household import Household
from mealie.db.models.recipe_ingest import RecipeIngestionBatch, RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate
from mealie.schema.recipe_ingest import PageMeta, RecipeIngestionSettingsUpdate
from mealie.services import ocr
from mealie.services.ai.ingest import inbox, limits, storage
from mealie.services.ai.ingest import settings as ingest_settings
from mealie.services.ai.ingest.settings import IngestSettings
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
