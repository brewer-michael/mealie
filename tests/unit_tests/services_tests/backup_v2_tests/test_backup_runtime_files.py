"""
Fork: backups leave out the files of the running instance (recipe card ingestion's lock, pause marker, dispatcher
heartbeat and kept results, and the migration lock), and a restore leaves the live ones alone (docs/ai/PHASE2.md §17).
"""

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

import pytest

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.db import migration_lock
from mealie.services.ai.ingest import storage
from mealie.services.backups_v2.backup_v2 import BackupV2

RUNTIME_FILES = [
    ".ai-ingest-lock",
    ".ai-ingest-lock.restore",
    ".ai-ingest-paused",
    ".ai-ingest-dispatcher",
    ".mealie-migrate.lock",
]


@pytest.fixture()
def backup_v2() -> Iterator[BackupV2]:
    backup = BackupV2(get_app_settings().DB_URL)
    try:
        yield backup
    finally:
        backup.db_exporter.engine.dispose()


@pytest.fixture()
def runtime_files() -> Iterator[dict[Path, bytes]]:
    """The runtime files and a kept result, as a running instance has them, with their contents"""
    data_dir = get_app_dirs().DATA_DIR
    results = data_dir / ".ai-ingest-results"
    results.mkdir(exist_ok=True)
    files = {data_dir / name: f"{name} {uuid.uuid4()}".encode() for name in RUNTIME_FILES}
    files[data_dir / f".ai-ingest-paused.{uuid.uuid4().hex}.1.tmp"] = b"1759574400.000"
    files[results / f"{uuid.uuid4()}.extract.json"] = b'{"kept": true}'
    for path, content in files.items():
        path.write_bytes(content)
    try:
        yield files
    finally:
        for path in files:
            if path.name not in {
                storage.lock_path().name,
                storage.restore_lock_path().name,
                migration_lock.LOCK_FILE_NAME,
            }:
                path.unlink(missing_ok=True)
        shutil.rmtree(results, ignore_errors=True)


def test_the_names_are_the_ones_the_instance_uses():
    assert storage.lock_path().name in BackupV2.RUNTIME_FILES
    assert storage.restore_lock_path().name in BackupV2.RUNTIME_FILES
    assert storage.pause_marker_path().name in BackupV2.RUNTIME_FILES
    assert storage.dispatcher_seen_path().name in BackupV2.RUNTIME_FILES
    assert migration_lock.lock_path().name in BackupV2.RUNTIME_FILES


def test_a_backup_leaves_out_runtime_files(backup_v2: BackupV2, runtime_files: dict[Path, bytes]):
    data_dir = get_app_dirs().DATA_DIR
    kept = data_dir / "groups" / f"runtime-test-{uuid.uuid4().hex}.txt"
    kept.parent.mkdir(exist_ok=True)
    kept.write_text("group data")
    try:
        backup = backup_v2.backup()
    finally:
        kept.unlink()

    try:
        with ZipFile(backup) as zip_file:
            names = set(zip_file.namelist())
    finally:
        backup.unlink()

    assert f"data/groups/{kept.name}" in names
    for path in runtime_files:
        assert f"data/{path.relative_to(data_dir)}" not in names
    assert not any(name.startswith("data/.ai-ingest-results") for name in names)


def test_a_restore_leaves_runtime_files_alone(backup_v2: BackupV2, runtime_files: dict[Path, bytes]):
    data_dir = get_app_dirs().DATA_DIR
    backup = backup_v2.backup()
    try:
        # an older backup, from before runtime files were left out
        with ZipFile(backup, "a") as zip_file:
            zip_file.writestr("data/.ai-ingest-results/stale.extract.json", '{"stale": true}')
            zip_file.writestr("data/.ai-ingest-dispatcher", "1700000000")
            zip_file.writestr("data/.mealie-migrate.lock", "")

        backup_v2.restore(backup)
    finally:
        backup.unlink()

    assert not (data_dir / ".ai-ingest-results" / "stale.extract.json").exists()
    for path, content in runtime_files.items():
        if path.name == storage.PAUSE_MARKER_NAME:
            continue  # the restore's own pause wrote and removed it
        assert path.read_bytes() == content, path.name
