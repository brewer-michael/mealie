"""
What every recipe card route shares (docs/ai/PHASE2.md §14): the error body, the pause and the on/off switch, and a
translation for every code, kind and reason the pages show.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from mealie.lang import providers
from mealie.lang.locale_config import LOCALE_CONFIG
from mealie.lang.providers import get_locale_provider
from mealie.routes.ai.ingest._deps import (
    ingest_error,
    paused_error,
    require_enabled,
    require_not_paused,
    write_section,
)
from mealie.schema.recipe_ingest import CardFlagKind, IngestErrorCode, IngestRejectReason
from mealie.services.ai.ingest import limits, storage
from mealie.services.ai.ingest.i18n import translator_for, with_fallback
from mealie.services.ai.ingest.settings import IngestSettings, get_ingest_settings

ROOT = Path(__file__).parents[5]
FRONTEND_MESSAGES = ROOT / "frontend" / "app" / "lang" / "messages" / "en-US.json"
BACKEND_MESSAGES = ROOT / "mealie" / "lang" / "messages" / "en-US.json"


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(storage, "_data_dir", lambda: tmp_path)
    return tmp_path


def _frontend() -> dict:
    return json.loads(FRONTEND_MESSAGES.read_text())["recipe-ingest"]


def test_every_error_code_flag_kind_and_rejection_has_frontend_text():
    messages = _frontend()
    for code in [*IngestErrorCode, "version_conflict", "busy", "unresolved_flags", "paused_for_restore"]:
        assert messages["error"].get(code), code
    for kind in CardFlagKind:
        assert messages["flag"][kind.value]["title"], kind
        assert any(key.startswith("explanation") for key in messages["flag"][kind.value]), kind
    for reason in IngestRejectReason:
        assert messages["reject"].get(reason.value), reason
    for step in ("orienting", "reading-card", "reading-card-ocr", "cross-reading", "structuring"):
        assert messages["progress"].get(step), step
    for step in ("linking-ingredients", "suggesting-organizers"):
        assert messages["progress"].get(step), step


def test_backend_texts_are_translated():
    t = get_locale_provider("en-US").t
    assert json.loads(BACKEND_MESSAGES.read_text())["recipe-ingest"]
    assert t("recipe-ingest.upload-summary", count=1) == "1 recipe card queued. You'll be notified when it's ready."
    assert t("recipe-ingest.upload-summary", count=3).startswith("3 recipe cards queued")
    assert t("recipe-ingest.notification-ready", count=0) == "No cards are ready to review"
    assert t("recipe-ingest.notification-ready", count=10) == "10 cards are ready to review"
    assert t("recipe-ingest.unreadable") == "(unreadable)"
    assert t("recipe-ingest.errors.too-large", max=100) == "The upload is too large. The limit is 100 MB."


def test_error_bodies_carry_a_code_and_only_sometimes_a_message():
    handled = ingest_error(409, "version_conflict", current=4)
    assert handled.status_code == 409
    assert handled.detail == {"code": "version_conflict", "current": 4}

    shown = ingest_error(
        413,
        "too_large",
        message_key="recipe-ingest.errors.too-large",
        message_params={"max": 100},
        translator=get_locale_provider("en-US"),
    )
    assert shown.detail == {"code": "too_large", "message": "The upload is too large. The limit is 100 MB."}


@pytest.mark.parametrize("locale", ["de-DE", "en-GB", "fr-FR"])
def test_messages_fall_back_to_english_where_the_language_lacks_them(locale: str):
    """Only en-US carries the fork's texts: another language gets them in English, never as their keys"""
    key = "recipe-ingest.errors.paused-for-restore"
    english = get_locale_provider("en-US").t(key)
    translator = get_locale_provider(locale)
    expected = english if translator.t(key) == key else translator.t(key)

    assert ingest_error(503, "paused_for_restore", message_key=key, translator=translator).detail["message"] == expected
    assert paused_error(translator).detail["message"] == expected
    assert translator_for(locale).t("recipe-ingest.unreadable") != "recipe-ingest.unreadable"
    assert with_fallback(translator_for(locale)).t(key) == expected

    # no translator: the request's language, as the locale middleware set it
    token = providers._locale_context.set((translator, LOCALE_CONFIG["en-US"]))
    try:
        assert paused_error().detail["message"] == expected
    finally:
        providers._locale_context.reset(token)


def test_the_switch_and_the_pause_answer_503(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    require_enabled()
    require_not_paused()

    monkeypatch.setattr(
        "mealie.routes.ai.ingest._deps.get_ingest_settings", lambda: IngestSettings(ENABLED=False, WORKER=False)
    )
    with pytest.raises(HTTPException) as e:
        require_enabled()
    assert (e.value.status_code, e.value.detail["code"]) == (503, "ingest_disabled")
    assert e.value.detail["message"]

    (data_dir / storage.PAUSE_MARKER_NAME).write_text(str(time.time()))
    with pytest.raises(HTTPException) as e:
        require_not_paused()
    assert e.value.status_code == 503
    assert e.value.detail["code"] == "paused_for_restore"
    assert e.value.headers == {"Retry-After": str(limits.PAUSED_RETRY_AFTER)}
    assert paused_error().detail["message"]


def test_a_write_section_turns_the_pause_into_503(data_dir: Path):
    with write_section():
        pass

    (data_dir / storage.PAUSE_MARKER_NAME).write_text(str(time.time()))
    with pytest.raises(HTTPException) as e, write_section():
        pass
    assert e.value.status_code == 503
    assert e.value.headers == {"Retry-After": "60"}


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_INGEST_CONCURRENCY", "3")
    monkeypatch.setenv("AI_INGEST_MAX_UPLOAD_MB", "20")
    monkeypatch.setenv("AI_INGEST_ENABLED", "false")
    settings = IngestSettings()
    assert (settings.CONCURRENCY, settings.ENABLED) == (3, False)
    assert settings.max_upload_bytes == 20 * 1024 * 1024
    # the dispatcher doesn't start under TESTING
    assert get_ingest_settings().WORKER is False
    assert get_ingest_settings().INBOX_DIR is None


def test_the_inbox_may_not_be_inside_the_data_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mealie.core.config import get_app_dirs
    from mealie.services.ai.ingest import settings as ingest_settings

    def inbox(path: Path | None) -> Path | None:
        monkeypatch.setattr(ingest_settings, "get_ingest_settings", lambda: IngestSettings(INBOX_DIR=path))
        ingest_settings.inbox_root.cache_clear()
        try:
            return ingest_settings.inbox_root()
        finally:
            ingest_settings.inbox_root.cache_clear()

    assert inbox(None) is None
    assert inbox(tmp_path / "inbox") == (tmp_path / "inbox").resolve()
    assert inbox(get_app_dirs().DATA_DIR / "inbox") is None
    assert inbox(Path("/app/inbox")) is None
    assert inbox(Path("/")) is None
