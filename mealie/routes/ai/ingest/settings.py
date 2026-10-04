"""
`GET`/`PUT /api/ai/ingest/settings` (docs/ai/PHASE2.md §9, §10, §14): the group's recipe card settings, plus what
every member's capture page needs to know.

- **Everyone** reads them: whether the group can read cards (the sidebar entry and the Create-menu item), the
  `reader` for the privacy chip, whether a card can be kept local (`localOnlyAvailable`), OCR, the upload limits and
  the household's inbox folder. Group managers also get `localReadiness`, the local providers per slot.
- **Group managers** (`checks.can_manage()`) change `localOnly` and `crossRead`; the row is upserted (no row means
  the defaults).

`canReadCards` and `localOnlyAvailable` follow exactly the rule the upload applies (`intake.reading_readiness`), so
the app never offers what an upload would refuse: a provider over its monthly token limit still counts as able to
read, and the card fails `limit_reached` when it's read if the limit still applies. `limitReached` says so up front,
from the same lookups, so the capture page can warn before anything is uploaded. The privacy chip's `reader` is the
first provider the card's read would try under the group's policy.

What the cards page and the settings card warn about comes with them: `limitedFeatures` (optional parts of the read
a monthly limit skips, from the same readiness), `baseUrlSet` (whether notification links open on a phone),
`readerRunning` (whether any process reads cards: `about.reader_running`), and the household's inbox: photos waiting
in its folder and why, and the photos it refused lately (`inbox.household_status`).

The handlers are synchronous, so FastAPI runs them in its threadpool: the provider settings, the address lookups that
decide what's local (`getaddrinfo`), the inbox folder and every query stay off the event loop. With
`AI_INGEST_ENABLED` off the `GET` still answers, with `enabled: false` and `canReadCards: false`, because the app asks
on every load; the `PUT` answers 503.
"""

import ipaddress
from urllib.parse import urlsplit
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter
from sqlalchemy.orm import Session

from mealie.core.config import get_app_settings
from mealie.db.models.group import Group
from mealie.db.models.household import Household
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.routes._base import controller
from mealie.schema.recipe_ingest import (
    IngestInboxInfo,
    IngestLimits,
    RecipeIngestionSettingsOut,
    RecipeIngestionSettingsUpdate,
)
from mealie.services import ocr
from mealie.services.ai.ingest import images, inbox, limits
from mealie.services.ai.ingest.intake import ReadingReadiness, reading_readiness
from mealie.services.ai.ingest.settings import get_ingest_settings, inbox_root
from mealie.services.ai.local import card_reader, local_readiness

from ._deps import IngestController, require_enabled
from .about import reader_running

router = APIRouter(prefix="/ai/ingest", tags=["AI: Recipe Cards"])


def _folder_name(slug: str | None) -> bool:
    """Whether a slug works as one folder name (the inbox skips households whose slugs don't)"""
    if not slug:
        return False
    return "/" not in slug and "\\" not in slug and not slug.startswith(".")


def _inbox(session: Session, group_id: UUID, household_id: UUID, readiness: ReadingReadiness | None) -> IngestInboxInfo:
    """
    Whether the inbox is on, the household's folder in it (`<group-slug>/<household-slug>`), the photos waiting there
    and why (by the group's `readiness`), and the photos it refused lately
    """
    if not get_ingest_settings().ENABLED or inbox_root() is None:
        return IngestInboxInfo(enabled=False)

    group_slug = session.execute(sa.select(Group.slug).where(Group.id == group_id)).scalar_one_or_none()
    household_slug = session.execute(sa.select(Household.slug).where(Household.id == household_id)).scalar_one_or_none()
    if not (_folder_name(group_slug) and _folder_name(household_slug)):
        return IngestInboxInfo(enabled=True)
    status = inbox.household_status(group_slug, household_slug, readiness)
    return IngestInboxInfo(
        enabled=True,
        folder=f"{group_slug}/{household_slug}",
        waiting=status.waiting,
        waiting_reason=status.waiting_reason,
        rejections=status.rejections,
    )


def _limits() -> IngestLimits:
    return IngestLimits(
        max_upload_bytes=get_ingest_settings().max_upload_bytes,
        max_file_bytes=limits.MAX_FILE_BYTES,
        max_images_per_request=limits.MAX_IMAGES_PER_REQUEST,
        max_pages_per_card=limits.MAX_PAGES_PER_CARD,
        max_pixels=limits.MAX_PIXELS,
        max_jpeg_pixels=images.MAX_JPEG_SOURCE_PIXELS,
    )


def base_url_set() -> bool:
    """
    Whether `BASE_URL`, which notification links are built from, is an address a phone can open: not the default, and
    not the server's own (`localhost`, a loopback address, `0.0.0.0`)
    """
    settings = get_app_settings()
    if settings.is_default_base_url:
        return False
    try:
        host = (urlsplit(settings.BASE_URL.strip()).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a name: whatever it resolves to, it's meant to be reached
    return not (address.is_loopback or address.is_unspecified)


def settings_out(session: Session, group_id: UUID, household_id: UUID, *, manager: bool) -> RecipeIngestionSettingsOut:
    """
    The group's card settings as a member of `household_id` sees them; `localReadiness` only for a manager. Blocking
    (provider settings, address lookups, queries): call it from a worker thread.
    """
    from mealie.services.openai import OpenAIService

    enabled = get_ingest_settings().ENABLED
    stored = IngestRepos(session, group_id, household_id).settings.get()
    readiness = reading_readiness(session, group_id, household_id) if enabled else None
    settings = RecipeIngestionSettingsOut(
        enabled=enabled,
        local_only=stored.local_only,
        cross_read=stored.cross_read,
        base_url_set=base_url_set(),
        reader_running=enabled and reader_running(),
        ocr_available=ocr.is_available(),
        limits=_limits(),
        inbox=_inbox(session, group_id, household_id, readiness),
    )

    if readiness is not None:
        settings.can_read_cards = readiness.can_read
        settings.local_only_available = readiness.local_ready
        settings.limit_reached = readiness.limit_reached
        settings.limited_features = list(readiness.limited_features)

        service = OpenAIService(get_repositories(session, group_id=group_id, household_id=household_id))
        settings.reader = card_reader(service, local_only=stored.local_only)
        if manager:
            settings.local_readiness = local_readiness(service)

    if session.in_transaction():
        session.commit()  # read-only: the connection goes back to the pool from this thread
    return settings


@controller(router)
class RecipeIngestSettingsController(IngestController):
    @router.get("/settings", response_model=RecipeIngestionSettingsOut)
    def get_settings(self) -> RecipeIngestionSettingsOut:
        """The group's recipe card settings; every member reads them, `localReadiness` is for group managers"""
        return settings_out(self.session, self.group_id, self.household_id, manager=self.user.can_manage)

    @router.put("/settings", response_model=RecipeIngestionSettingsOut)
    def update_settings(self, data: RecipeIngestionSettingsUpdate) -> RecipeIngestionSettingsOut:
        """Keep cards on this server, and read every card twice (group managers)"""
        self.checks.can_manage()
        require_enabled(self.translator)

        self.ingest_repos.settings.upsert(data)
        return settings_out(self.session, self.group_id, self.household_id, manager=True)
