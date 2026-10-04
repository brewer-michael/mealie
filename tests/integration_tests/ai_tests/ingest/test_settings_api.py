"""
`GET`/`PUT /api/ai/ingest/settings` (docs/ai/PHASE2.md §9, §10, §14): what every member reads (the privacy chip's
reader, whether a card can be kept local, OCR, limits, the inbox folder), the local readiness managers get, and the
managers-only upsert.
"""

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionSettings
from mealie.routes.ai.ingest import about
from mealie.routes.ai.ingest import settings as settings_routes
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate
from mealie.schema.recipe.recipe_category import TagSave
from mealie.services import ocr
from mealie.services.ai import local
from mealie.services.ai.errors import AIProviderLimitReachedError
from mealie.services.ai.ingest import images, inbox, limits, storage
from mealie.services.ai.ingest.settings import get_ingest_settings
from mealie.services.ai.runtime import AIRuntime
from tests.integration_tests.ai_tests.ingest.test_jobs_api import household_member
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser

SETTINGS = "/api/ai/ingest/settings"

LOCAL_URL = "http://127.0.0.1:11434/v1"
PUBLIC_URL = "http://8.8.8.8/v1"
"""IP literals: deciding whether they're private needs no DNS"""


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the group can do mustn't depend on this machine having Tesseract"""
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    local.clear_address_cache()


@pytest.fixture(autouse=True)
def no_reader_seen() -> Iterator[Path]:
    """No dispatcher presence file, as on a server with no reader; what another test's dispatcher left is put back"""
    path = storage.dispatcher_seen_path()
    seen = path.stat().st_mtime if path.exists() else None
    path.unlink(missing_ok=True)
    yield path
    path.unlink(missing_ok=True)
    if seen is not None:
        path.touch()
        os.utime(path, (seen, seen))


def providers(
    user: TestUser,
    *,
    default: dict[str, Any] | None = None,
    image: dict[str, Any] | None = None,
    extra: list[dict[str, Any]] | None = None,
) -> None:
    """Sets the group's default and image providers (each `{name, base_url?, runs_locally?}`), plus unused extras"""
    repos = user.repos

    def create(spec: dict[str, Any]) -> UUID:
        return repos.group_ai_providers.create(AIProviderCreate(model="m", api_key="k", **spec)).id

    for spec in extra or []:
        create(spec)
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(
            default_provider_id=create(default) if default else None,
            image_provider_id=create(image) if image else None,
            audio_provider_id=None,
        ),
    )


def get_settings(api_client: TestClient, user: TestUser) -> dict[str, Any]:
    response = api_client.get(SETTINGS, headers=user.token)
    assert response.status_code == 200, response.text
    return response.json()


def settings_rows(user: TestUser) -> int:
    with session_context() as session:
        return session.execute(
            sa.select(sa.func.count())
            .select_from(RecipeIngestionSettings)
            .where(RecipeIngestionSettings.group_id == UUID(user.group_id))
        ).scalar_one()


# ==================================================================================================================
# GET


def test_a_group_without_ai_cant_read_cards(api_client: TestClient, unique_user_fn_scoped: TestUser):
    settings = get_settings(api_client, unique_user_fn_scoped)

    assert settings == {
        "enabled": True,
        "localOnly": False,
        "crossRead": False,
        "canReadCards": False,
        "limitReached": False,
        "limitedFeatures": [],
        "baseUrlSet": False,
        "readerRunning": False,
        "ocrAvailable": False,
        "reader": None,
        "localOnlyAvailable": False,
        "localReadiness": {"image": [], "default": [], "fast": [], "notPrivate": []},
        "limits": {
            "maxUploadBytes": 100 * limits.MIB,
            "maxFileBytes": limits.MAX_FILE_BYTES,
            "maxImagesPerRequest": limits.MAX_IMAGES_PER_REQUEST,
            "maxPagesPerCard": limits.MAX_PAGES_PER_CARD,
            "maxPixels": limits.MAX_PIXELS,
            "maxJpegPixels": images.MAX_JPEG_SOURCE_PIXELS,
        },
        "inbox": {"enabled": False, "folder": None, "waiting": 0, "waitingReason": None, "rejections": []},
    }


def test_a_cloud_reader(api_client: TestClient, unique_user_fn_scoped: TestUser):
    providers(unique_user_fn_scoped, default={"name": "Claude Sonnet"}, image={"name": "Gemini Flash"})
    settings = get_settings(api_client, unique_user_fn_scoped)

    assert settings["canReadCards"] is True
    assert settings["reader"] == {"name": "Gemini Flash", "local": False, "viaOcr": False}
    assert settings["localOnlyAvailable"] is False


def test_ocr_then_a_text_provider(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    providers(unique_user_fn_scoped, default={"name": "Claude Sonnet"})
    assert get_settings(api_client, unique_user_fn_scoped)["canReadCards"] is False  # no image provider, no OCR

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    settings = get_settings(api_client, unique_user_fn_scoped)
    assert settings["canReadCards"] is True
    assert settings["ocrAvailable"] is True
    assert settings["reader"] == {"name": "Claude Sonnet", "local": False, "viaOcr": True}


def test_local_providers(api_client: TestClient, unique_user_fn_scoped: TestUser):
    providers(
        unique_user_fn_scoped,
        default={"name": "Qwen text", "base_url": LOCAL_URL, "runs_locally": True},
        image={"name": "Qwen VL", "base_url": LOCAL_URL, "runs_locally": True},
        extra=[
            {"name": "LAN proxy", "base_url": PUBLIC_URL, "runs_locally": True},  # marked local, but public
            {"name": "Unmarked", "base_url": LOCAL_URL},
        ],
    )
    settings = get_settings(api_client, unique_user_fn_scoped)

    assert settings["canReadCards"] is True
    assert settings["localOnlyAvailable"] is True
    assert settings["reader"] == {"name": "Qwen VL", "local": True, "viaOcr": False}
    assert settings["localReadiness"] == {
        "image": ["Qwen VL"],
        "default": ["Qwen text"],
        "fast": ["Qwen text"],
        "notPrivate": ["LAN proxy"],
    }


def test_the_reader_follows_the_groups_policy(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """A local-only group's chip names the local reader, never the cloud provider ahead of it"""
    user = unique_user_fn_scoped
    providers(
        user,
        default={"name": "Qwen text", "base_url": LOCAL_URL, "runs_locally": True},
        image={"name": "Gemini Flash"},
    )
    settings = get_settings(api_client, user)
    assert settings["reader"] == {"name": "Gemini Flash", "local": False, "viaOcr": False}
    assert settings["localOnlyAvailable"] is False  # no local image provider and no OCR

    assert api_client.put(SETTINGS, json={"localOnly": True}, headers=user.token).status_code == 200
    settings = get_settings(api_client, user)
    assert settings["localOnly"] is True
    assert settings["reader"] is None  # nothing local can read the photos
    assert settings["canReadCards"] is True


def test_a_provider_over_its_monthly_limit_still_counts_as_able_to_read(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """The upload's rule: the card is accepted, and fails `limit_reached` when it's read if the limit still applies"""
    providers(unique_user_fn_scoped, default={"name": "Claude Sonnet"}, image={"name": "Gemini Flash"})

    def over_the_limit(self: AIRuntime, slot: Any) -> list:
        raise AIProviderLimitReachedError("over the limit")

    monkeypatch.setattr(AIRuntime, "candidates", over_the_limit)
    monkeypatch.setattr(ocr, "is_available", lambda: False)  # nothing stands in for the image slot
    settings = get_settings(api_client, unique_user_fn_scoped)
    assert settings["canReadCards"] is True
    assert settings["limitReached"] is True  # the capture page warns before anything is uploaded
    # the reader is still named (LO4): the limit is reported on its own, not as "AI isn't set up"
    assert settings["reader"] == {"name": "Gemini Flash", "local": False, "viaOcr": False}


@pytest.mark.parametrize(
    ("over", "ocr_available", "limit_reached"),
    [
        (set(), False, False),
        ({"image"}, False, True),
        ({"image"}, True, False),  # OCR reads the photo, and the default slot builds the recipe
        ({"default"}, True, True),  # every card needs the default slot
    ],
)
def test_limit_reached_says_whether_a_card_read_now_would_fail(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    over: set[str],
    ocr_available: bool,
    limit_reached: bool,
):
    providers(unique_user_fn_scoped, default={"name": "Claude Sonnet"}, image={"name": "Gemini Flash"})
    monkeypatch.setattr(ocr, "is_available", lambda: ocr_available)
    real = AIRuntime.candidates

    def some_over_the_limit(self: AIRuntime, slot: Any) -> list:
        if slot.value in over:
            raise AIProviderLimitReachedError("over the limit")
        return real(self, slot)

    monkeypatch.setattr(AIRuntime, "candidates", some_over_the_limit)
    settings = get_settings(api_client, unique_user_fn_scoped)
    assert (settings["canReadCards"], settings["limitReached"]) == (True, limit_reached)


def test_limited_features_name_what_a_monthly_limit_skips(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Cards are still read, but the fast slot's providers are over their limit: no tag suggestions until it resets"""
    user = unique_user_fn_scoped
    providers(user, default={"name": "Claude Sonnet"}, image={"name": "Gemini Flash"})
    user.repos.tags.create(TagSave(name="Dessert", group_id=user.repos.group_id))
    real = AIRuntime.candidates

    def fast_over_the_limit(self: AIRuntime, slot: Any) -> list:
        if slot.value == "fast":
            raise AIProviderLimitReachedError("over the limit")
        return real(self, slot)

    assert get_settings(api_client, user)["limitedFeatures"] == []
    monkeypatch.setattr(AIRuntime, "candidates", fast_over_the_limit)
    settings = get_settings(api_client, user)
    assert (settings["canReadCards"], settings["limitReached"]) == (True, False)
    assert settings["limitedFeatures"] == ["suggestions"]


@pytest.mark.parametrize(
    ("base_url", "base_url_set"),
    [
        ("http://localhost:8080", False),  # the default
        ("http://localhost:9925", False),
        ("http://mealie.localhost", False),
        ("http://127.0.0.1:9000", False),
        ("http://127.1.2.3", False),
        ("http://[::1]:9000", False),
        ("http://0.0.0.0:9000", False),
        ("", False),
        ("http://192.168.1.20:9925", True),
        ("http://mealie.local:9925", True),
        ("https://recipes.example.com", True),
    ],
)
def test_base_url_set_says_whether_links_open_on_a_phone(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    base_url_set: bool,
):
    monkeypatch.setattr(get_app_settings(), "BASE_URL", base_url)
    assert get_settings(api_client, unique_user_fn_scoped)["baseUrlSet"] is base_url_set


def test_reader_running_says_whether_cards_are_read(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, no_reader_seen: Path
):
    user = unique_user_fn_scoped

    def running() -> bool:
        return get_settings(api_client, user)["readerRunning"]

    assert running() is False  # no dispatcher ever ran (AI_INGEST_WORKER=false everywhere)
    storage.mark_dispatcher_seen()  # what a running dispatcher does every minute
    assert running() is True

    stale = time.time() - about.READER_SEEN_WITHIN - 5  # three marks missed: the reader stopped
    os.utime(no_reader_seen, (stale, stale))
    assert running() is False

    storage.mark_dispatcher_seen()
    monkeypatch.setattr(get_ingest_settings(), "ENABLED", False)
    assert running() is False


def test_the_inbox_says_what_waits_and_what_it_refused(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    monkeypatch.setattr(settings_routes, "inbox_root", lambda: tmp_path)
    monkeypatch.setattr(inbox, "inbox_root", lambda: tmp_path)
    group = api_client.get(api_routes.groups_self, headers=user.token).json()
    household = api_client.get(api_routes.households_self, headers=user.token).json()
    folder = tmp_path / group["slug"] / household["slug"]
    (folder / "failed").mkdir(parents=True)
    settled = time.time() - limits.INBOX_SETTLE - 60
    for path, data in [
        (folder / "card.jpg", b"waiting"),
        (folder / "failed" / "notes.txt", b"not a photo"),
        (folder / "failed" / "notes.txt.error.txt", b"Not added (unsupported_format): not a photo.\n"),
    ]:
        path.write_bytes(data)
        os.utime(path, (settled, settled))

    # the group can't read cards, so the photo waits, and says why
    info = get_settings(api_client, user)["inbox"]
    assert {key: info[key] for key in ("enabled", "folder", "waiting", "waitingReason")} == {
        "enabled": True,
        "folder": f"{group['slug']}/{household['slug']}",
        "waiting": 1,
        "waitingReason": "cannot_read",
    }
    assert [(item["name"], item["reason"]) for item in info["rejections"]] == [("notes.txt", "unsupported_format")]
    assert abs(_timestamp(info["rejections"][0]["at"]) - settled) < 2

    providers(user, default={"name": "Claude Sonnet"}, image={"name": "Gemini Flash"})
    info = get_settings(api_client, user)["inbox"]
    assert (info["waiting"], info["waitingReason"]) == (1, None)  # the next scan takes it


def _timestamp(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value).timestamp()


def test_a_member_gets_the_reader_but_not_the_readiness(
    api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    providers(
        user,
        default={"name": "Qwen text", "base_url": LOCAL_URL, "runs_locally": True},
        image={"name": "Qwen VL", "base_url": LOCAL_URL, "runs_locally": True},
    )
    member = household_member(api_client, admin_token, user)

    settings = get_settings(api_client, member)
    assert settings["canReadCards"] is True
    assert settings["reader"] == {"name": "Qwen VL", "local": True, "viaOcr": False}
    assert settings["localOnlyAvailable"] is True
    assert settings["localReadiness"] is None


def test_the_households_inbox_folder(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(settings_routes, "inbox_root", lambda: tmp_path)
    user = unique_user_fn_scoped
    group = api_client.get(api_routes.groups_self, headers=user.token).json()
    household = api_client.get(api_routes.households_self, headers=user.token).json()

    assert get_settings(api_client, user)["inbox"] == {
        "enabled": True,
        "folder": f"{group['slug']}/{household['slug']}",
        "waiting": 0,
        "waitingReason": None,
        "rejections": [],
    }


def test_with_ingestion_turned_off(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The app asks on every load, so the GET still answers (no 503 toast); the PUT is refused"""
    providers(unique_user_fn_scoped, default={"name": "Claude Sonnet"}, image={"name": "Gemini Flash"})
    monkeypatch.setattr(get_ingest_settings(), "ENABLED", False)
    monkeypatch.setattr(settings_routes, "inbox_root", lambda: tmp_path)

    settings = get_settings(api_client, unique_user_fn_scoped)
    assert settings["enabled"] is False  # the settings card says so, rather than asking for a provider
    assert settings["canReadCards"] is False
    assert settings["reader"] is None
    assert settings["inbox"] == {
        "enabled": False,
        "folder": None,
        "waiting": 0,
        "waitingReason": None,
        "rejections": [],
    }

    response = api_client.put(SETTINGS, json={"localOnly": True}, headers=unique_user_fn_scoped.token)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ingest_disabled"
    assert settings_rows(unique_user_fn_scoped) == 0


def test_the_work_runs_off_the_event_loop(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Provider settings and the address lookups that decide what's local never block the event loop"""
    providers(unique_user_fn_scoped, default={"name": "Claude Sonnet"}, image={"name": "Gemini Flash"})
    loops: list[bool] = []
    readiness, reader = settings_routes.reading_readiness, settings_routes.card_reader

    def on_a_loop() -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def checked_readiness(*args: Any, **kwargs: Any) -> Any:
        loops.append(on_a_loop())
        return readiness(*args, **kwargs)

    def checked_reader(*args: Any, **kwargs: Any) -> Any:
        loops.append(on_a_loop())
        return reader(*args, **kwargs)

    monkeypatch.setattr(settings_routes, "reading_readiness", checked_readiness)
    monkeypatch.setattr(settings_routes, "card_reader", checked_reader)
    get_settings(api_client, unique_user_fn_scoped)
    assert api_client.put(SETTINGS, json={"crossRead": True}, headers=unique_user_fn_scoped.token).status_code == 200

    assert loops == [False, False, False, False]


# ==================================================================================================================
# PUT


def test_a_manager_upserts_the_settings(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    assert settings_rows(user) == 0

    response = api_client.put(SETTINGS, json={"localOnly": True, "crossRead": False}, headers=user.token)
    assert response.status_code == 200
    assert response.json()["localOnly"] is True
    assert response.json()["localReadiness"] is not None  # the full settings, as a manager sees them
    assert settings_rows(user) == 1

    response = api_client.put(SETTINGS, json={"localOnly": False, "crossRead": True}, headers=user.token)
    assert response.status_code == 200
    assert (response.json()["localOnly"], response.json()["crossRead"]) == (False, True)
    assert settings_rows(user) == 1
    assert (get_settings(api_client, user)["localOnly"], get_settings(api_client, user)["crossRead"]) == (False, True)

    # left out means the default, as the stored row would
    assert api_client.put(SETTINGS, json={"localOnly": True}, headers=user.token).json()["crossRead"] is False


def test_only_managers_change_them(api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser):
    member = household_member(api_client, admin_token, unique_user_fn_scoped)

    response = api_client.put(SETTINGS, json={"localOnly": True}, headers=member.token)
    assert response.status_code == 403
    assert settings_rows(unique_user_fn_scoped) == 0

    manager = household_member(api_client, admin_token, unique_user_fn_scoped, canManage=True)
    assert api_client.put(SETTINGS, json={"localOnly": True}, headers=manager.token).status_code == 200
    # the group's settings: every member sees them
    assert get_settings(api_client, member)["localOnly"] is True


def test_unknown_fields_and_login(api_client: TestClient, unique_user_fn_scoped: TestUser):
    response = api_client.put(
        SETTINGS, json={"localOnly": True, "canReadCards": True}, headers=unique_user_fn_scoped.token
    )
    assert response.status_code == 422

    api_client.cookies.clear()  # a login in an earlier test leaves its session cookie
    assert api_client.get(SETTINGS).status_code == 401
    assert api_client.put(SETTINGS, json={"localOnly": True}).status_code == 401


def test_groups_are_separate(api_client: TestClient, unique_user: TestUser, g2_user: TestUser):
    assert api_client.put(SETTINGS, json={"crossRead": True}, headers=unique_user.token).status_code == 200
    assert get_settings(api_client, unique_user)["crossRead"] is True
    assert get_settings(api_client, g2_user)["crossRead"] is False
