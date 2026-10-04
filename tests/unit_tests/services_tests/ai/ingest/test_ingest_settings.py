"""
Recipe card ingestion's environment settings (docs/ai/PHASE2.md §15): each `AI_INGEST_*` setting's default and
validation, empty variables counting as unset, and the inbox turning itself on when a folder is mounted at `/inbox`.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from mealie.core.config import get_app_dirs
from mealie.services.ai.ingest import settings as ingest_settings
from mealie.services.ai.ingest.settings import IngestSettings

NEW_VARIABLES = (
    "INBOX_PROCESSED_DAYS",
    "INBOX_DIR_MODE",
    "ORIENT",
    "LOCK_DIR",
    "GROUP_CONCURRENCY",
    "MAX_PROCESSING_PER_USER",
    "URL_FETCH",
    "URL_ALLOW_HOSTS",
    "URL_TIMEOUT",
    "PDF_UNCONFINED",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("INBOX_DIR", *NEW_VARIABLES):
        monkeypatch.delenv(f"AI_INGEST_{name}", raising=False)


def test_the_defaults():
    settings = IngestSettings()
    assert settings.ORIENT is True
    assert settings.INBOX_PROCESSED_DAYS is None
    assert settings.INBOX_DIR_MODE == "2775"
    assert settings.inbox_dir_mode == 0o2775
    assert settings.LOCK_DIR is None
    assert settings.GROUP_CONCURRENCY == 0
    assert settings.MAX_PROCESSING_PER_USER == 0
    assert settings.URL_FETCH is False  # image URLs are refused unless switched on
    assert settings.url_allow_hosts == []
    assert settings.URL_TIMEOUT == 20
    assert settings.PDF_UNCONFINED is False  # PDFs aren't rendered where the renderer can't be confined


def test_the_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AI_INGEST_ORIENT", "false")
    monkeypatch.setenv("AI_INGEST_INBOX_PROCESSED_DAYS", "30")
    monkeypatch.setenv("AI_INGEST_INBOX_DIR_MODE", "755")
    monkeypatch.setenv("AI_INGEST_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("AI_INGEST_GROUP_CONCURRENCY", "2")
    monkeypatch.setenv("AI_INGEST_MAX_PROCESSING_PER_USER", "25")
    monkeypatch.setenv("AI_INGEST_URL_FETCH", "true")
    monkeypatch.setenv("AI_INGEST_URL_ALLOW_HOSTS", " homeassistant.local, 192.168.1.0/24 ,,")
    monkeypatch.setenv("AI_INGEST_URL_TIMEOUT", "45")
    monkeypatch.setenv("AI_INGEST_PDF_UNCONFINED", "true")
    settings = IngestSettings()
    assert settings.ORIENT is False
    assert settings.INBOX_PROCESSED_DAYS == 30
    assert settings.inbox_dir_mode == 0o755
    assert settings.LOCK_DIR == tmp_path
    assert settings.GROUP_CONCURRENCY == 2
    assert settings.MAX_PROCESSING_PER_USER == 25
    assert settings.URL_FETCH is True
    assert settings.url_allow_hosts == ["homeassistant.local", "192.168.1.0/24"]
    assert settings.URL_TIMEOUT == 45
    assert settings.PDF_UNCONFINED is True


@pytest.mark.parametrize("name", ["INBOX_DIR", *NEW_VARIABLES, "CONCURRENCY", "RETENTION_DAYS"])
def test_an_empty_variable_is_unset(name: str, monkeypatch: pytest.MonkeyPatch):
    # Unraid and compose files pass a variable nobody filled in as an empty string
    monkeypatch.setenv(f"AI_INGEST_{name}", "")
    assert getattr(IngestSettings(), name) == IngestSettings.model_fields[name].get_default(call_default_factory=True)


@pytest.mark.parametrize("name", ["INBOX_DIR", "LOCK_DIR", "INBOX_PROCESSED_DAYS"])
def test_a_blank_value_is_unset(name: str):
    assert getattr(IngestSettings(**{name: "  "}), name) is None


@pytest.mark.parametrize(
    "values",
    [
        {"INBOX_PROCESSED_DAYS": 0},
        {"INBOX_PROCESSED_DAYS": -3},
        {"GROUP_CONCURRENCY": -1},
        {"MAX_PROCESSING_PER_USER": -1},
        {"URL_TIMEOUT": 0},
        {"URL_TIMEOUT": 301},
    ],
)
def test_numbers_below_their_minimum_are_refused(values: dict):
    with pytest.raises(ValidationError):
        IngestSettings(**values)


@pytest.mark.parametrize(
    ("given", "mode"),
    [
        ("2775", 0o2775),
        ("02775", 0o2775),
        ("0o2770", 0o2770),
        ("755", 0o755),
        (" 775 ", 0o775),
        ("0", 0),
        ("7777", 0o7777),
    ],
)
def test_the_folder_mode_is_octal(given: str, mode: int):
    settings = IngestSettings(INBOX_DIR_MODE=given)
    assert settings.inbox_dir_mode == mode
    assert settings.INBOX_DIR_MODE == format(mode, "o")


@pytest.fixture()
def errors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    logged: list[str] = []
    monkeypatch.setattr(ingest_settings.logger, "error", logged.append)
    return logged


@pytest.mark.parametrize("given", ["8", "778", "17777", "rwxrwxr-x", "-1", "0x1ff", "2775.0", "8888"])
def test_a_folder_mode_that_isnt_octal_is_logged_and_the_default_used(given: str, errors: list[str]):
    # a mistyped mode never stops Mealie from starting
    settings = IngestSettings(INBOX_DIR_MODE=given)
    assert settings.inbox_dir_mode == 0o2775
    assert len(errors) == 1
    assert f"AI_INGEST_INBOX_DIR_MODE is {given!r}" in errors[0]
    assert "2775" in errors[0]


def test_a_folder_mode_from_the_environment_that_isnt_octal_still_starts(
    monkeypatch: pytest.MonkeyPatch, errors: list[str]
):
    monkeypatch.setenv("AI_INGEST_INBOX_DIR_MODE", "8888")
    ingest_settings.get_ingest_settings.cache_clear()
    try:
        assert ingest_settings.get_ingest_settings().inbox_dir_mode == 0o2775
    finally:
        ingest_settings.get_ingest_settings.cache_clear()
    assert len(errors) == 1 and "'8888'" in errors[0]


@pytest.mark.parametrize("given", ["", "  ", "0o"])
def test_a_blank_folder_mode_is_the_default(given: str, errors: list[str]):
    assert IngestSettings(INBOX_DIR_MODE=given).inbox_dir_mode == 0o2775
    assert errors == []


# ==========================================
# /inbox turns itself on when a folder is mounted there


@pytest.fixture()
def mount_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A stand-in for `/inbox`, which nothing has mounted yet; `inbox_root` sees no `AI_INGEST_INBOX_DIR`"""
    path = tmp_path / "inbox"
    path.mkdir()
    monkeypatch.setattr(ingest_settings, "MOUNTED_INBOX", path)
    monkeypatch.setattr(ingest_settings, "get_ingest_settings", lambda: IngestSettings(INBOX_DIR=None))
    ingest_settings.inbox_root.cache_clear()
    yield path
    ingest_settings.inbox_root.cache_clear()


def _mounted(monkeypatch: pytest.MonkeyPatch, *paths: Path) -> None:
    real_ismount = os.path.ismount
    monkeypatch.setattr(
        os.path,
        "ismount",
        lambda path: Path(path) in paths or real_ismount(path),  # type: ignore[arg-type]
    )


def test_a_mounted_inbox_turns_on_without_the_variable(mount_point: Path, monkeypatch: pytest.MonkeyPatch):
    _mounted(monkeypatch, mount_point)
    logged: list[str] = []
    monkeypatch.setattr(ingest_settings.logger, "info", logged.append)

    assert ingest_settings.inbox_root() == mount_point.resolve()
    assert ingest_settings.inbox_root() == mount_point.resolve()
    assert logged == [f"Recipe card inbox on at {mount_point} (a mounted folder)"]


def test_a_plain_inbox_folder_isnt_used(mount_point: Path, monkeypatch: pytest.MonkeyPatch):
    # an /inbox nobody mounted is just a folder inside the container: photos put there would be lost with it
    logged: list[str] = []
    monkeypatch.setattr(ingest_settings.logger, "info", logged.append)
    assert not os.path.ismount(mount_point)
    assert ingest_settings.inbox_root() is None
    assert logged == []


def test_a_missing_inbox_isnt_used(mount_point: Path, monkeypatch: pytest.MonkeyPatch):
    _mounted(monkeypatch, mount_point)
    mount_point.rmdir()
    assert ingest_settings.inbox_root() is None


def test_a_mounted_file_isnt_an_inbox(mount_point: Path, monkeypatch: pytest.MonkeyPatch):
    _mounted(monkeypatch, mount_point)
    mount_point.rmdir()
    mount_point.write_text("not a folder")
    assert ingest_settings.inbox_root() is None


def test_the_variable_wins_over_a_mounted_inbox(mount_point: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _mounted(monkeypatch, mount_point)
    chosen = tmp_path / "elsewhere"
    monkeypatch.setattr(ingest_settings, "get_ingest_settings", lambda: IngestSettings(INBOX_DIR=chosen))
    assert ingest_settings.inbox_root() == chosen.resolve()


def test_a_mounted_inbox_inside_the_data_directory_is_refused(monkeypatch: pytest.MonkeyPatch, mount_point: Path):
    inside = get_app_dirs().DATA_DIR / "inbox-mount-test"
    inside.mkdir(exist_ok=True)
    try:
        monkeypatch.setattr(ingest_settings, "MOUNTED_INBOX", inside)
        _mounted(monkeypatch, inside)
        warned: list[str] = []
        monkeypatch.setattr(ingest_settings.logger, "warning", warned.append)
        assert ingest_settings.inbox_root() is None
        assert len(warned) == 1
        assert warned[0].startswith(f"The folder mounted at {inside} overlaps")
    finally:
        inside.rmdir()


def test_the_templates_inbox_with_no_folder_mapped_is_off(mount_point: Path, monkeypatch: pytest.MonkeyPatch):
    # the Unraid template fills in AI_INGEST_INBOX_DIR=/inbox; a user who maps no folder there gets no inbox (and the
    # app doesn't claim one), with a line in the log
    mount_point.rmdir()
    monkeypatch.setattr(ingest_settings, "get_ingest_settings", lambda: IngestSettings(INBOX_DIR=mount_point))
    warned: list[str] = []
    monkeypatch.setattr(ingest_settings.logger, "warning", warned.append)
    assert ingest_settings.inbox_root() is None
    assert warned == [
        f"AI_INGEST_INBOX_DIR is {mount_point}, but no folder is mapped there: the recipe card inbox is off"
    ]

    # a folder there, mounted or not, is the administrator's choice
    mount_point.mkdir()
    ingest_settings.inbox_root.cache_clear()
    assert ingest_settings.inbox_root() == mount_point.resolve()
