"""
Committing a reviewed card as a recipe (docs/ai/PHASE2.md §7): crash-safe and idempotent, with a server-owned recipe id
and asset token persisted before anything is created, and resumed by the dispatcher when a commit stalls.

The whole commit, from the claim to the finish, runs inside the ingest write lock (§3.9):

1. **Check** (in the request): `ready`, the version matches, no unresolved error (422 `unresolved_flags`).
2. **Claim**, committed on its own: `ready → committing` with `commit_recipe_id` and `commit_asset_token` kept if
   already set (`COALESCE`), `committed_by`, `commit_started_at` (the commit's lease), and the task cleared, which
   cancels a pending re-read or re-extract. Every check of step 1 is repeated in its `WHERE`.
3. **A recipe with that id exists?** Skip to step 6: `create_one` commits more than once, so a partial create leaves a
   row, and creating it again would only clash.
4. **Files** into `recipes/<id>/`, once a page turn a stop left half done is settled (`review.settle_turns`): each
   `page.jpg` as `assets/recipe-card-<token>-<n>.jpg` when the card photo is attached (`attaches_card_photo`: the
   draft's switch, else not in a household whose recipes are public), and the front's `view.jpg` as the cover when the
   draft asks for it, a portrait card letterboxed to 4:3 (`cover_image`).
5. **Build and create** (`draft_to_recipe`, then `RecipeService.create_one`): ingredients re-linked through a fresh
   `IngestMatcher` (§5), organizers looked up in the group by id (or created by name, for a committer who can
   organize), the kept markers converted, the attribution as a
   note titled "From", the card assets when attached, and settings from the household with `show_assets` on when
   they are.
6. **Cover key:** `update_image(slug)` when the cover was written, and upstream's `is_ocr_recipe` set.
7. **Finish:** `committing → committed` with `recipe_id`; whichever call wins it publishes `recipe_created`, holding
   `recipe_event_claimed_at` from the finish, and records `recipe_event_sent_at` once it went out. Housekeeping sends
   it for a committed card whose event wasn't recorded a minute on (`resend_recipe_events`), so it's sent at least
   once, even when the process stops between the finish and the send.

A validation error before `create_one` returns the job to `ready` with `commit_invalid` (and removes `recipes/<id>`
when no recipe has that id). Once `create_one` has been called the job never goes back to `ready`: any failure leaves
it `committing`, and the next request for it or the dispatcher's housekeeping resumes it at step 3 once its lease
(`COMMIT_LEASE`) has passed.

**The lease fences every write after the claim.** `commit_started_at` is set by the claim, by each takeover and by
the renewal after the files, and the caller keeps the value it set: the renewal, the return to `ready` (and the
removal of `recipes/<id>` with it) and the finish all match that value. A committer that stalled past its lease and
was taken over therefore can't undo the new owner's commit or delete its files; it answers 409 `invalid_status`.

`recipe_created` is published after the write lock is released: notifiers and webhooks may take a while, and a
restore waits for that lock.
"""

import io
import re
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from fastapi import BackgroundTasks, status
from PIL import Image, ImageStat
from pydantic import ValidationError
from slugify import slugify
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.core.root_logger import get_logger
from mealie.db.db_setup import session_context
from mealie.db.models._model_utils.guid import GUID
from mealie.db.models.recipe.ingredient import IngredientFoodModel, IngredientUnitModel
from mealie.db.models.recipe.recipe import RecipeModel
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.lang.providers import Translator
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_factory import AllRepositories
from mealie.repos.repository_recipe_ingest import TASK_CLEARED, IngestQueue, IngestRepos, utcnow
from mealie.schema.household.household import HouseholdInDB
from mealie.schema.recipe.recipe import Recipe, RecipeCategory, RecipeTag, RecipeTool
from mealie.schema.recipe.recipe_asset import RecipeAsset
from mealie.schema.recipe.recipe_category import CategorySave, TagSave
from mealie.schema.recipe.recipe_image_types import RecipeImageTypes
from mealie.schema.recipe.recipe_ingredient import (
    IngredientFood,
    IngredientUnit,
    RecipeIngredient,
    SaveIngredientFood,
    SaveIngredientUnit,
)
from mealie.schema.recipe.recipe_notes import RecipeNote
from mealie.schema.recipe.recipe_settings import RecipeSettings
from mealie.schema.recipe.recipe_step import RecipeStep
from mealie.schema.recipe.recipe_tool import RecipeToolSave
from mealie.schema.recipe_ingest import (
    BulkCommitOut,
    BulkCommitRequest,
    BulkCommitSkipped,
    BulkCommitted,
    CardDraft,
    CardDraftIngredient,
    CardDraftRef,
    CardDraftUpdate,
    CommitOut,
    CommitRequest,
    IngestErrorCode,
    IngestStatus,
    PageMeta,
    RecipeIngestionJobState,
    UncommitRequest,
    UnresolvedFlagsDetail,
)
from mealie.schema.user.user import DEFAULT_INTEGRATION_ID, PrivateUser
from mealie.services import urls
from mealie.services.ai.errors import IngestPaused
from mealie.services.ai.ingest import images, limits, storage
from mealie.services.ai.ingest.i18n import translator_for, with_fallback
from mealie.services.ai.ingest.matching import IngestMatcher
from mealie.services.ai.ingest.review import (
    COMMIT_INVALID,
    FORBIDDEN,
    INVALID_STATUS,
    NOT_CLEAN,
    NOT_FOUND,
    PAUSED_FOR_RESTORE,
    PURGED,
    RECIPE_EDITED,
    UNCOMMIT_GRACE,
    UNRESOLVED_FLAGS,
    VERSION_CONFLICT,
    JobActionError,
    ReviewService,
    attaches_card_photo,
    invalid_status,
    is_slimmed,
    not_found,
    parse_flags,
    parse_pages,
    settle_turns,
    version_conflict,
)
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.event_types import EventOperation, EventRecipeData, EventTypes
from mealie.services.recipe.recipe_data_service import RecipeDataService
from mealie.services.recipe.recipe_service import RecipeService

logger = get_logger(__name__)

Job = RecipeIngestionJob

COMMIT_INTERRUPTED = IngestErrorCode.commit_interrupted.value
"""409: the commit couldn't go on (the committer is gone, or a file couldn't be written); the job is `ready` again"""

ASSET_ICON = "mdi-file-image"
BLANK_TEXT = "___"
"""What a `[blank]` the reviewer kept becomes in the recipe"""

_MARKER = re.compile(r"\[\s*(illegible|blank)\s*\]", re.IGNORECASE)

COVER_ASPECT = 4 / 3
"""A portrait card's cover is letterboxed to this landscape shape, so the recipe header doesn't crop off its top"""
COVER_BORDER = 0.02
"""The strip along each edge of the page whose average colour fills the letterbox"""
COVER_BACKGROUND = (0xF5, 0xF5, 0xF5)
"""The letterbox colour when the page's border can't be measured"""
COVER_QUALITY = 90

RECIPE_EVENT_GRACE = timedelta(seconds=60)
"""How long after a commit's finish housekeeping leaves `recipe_created` to the call that won it"""
RECIPE_EVENT_LEASE = timedelta(minutes=5)
"""How long a sender holds `recipe_event_claimed_at` before another may send the event again"""
RECIPE_EVENT_CUTOFF = timedelta(hours=24)
"""Older commits never get a late `recipe_created`, so a restored backup doesn't replay old events"""
RECIPE_EVENT_BATCH = 50
"""The most late `recipe_created` events one housekeeping run sends"""

# ==========================================
# Names and conversions


def asset_file_name(token: str, index: int) -> str:
    """The card asset of page `index` (0 is the front): `recipe-card-<token>-<n>.jpg`, `n` counting from 1"""
    return f"recipe-card-{token}-{index + 1}.jpg"


def asset_name(translator: Translator, index: int, page_count: int) -> str:
    """ "Recipe card", "Recipe card (back)" for the second of two pages, else "Recipe card (page n)" """
    if index == 0:
        return translator.t("recipe-ingest.asset-card")
    if index == 1 and page_count == 2:
        return translator.t("recipe-ingest.asset-card-back")
    return translator.t("recipe-ingest.asset-card-page", number=index + 1)


def convert_markers(text: str | None, unreadable: str) -> str:
    """
    The markers left at commit, which the reviewer kept as written (§4.6): `[blank]` becomes `___` and
    `[illegible]` the translated "(unreadable)". Unresolved markers never reach here: they're errors that block commit.
    """
    if not text:
        return ""
    return _MARKER.sub(lambda match: BLANK_TEXT if match.group(1).lower() == "blank" else unreadable, text)


def _lead_note(name: str, note: str) -> str:
    """A food name kept as text leads the line's note, e.g. "flour, sifted" or "coconut oil (melted)" """
    if not note:
        return name
    if note.startswith(","):
        return f"{name}{note}"
    if note.startswith("("):
        return f"{name} {note}"
    return f"{name}, {note}"


class DraftInvalid(Exception):
    """The draft doesn't validate into a recipe; `fields` says where"""

    def __init__(self, fields: Sequence[str]) -> None:
        super().__init__(", ".join(fields))
        self.fields = list(fields)

    @classmethod
    def of(cls, error: ValidationError) -> DraftInvalid:
        fields = sorted({".".join(str(part) for part in item["loc"]) or "draft" for item in error.errors()})
        return cls(fields)


# ==========================================
# Ingredients (§5, at commit)


class IngredientLinker:
    """
    Links a draft's foods and units to the group's again at commit, with a fresh matcher: an id that isn't one of the
    group's becomes a name, and names are matched exactly (name, plural or alias), so foods and units created since
    extraction, or by a commit that crashed, are found rather than created twice. A missing unit is created; a missing
    food only when the committer can organize, otherwise its name is kept as text. A food or unit another commit
    created since the matcher was loaded (the name is unique per group) is looked up again rather than failing.
    """

    def __init__(self, repos: AllRepositories, group_id: UUID, *, can_create_foods: bool) -> None:
        self.repos = repos
        self.group_id = group_id
        self.can_create_foods = can_create_foods
        self.matcher = IngestMatcher(repos)
        self._new_units: dict[str, IngredientUnit] = {}
        self._new_foods: dict[str, IngredientFood] = {}

    def unit(self, ref: CardDraftRef | None) -> IngredientUnit | None:
        if ref is None:
            return None
        if (unit := self.matcher.unit_by_id(ref.id)) is not None:
            return unit
        name = ref.name.strip()
        if not name:
            return None
        if (unit := self.matcher.exact_unit(name)) is not None:
            return unit

        key = IngredientUnitModel.normalize(name)
        if key not in self._new_units:
            try:
                self._new_units[key] = self.repos.ingredient_units.create(
                    SaveIngredientUnit(name=name, group_id=self.group_id)
                )
            except IntegrityError:
                # `create` has rolled back: another commit in the group created it just now
                if (unit := self._reloaded().exact_unit(name)) is None:
                    raise
                self._new_units[key] = unit
        return self._new_units[key]

    def food(self, ref: CardDraftRef | None) -> tuple[IngredientFood | None, str | None]:
        """The linked food, or None and the name to keep as text"""
        if ref is None:
            return None, None
        if (food := self.matcher.food_by_id(ref.id)) is not None:
            return food, None
        name = ref.name.strip()
        if not name:
            return None, None
        if (food := self.matcher.exact_food(name)) is not None:
            return food, None
        if not self.can_create_foods:
            return None, name

        key = IngredientFoodModel.normalize(name)
        if key not in self._new_foods:
            try:
                self._new_foods[key] = self.repos.ingredient_foods.create(
                    SaveIngredientFood(name=name, group_id=self.group_id)
                )
            except IntegrityError:
                # `create` has rolled back: another commit in the group created it just now
                if (food := self._reloaded().exact_food(name)) is None:
                    raise
                self._new_foods[key] = food
        return self._new_foods[key], None

    def _reloaded(self) -> IngestMatcher:
        self.matcher = IngestMatcher(self.repos)
        return self.matcher


def _has_marker(ref: CardDraftRef | None) -> bool:
    return ref is not None and _MARKER.search(ref.name) is not None


def _ingredient(
    line: CardDraftIngredient, linker: IngredientLinker | None, convert: Callable[[str | None], str]
) -> RecipeIngredient | None:
    original = convert(line.original_text).strip()
    title = convert(line.title).strip()
    note = convert(line.note).strip()

    # A unit or food name holding a marker the reviewer kept is never linked or created (it would become one of the
    # group's units or foods): the names stay as text at the head of the note, the unit's first, as the line reads.
    as_text: list[str] = []
    unit: IngredientUnit | None = None
    food: IngredientFood | None = None
    if line.unit is not None and _has_marker(line.unit):
        as_text.append(line.unit.name.strip())
    elif linker:
        unit = linker.unit(line.unit)

    food_name = line.food.name.strip() if line.food else ""
    if food_name and (as_text or _has_marker(line.food)):
        as_text.append(food_name)
    elif linker:
        food, kept_as_text = linker.food(line.food)
        if kept_as_text:
            as_text.append(kept_as_text)
    if as_text:
        note = _lead_note(convert(" ".join(as_text)), note)

    quantity = line.quantity
    if unit is None and food is None and not note and original:
        # nothing but (perhaps) a number was parsed: the line keeps its text as the card has it
        quantity, note = None, original

    if not (note or food or unit or quantity or title):
        return None
    return RecipeIngredient(
        reference_id=line.reference_id,
        title=title or None,
        original_text=original or None,
        quantity=quantity,
        unit=unit,
        food=food,
        note=note,
        display="",  # recomputed from the linked fields
    )


# ==========================================
# The recipe


def recipe_settings(household: HouseholdInDB, *, show_assets: bool = True) -> RecipeSettings:
    """
    The household's defaults for new recipes, as `create_one` would use them, but with the card assets shown when
    they're attached (`show_assets`); otherwise the household's own default
    """
    preferences = household.preferences
    if preferences is None:
        return RecipeSettings(show_assets=True) if show_assets else RecipeSettings()
    return RecipeSettings(
        public=preferences.recipe_public,
        show_nutrition=preferences.recipe_show_nutrition,
        show_assets=show_assets or preferences.recipe_show_assets,
        landscape_view=preferences.recipe_landscape_view,
        disable_comments=preferences.recipe_disable_comments,
    )


class OrganizerMaker:
    """
    Creates the tags, categories and tools a draft names without an id, for a committer who can organize (§7), like
    the foods: found by slug first (an earlier attempt at this commit, or anyone, may have made it), and looked up
    again when another commit creates the same one at the same moment (the slug is unique per group)
    """

    def __init__(self, repos: AllRepositories, group_id: UUID) -> None:
        self.repos = repos
        self.group_id = group_id

    def _find_or_create(self, repo: Any, save: Any, name: str) -> Any | None:
        name = name.strip()
        slug = slugify(name)
        if not slug or _MARKER.search(name):
            return None
        if (found := repo.get_one(slug, "slug")) is not None:
            return found
        try:
            return repo.create(save)
        except IntegrityError:
            # `create` has rolled back: another commit in the group created it just now
            return repo.get_one(slug, "slug")

    def tag(self, name: str) -> Any | None:
        return self._find_or_create(self.repos.tags, TagSave(name=name.strip(), group_id=self.group_id), name)

    def category(self, name: str) -> Any | None:
        return self._find_or_create(
            self.repos.categories, CategorySave(name=name.strip(), group_id=self.group_id), name
        )

    def tool(self, name: str) -> Any | None:
        return self._find_or_create(self.repos.tools, RecipeToolSave(name=name.strip(), group_id=self.group_id), name)


def _organizers[T: RecipeTag](
    refs: Sequence[CardDraftRef],
    lookup: Callable[[UUID], Any],
    build: Callable[[Any], T],
    kind: str,
    warnings: list[str],
    create: Callable[[str], Any] | None = None,
) -> list[T]:
    """
    The group's organizers by id; one named without an id is created when `create` is given (the committer can
    organize). Unknown or foreign ids, and names nobody may create, are dropped with a warning.
    """
    found: list[T] = []
    seen: set[UUID] = set()
    for ref in refs:
        if ref.id:
            organizer = lookup(ref.id)
        else:
            organizer = create(ref.name) if create and ref.name.strip() else None
        if organizer is None:
            warnings.append(f"{kind}_dropped:{ref.name or ref.id}")
            continue
        if organizer.id not in seen:
            seen.add(organizer.id)
            found.append(build(organizer))
    return found


def draft_to_recipe(
    draft: CardDraft,
    *,
    recipe_id: UUID,
    token: str,
    page_count: int,
    repos: AllRepositories,
    settings: RecipeSettings,
    translator: Translator,
    unreadable: str,
    linker: IngredientLinker | None,
    organizers: OrganizerMaker | None = None,
) -> tuple[Recipe, list[str]]:
    """
    The recipe a draft becomes (§7 step 5), and warnings about what was left out. Only the draft's own fields are
    used; the id, slug, assets, settings and owners are the server's. `page_count` card assets are listed (0 when the
    card photo isn't attached). Without a `linker` no food or unit is linked (or created): a dry run that only
    validates. Organizers named without an id are created only with `organizers`.

    Raises `DraftInvalid`.
    """

    def convert(text: str | None) -> str:
        return convert_markers(text, unreadable)

    name = convert(draft.name).strip()
    if not name:
        raise DraftInvalid(["name"])

    try:
        return _build_recipe(
            draft, name, recipe_id, token, page_count, repos, settings, translator, convert, linker, organizers
        )
    except ValidationError as e:
        raise DraftInvalid.of(e) from e


def _attribution_text(attribution: str, title: str) -> str:
    """
    The attribution as the note titled `title` ("From") holds it: a card's own leading "From" (or the title's word),
    with or without a colon, isn't repeated, so "From Grandma Jo" reads "From: Grandma Jo", not "From: From Grandma Jo"
    """
    words = sorted({"from", title.strip().lower()} - {""}, key=len, reverse=True)
    leading = re.compile(rf"^(?:{'|'.join(map(re.escape, words))})(?:\s*:\s*|\s+|$)", re.IGNORECASE)
    return leading.sub("", attribution.strip(), count=1).strip()


def _build_recipe(
    draft: CardDraft,
    name: str,
    recipe_id: UUID,
    token: str,
    page_count: int,
    repos: AllRepositories,
    settings: RecipeSettings,
    translator: Translator,
    convert: Callable[[str | None], str],
    linker: IngredientLinker | None,
    organizers: OrganizerMaker | None,
) -> tuple[Recipe, list[str]]:
    warnings: list[str] = []
    ingredients = [ing for line in draft.ingredients if (ing := _ingredient(line, linker, convert)) is not None]

    steps: list[RecipeStep] = []
    for step in draft.steps:
        text, title = convert(step.text).strip(), convert(step.title).strip()
        if text or title:
            # fresh, but the same for every attempt at this commit
            steps.append(RecipeStep(id=uuid5(recipe_id, f"step:{step.id}"), title=title, text=text))

    notes: list[RecipeNote] = []
    note_from = translator.t("recipe-ingest.note-from")
    if attribution := _attribution_text(convert(draft.attribution), note_from):
        notes.append(RecipeNote(title=note_from, text=attribution))
    for note in draft.notes:
        title, text = convert(note.title).strip(), convert(note.text).strip()
        if title or text:
            notes.append(RecipeNote(title=title, text=text))

    tags = _organizers(
        draft.tags,
        repos.tags.get_one,
        lambda t: RecipeTag(id=t.id, group_id=t.group_id, name=t.name, slug=t.slug),
        "tag",
        warnings,
        organizers.tag if organizers else None,
    )
    categories = _organizers(
        draft.categories,
        repos.categories.get_one,
        lambda c: RecipeCategory(id=c.id, group_id=c.group_id, name=c.name, slug=c.slug),
        "category",
        warnings,
        organizers.category if organizers else None,
    )
    tools = _organizers(
        draft.tools,
        repos.tools.get_one,
        lambda t: RecipeTool(
            id=t.id, group_id=t.group_id, name=t.name, slug=t.slug, households_with_tool=t.households_with_tool
        ),
        "tool",
        warnings,
        organizers.tool if organizers else None,
    )

    assets = [
        RecipeAsset(
            name=asset_name(translator, index, page_count), icon=ASSET_ICON, file_name=asset_file_name(token, index)
        )
        for index in range(page_count)
    ]

    recipe = Recipe(
        id=recipe_id,
        slug="",
        name=name,
        description=convert(draft.description).strip(),
        recipe_yield=convert(draft.recipe_yield).strip() or None,
        recipe_yield_quantity=draft.recipe_yield_quantity or 0,
        recipe_servings=draft.recipe_servings or 0,
        prep_time=convert(draft.prep_time).strip() or None,
        perform_time=convert(draft.perform_time).strip() or None,
        total_time=convert(draft.total_time).strip() or None,
        recipe_ingredient=ingredients,
        recipe_instructions=steps,
        notes=notes,
        tags=tags,
        recipe_category=categories,
        tools=tools,
        assets=assets,
        settings=settings,
    )
    return recipe, warnings


# ==========================================
# The steps


def _rowcount(result: Any) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


def _update(session: Session, stmt: sa.Update) -> bool:
    """Runs a conditional update and commits it; whether it matched the job"""
    try:
        updated = _rowcount(session.execute(stmt, execution_options={"synchronize_session": False})) == 1
    except BaseException:
        session.rollback()
        raise
    session.commit()
    return updated


def _lease_fence(job_id: UUID, lease: datetime) -> list[sa.ColumnElement[bool]]:
    """Still `committing`, under the lease this caller set (`commit_started_at`): the fence of every later write"""
    return [Job.id == job_id, Job.status == IngestStatus.committing.value, Job.commit_started_at == lease]


def _claim(
    session: Session,
    job_id: UUID,
    household_id: UUID,
    user_id: UUID,
    version: int,
    now: datetime,
    *,
    require_clean: bool = False,
) -> bool:
    """
    Step 2: `ready → committing`, with the recipe id and asset token kept from an earlier attempt or made now. The
    commit's lease is `now`. `require_clean` (a bulk commit) also needs no unresolved warning.
    """
    clean = [Job.warning_count == 0] if require_clean else []
    stmt = (
        sa.update(Job)
        .where(
            Job.id == job_id,
            Job.household_id == household_id,
            Job.status == IngestStatus.ready.value,
            Job.draft_version == version,
            Job.error_count == 0,
            *clean,
        )
        .values(
            **TASK_CLEARED,
            status=IngestStatus.committing.value,
            commit_recipe_id=sa.func.coalesce(Job.commit_recipe_id, sa.literal(uuid4(), GUID())),
            commit_asset_token=sa.func.coalesce(
                Job.commit_asset_token, sa.literal(secrets.token_urlsafe(16), sa.String(32))
            ),
            committed_by=user_id,
            commit_started_at=now,
            error_code=None,
            error_params=None,
            row_version=Job.row_version + 1,
        )
    )
    return _update(session, stmt)


def _win_lease(session: Session, job_id: UUID, now: datetime, *, lease: datetime | None = None) -> datetime | None:
    """
    Takes over a commit whose lease had passed by `now` (§7), with the new lease `lease` (default `now`): the lease
    when this caller won it, else None
    """
    lease = lease or now
    cutoff = now - timedelta(seconds=limits.COMMIT_LEASE)
    stmt = (
        sa.update(Job)
        .where(
            Job.id == job_id,
            Job.status == IngestStatus.committing.value,
            sa.or_(Job.commit_started_at.is_(None), Job.commit_started_at < cutoff),
        )
        .values(commit_started_at=lease)
    )
    return lease if _update(session, stmt) else None


class _LeaseLost(Exception):
    """The commit was taken over while this caller wrote its files"""


def _renew_lease(session: Session, job: RecipeIngestionJob, lease: datetime) -> datetime:
    """
    Restarts the commit's lease after its slow part (the files), so housekeeping doesn't take over a live commit: the
    new lease, always later than `lease`. Raises `_LeaseLost` when the lease is no longer this caller's.
    """
    renewed = max(utcnow(), lease + timedelta(microseconds=1))
    stmt = (
        sa.update(Job)
        .where(*_lease_fence(job.id, lease), Job.commit_recipe_id == job.commit_recipe_id)
        .values(commit_started_at=renewed)
    )
    if not _update(session, stmt):
        raise _LeaseLost()
    return renewed


def _recipe_dir_without_row(session: Session, recipe_id: UUID) -> None:
    """Removes `recipes/<id>` when no recipe has that id; a directory with a row is never deleted"""
    exists = session.execute(sa.select(RecipeModel.id).where(RecipeModel.id == recipe_id)).first() is not None
    session.commit()
    if not exists:
        shutil.rmtree(get_app_dirs().RECIPE_DATA_DIR / str(recipe_id), ignore_errors=True)


def _back_to_ready(
    session: Session, job: RecipeIngestionJob, lease: datetime, code: str, params: dict[str, Any] | None
) -> bool:
    """
    A commit that stopped before `create_one`: the job is `ready` again, with the reason as its error; whether it is.
    Nothing changes (and no file is removed) when the commit has been taken over since.
    """
    reserved = job.commit_recipe_id
    stmt = (
        sa.update(Job)
        .where(
            *_lease_fence(job.id, lease),
            Job.commit_recipe_id.is_(None) if reserved is None else Job.commit_recipe_id == reserved,
        )
        .values(
            status=IngestStatus.ready.value,
            error_code=code,
            error_params=params or None,
            commit_started_at=None,
            row_version=Job.row_version + 1,
        )
    )
    if not _update(session, stmt):
        return False
    if reserved is not None:
        _recipe_dir_without_row(session, reserved)
    return True


def _refused(
    session: Session, job: RecipeIngestionJob, lease: datetime, status_code: int, code: str, **params: Any
) -> JobActionError:
    """
    Returns the job to `ready` with `code` as its error, and the refusal to answer; a commit taken over meanwhile is
    left to its new owner (409 `invalid_status`, `committing`)
    """
    if _back_to_ready(session, job, lease, code, params or None):
        return JobActionError(status_code, code, **params)
    return invalid_status(IngestStatus.committing.value)


def _border_colour(page: Image.Image) -> tuple[int, int, int]:
    """The average colour of the strip along the page's edges (`COVER_BORDER` of each side), else `COVER_BACKGROUND`"""
    width, height = page.size
    side = max(1, round(min(width, height) * COVER_BORDER))
    if width <= 2 * side or height <= 2 * side:
        return COVER_BACKGROUND
    strips = [
        page.crop((0, 0, width, side)),
        page.crop((0, height - side, width, height)),
        page.crop((0, side, side, height - side)),
        page.crop((width - side, side, width, height - side)),
    ]
    totals, pixels = [0.0, 0.0, 0.0], 0
    for strip in strips:
        stat = ImageStat.Stat(strip)
        count = strip.width * strip.height
        totals = [total + band * count for total, band in zip(totals, stat.mean[:3], strict=True)]
        pixels += count
    red, green, blue = (round(total / pixels) for total in totals)
    return red, green, blue


def cover_image(view: Path) -> Path | bytes:
    """
    The recipe's cover from the front page's `view.jpg`. Upstream's recipe header crops its image to a landscape box,
    which cuts the top (the title) off a portrait card, so a portrait page is letterboxed to 4:3: the whole page at
    its own height, centred on the average colour of its border, as a JPEG. A landscape or square page is used as it
    is. Either way the card asset stays the full page.
    """
    with Image.open(view) as image:
        if image.width >= image.height:
            return view
        icc_profile = image.info.get("icc_profile")
        page = image.convert("RGB")

    width = round(page.height * COVER_ASPECT)
    try:
        background = _border_colour(page)
    except Exception:
        background = COVER_BACKGROUND
    cover = Image.new("RGB", (width, page.height), background)
    cover.paste(page, ((width - page.width) // 2, 0))
    buffer = io.BytesIO()
    cover.save(buffer, "JPEG", quality=COVER_QUALITY, icc_profile=icc_profile)
    return buffer.getvalue()


def _write_files(
    job: RecipeIngestionJob, draft: CardDraft, pages: Sequence[PageMeta], token: str, attach: bool = True
) -> None:
    """
    Step 4: the card assets when the card photo is attached (`attach`), and the cover when the draft asks for it. Only
    `recipes/<new id>/` is created. Assets an earlier attempt wrote are removed when the photo isn't attached now.
    """
    recipe_id = job.commit_recipe_id
    if recipe_id is None:
        raise ValueError("The job has no reserved recipe id")
    data = RecipeDataService(recipe_id)
    for page in pages:
        asset = data.dir_assets / asset_file_name(token, page.index)
        if not attach:
            asset.unlink(missing_ok=True)
            continue
        source = images.page_file(storage.page_dir(job.group_id, job.id, page.index), "page")
        storage.atomic_write_bytes(asset, source.read_bytes())
    if draft.use_card_as_cover and pages:
        view = images.page_file(storage.page_dir(job.group_id, job.id, pages[0].index), "view")
        data.write_image(cover_image(view), "jpg")


def _create_recipe(
    repos: AllRepositories, user: PrivateUser, household: HouseholdInDB, translator: Translator, recipe: Recipe
) -> Recipe:
    """Step 5's create: upstream's `create_one`, which commits the recipe, then its rating and timeline entry"""
    return RecipeService(repos, user, household, translator).create_one(recipe)


def _set_cover_key(session: Session, job: RecipeIngestionJob, draft: CardDraft | None, slug: str) -> None:
    """Step 6: gives the recipe its image key when the cover was written (upstream's `create_one` doesn't)"""
    recipe_id = job.commit_recipe_id
    if recipe_id is None or draft is None or not draft.use_card_as_cover:
        return
    cover = get_app_dirs().RECIPE_DATA_DIR / str(recipe_id) / "images" / RecipeImageTypes.original.value
    if not cover.exists():
        return
    recipes = get_repositories(session, group_id=job.group_id, household_id=None).recipes
    recipe = recipes.get_one(recipe_id, "id")
    if recipe is not None and not recipe.image:
        recipes.update_image(slug)


def _mark_card_recipe(session: Session, job: RecipeIngestionJob) -> None:
    """
    Step 6 too: upstream's own provenance bit on the recipe row, `is_ocr_recipe`, which a backup keeps even where the
    fork's tables are gone (`recipe_ingestion_jobs.recipe_id` stays the fork's link). Idempotent, so a resumed commit
    sets it again. Upstream marks the column deprecated: if a sync drops it, this is skipped and commit still works.
    """
    recipe_id = job.commit_recipe_id
    if recipe_id is None or not hasattr(RecipeModel, "is_ocr_recipe"):
        return
    stmt = (
        sa.update(RecipeModel)
        .where(RecipeModel.id == recipe_id, RecipeModel.group_id == job.group_id)
        .values(is_ocr_recipe=True)
    )
    _update(session, stmt)


def _finish(session: Session, job: RecipeIngestionJob, lease: datetime, now: datetime) -> bool:
    """
    Step 7: `committing → committed`; whether this call won it (and so publishes `recipe_created`, holding the event's
    claim from now)
    """
    stmt = (
        sa.update(Job)
        .where(*_lease_fence(job.id, lease), Job.commit_recipe_id == job.commit_recipe_id)
        .values(
            status=IngestStatus.committed.value,
            recipe_id=job.commit_recipe_id,
            committed_at=now,
            recipe_event_claimed_at=now,
            recipe_event_sent_at=None,
            error_code=None,
            error_params=None,
            row_version=Job.row_version + 1,
        )
    )
    return _update(session, stmt)


def _publish_recipe_created(
    session: Session,
    *,
    group_id: UUID,
    household_id: UUID,
    slug: str,
    name: str,
    translator: Translator,
    integration_id: str,
) -> bool:
    """
    Upstream's `recipe_created`, as `RecipeController._publish_recipe_created` sends it, but to the listeners right
    away (in this thread); whether it went out without an error
    """
    try:
        group = get_repositories(session).groups.get_one(group_id)
        url = urls.recipe_url(group.slug if group else "", slug, get_app_settings().BASE_URL)
        EventBusService(None, session, translator).dispatch(
            integration_id=integration_id,
            group_id=group_id,
            household_id=household_id,
            event_type=EventTypes.recipe_created,
            document_data=EventRecipeData(operation=EventOperation.create, recipe_slug=slug),
            message=translator.t("notifications.generic-created-with-url", name=name, url=url),
        )
    except Exception as e:
        # the recipe is committed either way; housekeeping sends the event again later (`resend_recipe_events`)
        session.rollback()
        logger.error(f"Couldn't publish recipe_created for a committed recipe card ({type(e).__name__})")
        return False
    return True


def _mark_event_sent(session: Session, job_id: UUID) -> None:
    """Records that the committed recipe's `recipe_created` went out"""
    unsent = [Job.id == job_id, Job.recipe_event_sent_at.is_(None)]
    _update(session, sa.update(Job).where(*unsent).values(recipe_event_sent_at=utcnow()))


def _send_recipe_created(
    session: Session | None,
    *,
    job_id: UUID,
    group_id: UUID,
    household_id: UUID,
    slug: str,
    name: str,
    translator: Translator,
    integration_id: str,
) -> None:
    """
    Publishes `recipe_created` and records it as sent, with `session` or (from a request's background task, after the
    response) a session of its own
    """
    if session is None:
        with session_context() as own:
            _send_recipe_created(
                own,
                job_id=job_id,
                group_id=group_id,
                household_id=household_id,
                slug=slug,
                name=name,
                translator=translator,
                integration_id=integration_id,
            )
        return
    published = _publish_recipe_created(
        session,
        group_id=group_id,
        household_id=household_id,
        slug=slug,
        name=name,
        translator=translator,
        integration_id=integration_id,
    )
    if published:
        _mark_event_sent(session, job_id)


def _job_translator(job: RecipeIngestionJob) -> Translator:
    """The job's language (the uploader's; en-US for the inbox), falling back to en-US for the fork's texts"""
    return translator_for(job.locale)


def _committer(session: Session, job: RecipeIngestionJob) -> PrivateUser | None:
    """The user who started the commit, while they still belong to the job's household"""
    if job.committed_by is None:
        return None
    user = get_repositories(session, group_id=job.group_id, household_id=None).users.get_one(job.committed_by)
    if user is None or user.group_id != job.group_id or user.household_id != job.household_id:
        return None
    return user


@dataclass
class _Outcome:
    recipe_id: UUID
    slug: str
    name: str
    published: bool
    """This call won the finish, so it publishes `recipe_created` (once the write lock is released)"""
    warnings: list[str] = field(default_factory=list)


def _run(
    session: Session,
    job: RecipeIngestionJob,
    *,
    lease: datetime,
    user: PrivateUser | None,
    translator: Translator,
) -> _Outcome:
    """
    Steps 3 to 7 for a claimed (or taken over) commit, fenced on its `lease`. The caller holds the ingest write lock,
    and publishes `recipe_created` after releasing it when `published`.

    Raises `JobActionError`: `commit_invalid` or `commit_interrupted` when the job went back to `ready`, and
    `invalid_status` (`committing`) when the commit was taken over meanwhile.
    """
    recipe_id, token = job.commit_recipe_id, job.commit_asset_token
    if recipe_id is None or token is None:
        # never left by a claim, which reserves both; nothing can have been created
        raise _refused(session, job, lease, status.HTTP_409_CONFLICT, COMMIT_INTERRUPTED)

    draft = CardDraft.model_validate(job.draft) if job.draft else None
    group_repos = get_repositories(session, group_id=job.group_id, household_id=None)
    existing = group_repos.recipes.get_one(recipe_id, "id")
    warnings: list[str] = []

    if existing is None:
        if user is None:
            raise _refused(session, job, lease, status.HTTP_409_CONFLICT, COMMIT_INTERRUPTED)

        repos = get_repositories(session, group_id=job.group_id, household_id=job.household_id)
        household = repos.households.get_one(job.household_id)
        pages = parse_pages(job.pages)

        try:
            if draft is None or household is None:
                raise DraftInvalid(["draft"])
            attach = attaches_card_photo(draft, household)
            settings = recipe_settings(household, show_assets=attach)
            build = partial(
                draft_to_recipe,
                recipe_id=recipe_id,
                token=token,
                page_count=len(pages) if attach else 0,
                repos=repos,
                translator=translator,
                unreadable=_job_translator(job).t("recipe-ingest.unreadable"),
            )
            build(draft, settings=settings, linker=None)  # validates before anything is written or created
            # a turn a stop left half done is settled first, so the files copied are the ones the pages describe (a
            # committing job has no task, so every page is settled)
            settle_turns(IngestRepos(session, job.group_id, job.household_id), job)
            _write_files(job, draft, pages, token, attach)
            lease = _renew_lease(session, job, lease)
            linker = IngredientLinker(repos, job.group_id, can_create_foods=bool(user.can_organize))
            organizers = OrganizerMaker(repos, job.group_id) if user.can_organize else None
            recipe, warnings = build(draft, settings=settings, linker=linker, organizers=organizers)
        except _LeaseLost as e:
            # taken over while this caller wrote the files: the new owner finishes the commit
            raise invalid_status(IngestStatus.committing.value) from e
        except DraftInvalid as e:
            raise _refused(
                session, job, lease, status.HTTP_422_UNPROCESSABLE_CONTENT, COMMIT_INVALID, fields=e.fields
            ) from e
        except OSError as e:
            # a card file missing or unwritable: nothing was created, so the job can simply be committed again
            session.rollback()
            logger.error(f"Recipe card job {job.id}: couldn't write the recipe's files ({type(e).__name__})")
            raise _refused(session, job, lease, status.HTTP_409_CONFLICT, COMMIT_INTERRUPTED) from e
        except Exception:
            session.rollback()
            _back_to_ready(session, job, lease, COMMIT_INTERRUPTED, None)
            raise

        # from here on the job never goes back to ready: a failure leaves it committing, to be resumed
        created = _create_recipe(repos, user, household, translator, recipe)
        slug, name = created.slug, created.name or ""
    else:
        slug, name = existing.slug, existing.name or ""

    _set_cover_key(session, job, draft, slug)
    _mark_card_recipe(session, job)
    published = _finish(session, job, lease, utcnow())
    return _Outcome(recipe_id=recipe_id, slug=slug, name=name, published=published, warnings=warnings)


def _announce(
    session: Session,
    job: RecipeIngestionJob,
    outcome: _Outcome,
    *,
    translator: Translator,
    integration_id: str,
    background: BackgroundTasks | None,
) -> None:
    """
    `recipe_created` for the call that won the finish, sent once the ingest write lock is released: after the response
    for a request (`background`), else in this thread
    """
    if not outcome.published:
        return
    send = partial(
        _send_recipe_created,
        job_id=job.id,
        group_id=job.group_id,
        household_id=job.household_id,
        slug=outcome.slug,
        name=outcome.name,
        translator=translator,
        integration_id=integration_id,
    )
    if background is not None:
        background.add_task(send, None)
    else:
        send(session)


# ==========================================
# Entry points


@dataclass
class CommitResult:
    out: CommitOut
    created: bool
    """201 when this request committed the card; 200 when it had already been committed"""


def _done(review: ReviewService, job: RecipeIngestionJob) -> CommitResult:
    """A card committed earlier: the same recipe again (a double tap)"""
    recipe_id = job.recipe_id or job.commit_recipe_id
    if recipe_id is None:
        raise invalid_status(job.status)
    recipe = get_repositories(review.session, group_id=job.group_id, household_id=None).recipes.get_one(recipe_id, "id")
    return CommitResult(
        out=CommitOut(
            recipe_id=recipe_id,
            slug=recipe.slug if recipe else "",
            next_job_id=review.next_ready_job_id(job.batch_id, job.id),
        ),
        created=False,
    )


def _refusal(review: ReviewService, job_id: UUID, version: int) -> CommitResult:
    """Why the claim matched nothing; a card committed meanwhile is answered like a double tap"""
    job = review.job(job_id)
    if job.status == IngestStatus.committed.value:
        return _done(review, job)
    if job.status != IngestStatus.ready.value:
        raise invalid_status(job.status)
    if job.draft_version != version:
        raise version_conflict(job.draft_version)
    unresolved = UnresolvedFlagsDetail.of(parse_flags(job.flags)).flags
    if not unresolved and not job.error_count:
        raise not_clean()  # a bulk commit's claim: a warning came back meanwhile
    raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, UNRESOLVED_FLAGS, flags=unresolved)


def not_clean() -> JobActionError:
    return JobActionError(status.HTTP_409_CONFLICT, NOT_CLEAN)


def _take_over(
    review: ReviewService,
    job: RecipeIngestionJob,
    *,
    user: PrivateUser,
    translator: Translator,
    integration_id: str,
    background: BackgroundTasks | None,
) -> CommitResult:
    """A request for a job still `committing`: 409 while its lease runs, else it resumes the commit as its committer"""
    session = review.session
    with storage.ingest_write():
        lease = _win_lease(session, job.id, utcnow())
        if lease is None:
            raise invalid_status(IngestStatus.committing.value)
        job = review.job(job.id)
        if job.committed_by == user.id:
            committer: PrivateUser | None = user
        else:
            committer, translator = _committer(session, job), _job_translator(job)
        outcome = _run(session, job, lease=lease, user=committer, translator=translator)
    _announce(session, job, outcome, translator=translator, integration_id=integration_id, background=background)

    return CommitResult(
        out=CommitOut(
            recipe_id=outcome.recipe_id,
            slug=outcome.slug,
            next_job_id=review.next_ready_job_id(job.batch_id, job.id),
            warnings=outcome.warnings,
        ),
        created=outcome.published,
    )


def commit_job(
    repos: IngestRepos,
    user: PrivateUser,
    job_id: UUID,
    request: CommitRequest,
    *,
    translator: Translator,
    integration_id: str = DEFAULT_INTEGRATION_ID,
    background: BackgroundTasks | None = None,
    require_clean: bool = False,
) -> CommitResult:
    """
    Commits a reviewed card for the user (§7). `request.draft`, when sent, is saved first with the version check.
    `recipe_created` goes out through `background` after the response. `require_clean` (a bulk commit) refuses a card
    with an unresolved warning too (409 `not_clean`).

    Raises `JobActionError` (404, 409 `version_conflict` / `invalid_status` / `commit_interrupted`, 422
    `unresolved_flags` / `commit_invalid`) and `IngestPaused` while a backup restore holds ingestion.
    """
    translator = with_fallback(translator)
    review = ReviewService(repos, user)
    job = review.job(job_id)

    if job.status == IngestStatus.committed.value:
        return _done(review, job)
    if job.status == IngestStatus.committing.value:
        return _take_over(
            review, job, user=user, translator=translator, integration_id=integration_id, background=background
        )
    if job.status != IngestStatus.ready.value:
        raise invalid_status(job.status)

    version = request.draft_version
    if request.draft is not None:
        try:
            update = CardDraftUpdate(draft_version=version, draft=request.draft)
        except ValidationError as e:
            # the limits of a saved draft (which a PUT checks as it reads its body): nothing is saved or claimed
            fields = DraftInvalid.of(e).fields
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, COMMIT_INVALID, fields=fields) from e
        version = review.save_draft(job_id, update).draft_version
        job = review.job(job_id)
    elif job.draft_version != version:
        raise version_conflict(job.draft_version)

    unresolved = UnresolvedFlagsDetail.of(parse_flags(job.flags)).flags
    if unresolved or job.error_count:
        raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, UNRESOLVED_FLAGS, flags=unresolved)
    if require_clean and job.warning_count:
        raise not_clean()

    with storage.ingest_write():
        lease = utcnow()
        claimed = _claim(
            review.session, job_id, review.household_id, user.id, version, lease, require_clean=require_clean
        )
        if not claimed:
            return _refusal(review, job_id, version)
        job = review.job(job_id)
        outcome = _run(review.session, job, lease=lease, user=user, translator=translator)
    _announce(review.session, job, outcome, translator=translator, integration_id=integration_id, background=background)

    return CommitResult(
        out=CommitOut(
            recipe_id=outcome.recipe_id,
            slug=outcome.slug,
            next_job_id=review.next_ready_job_id(job.batch_id, job_id),
            warnings=outcome.warnings,
        ),
        created=True,
    )


def commit_clean(
    repos: IngestRepos,
    user: PrivateUser,
    batch_id: UUID,
    request: BulkCommitRequest,
    *,
    translator: Translator,
    integration_id: str = DEFAULT_INTEGRATION_ID,
    background: BackgroundTasks | None = None,
) -> BulkCommitOut:
    """
    Commits the listed cards of a batch, one by one through `commit_job` (the same checks, lease and events), each
    that is still `ready` at the version the page showed and has no unresolved error or warning; the others are left
    for review with the reason. A restore pausing ingestion stops the run. 404 `not_found` for another household's
    batch.
    """
    if repos.batches.get(batch_id) is None:
        raise not_found()

    out = BulkCommitOut()
    paused = False
    for job_id in request.job_ids:
        code: str | None = None
        job = repos.jobs.get(job_id)
        if paused:
            code = PAUSED_FOR_RESTORE
        elif job is None or job.batch_id != batch_id:
            code = NOT_FOUND
        elif job.status != IngestStatus.ready.value:
            code = INVALID_STATUS
        elif job.draft_version != request.draft_versions[job_id]:
            code = VERSION_CONFLICT
        elif job.error_count or job.warning_count:
            code = NOT_CLEAN
        if code is not None:
            out.skipped.append(BulkCommitSkipped(job_id=job_id, code=code))
            continue

        try:
            result = commit_job(
                repos,
                user,
                job_id,
                CommitRequest(draft_version=request.draft_versions[job_id]),
                translator=translator,
                integration_id=integration_id,
                background=background,
                require_clean=True,
            )
        except JobActionError as e:
            out.skipped.append(BulkCommitSkipped(job_id=job_id, code=e.code))
        except IngestPaused:
            paused = True
            out.skipped.append(BulkCommitSkipped(job_id=job_id, code=PAUSED_FOR_RESTORE))
        except Exception as e:
            repos.session.rollback()
            logger.error(f"Couldn't commit recipe card job {job_id} with its batch ({type(e).__name__})")
            out.skipped.append(BulkCommitSkipped(job_id=job_id, code=IngestErrorCode.internal_error.value))
        else:
            out.committed.append(BulkCommitted(job_id=job_id, recipe_id=result.out.recipe_id, slug=result.out.slug))
    return out


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _edited_since(recipe: Recipe, committed_at: datetime | None) -> bool:
    """Whether the recipe was updated after its commit (beyond the commit's own cover update)"""
    if recipe.date_updated is None or committed_at is None:
        return False
    return _naive_utc(recipe.date_updated) > _naive_utc(committed_at) + UNCOMMIT_GRACE


def uncommit_job(
    repos: IngestRepos,
    user: PrivateUser,
    job_id: UUID,
    request: UncommitRequest,
    *,
    translator: Translator,
    integration_id: str = DEFAULT_INTEGRATION_ID,
    background: BackgroundTasks | None = None,
) -> RecipeIngestionJobState:
    """
    Undoes a commit (back to review): deletes the recipe through upstream's recipe service (its files go too, and
    `recipe_deleted` is published as for any delete), then puts the card back to `ready` with its stored draft in
    one guarded update, with a new `draft_version` and the commit's ids and times cleared, so its next commit makes a
    new recipe. For the committer or a household manager, who must also be allowed to delete the recipe (its owner,
    or an admin). A recipe edited since the commit is a 409 `recipe_edited` unless `force`; one already deleted just
    lets the card go back. The caller holds the ingest write lock, so the retention purge can't remove the card's
    files meanwhile.

    Raises `JobActionError`: 404, 403 `forbidden`, 409 `invalid_status` / `purged` / `recipe_edited`.
    """
    translator = with_fallback(translator)
    review = ReviewService(repos, user)
    session = review.session
    job = review.job(job_id)
    if job.status != IngestStatus.committed.value:
        raise invalid_status(job.status)
    if is_slimmed(job):
        raise JobActionError(status.HTTP_409_CONFLICT, PURGED)
    if job.committed_by != user.id and not user.can_manage_household:
        raise JobActionError(status.HTTP_403_FORBIDDEN, FORBIDDEN)

    recipe_id = job.recipe_id
    recipe = None
    if recipe_id is not None:
        recipe = get_repositories(session, group_id=job.group_id, household_id=None).recipes.get_one(recipe_id, "id")
    if recipe is not None:
        household_repos = get_repositories(session, group_id=user.group_id, household_id=user.household_id)
        household = household_repos.households.get_one(user.household_id)
        if household is None:
            raise not_found()
        service = RecipeService(household_repos, user, household, translator)
        if not service.can_delete([recipe.slug]):
            raise JobActionError(status.HTTP_403_FORBIDDEN, FORBIDDEN)
        if not request.force and _edited_since(recipe, job.committed_at):
            raise JobActionError(status.HTTP_409_CONFLICT, RECIPE_EDITED)

        deleted = service.delete_one(recipe.slug)
        EventBusService(background, session, translator).dispatch(
            integration_id=integration_id,
            group_id=deleted.group_id,
            household_id=deleted.household_id,
            event_type=EventTypes.recipe_deleted,
            document_data=EventRecipeData(operation=EventOperation.delete, recipe_slug=deleted.slug),
            message=translator.t("notifications.generic-deleted", name=deleted.name),
        )

    stmt = (
        sa.update(Job)
        .where(
            Job.id == job_id,
            Job.household_id == review.household_id,
            Job.status == IngestStatus.committed.value,
            Job.draft.is_not(None),
            Job.recipe_id.is_(None) if recipe_id is None else Job.recipe_id == recipe_id,
        )
        .values(
            status=IngestStatus.ready.value,
            draft_version=Job.draft_version + 1,
            recipe_id=None,
            commit_recipe_id=None,
            commit_asset_token=None,
            committed_by=None,
            commit_started_at=None,
            committed_at=None,
            recipe_event_claimed_at=None,
            recipe_event_sent_at=None,
            error_code=None,
            error_params=None,
            row_version=Job.row_version + 1,
        )
    )
    if not _update(session, stmt):
        current = review.job(job_id)
        if current.status != IngestStatus.ready.value:
            raise invalid_status(current.status)
    return review.get_state(job_id)


def resend_recipe_events(now: datetime) -> int:
    """
    Sends `recipe_created` for committed cards whose event wasn't recorded as sent (the process stopped, or the send
    failed): committed more than `RECIPE_EVENT_GRACE` and less than `RECIPE_EVENT_CUTOFF` before `now`, with no claim
    or one older than `RECIPE_EVENT_LEASE`. Each is claimed with a conditional update first, so two processes never
    both send it; at most `RECIPE_EVENT_BATCH` a run. A card whose recipe is gone is marked sent without an event.
    Returns how many were sent.
    """
    if storage.is_paused():
        return 0

    def due(cutoff: datetime) -> list[sa.ColumnElement[bool]]:
        return [
            Job.status == IngestStatus.committed.value,
            Job.recipe_event_sent_at.is_(None),
            Job.recipe_id.is_not(None),
            Job.committed_at < now - RECIPE_EVENT_GRACE,
            Job.committed_at > now - RECIPE_EVENT_CUTOFF,
            sa.or_(Job.recipe_event_claimed_at.is_(None), Job.recipe_event_claimed_at < cutoff),
        ]

    sent = 0
    with session_context() as session:
        stmt = sa.select(Job.id).where(*due(now - RECIPE_EVENT_LEASE)).order_by(Job.committed_at, Job.id)
        job_ids = list(session.execute(stmt.limit(RECIPE_EVENT_BATCH)).scalars())
        session.commit()

        queue = IngestQueue(session)
        for job_id in job_ids:
            try:
                claimed_at = max(now, utcnow())
                claim = sa.update(Job).where(Job.id == job_id, *due(now - RECIPE_EVENT_LEASE))
                if not _update(session, claim.values(recipe_event_claimed_at=claimed_at)):
                    continue  # another process has it
                job = queue.get(job_id)
                if job is None or job.recipe_id is None:
                    continue
                recipes = get_repositories(session, group_id=job.group_id, household_id=None).recipes
                recipe = recipes.get_one(job.recipe_id, "id")
                if recipe is None:
                    _mark_event_sent(session, job_id)  # deleted since: nothing to announce
                    continue
                _send_recipe_created(
                    session,
                    job_id=job_id,
                    group_id=job.group_id,
                    household_id=job.household_id,
                    slug=recipe.slug,
                    name=recipe.name or "",
                    translator=_job_translator(job),
                    integration_id=DEFAULT_INTEGRATION_ID,
                )
                sent += 1
            except Exception as e:
                session.rollback()
                logger.error(f"Couldn't send recipe_created for recipe card job {job_id} ({type(e).__name__})")
    return sent


def resume_stale_commits(now: datetime) -> int:
    """
    Resumes commits whose lease (`commit_started_at`) was older than `COMMIT_LEASE` at `now`: the number resumed.
    Each one runs as its committer, in the job's language, inside the ingest write lock, with a lease taken when it
    starts (not at `now`, which may be a while back for the last of several), and publishes `recipe_created` in this
    thread, after the lock is released, if it wins the finish. A commit whose committer is gone and that created no
    recipe goes back to `ready` with `commit_interrupted`. Stops at once while a backup restore holds ingestion.
    """
    cutoff = now - timedelta(seconds=limits.COMMIT_LEASE)
    resumed = 0
    with session_context() as session:
        stmt = (
            sa.select(Job.id)
            .where(
                Job.status == IngestStatus.committing.value,
                sa.or_(Job.commit_started_at.is_(None), Job.commit_started_at < cutoff),
            )
            .order_by(Job.commit_started_at, Job.id)
        )
        job_ids = list(session.execute(stmt).scalars())
        session.commit()

        queue = IngestQueue(session)
        for job_id in job_ids:
            try:
                with storage.ingest_write():
                    lease = _win_lease(session, job_id, now, lease=max(now, utcnow()))
                    if lease is None:
                        continue
                    job = queue.get(job_id)
                    if job is None:
                        continue
                    resumed += 1
                    translator = _job_translator(job)
                    outcome = _run(session, job, lease=lease, user=_committer(session, job), translator=translator)
                _announce(
                    session,
                    job,
                    outcome,
                    translator=translator,
                    integration_id=DEFAULT_INTEGRATION_ID,
                    background=None,
                )
            except IngestPaused:
                break
            except JobActionError as e:
                logger.info(f"Recipe card job {job_id}: its interrupted commit ended with {e.code}")
            except Exception as e:
                session.rollback()
                # never the exception's text: it can hold card text (a recipe name in a failed INSERT)
                logger.error(f"Couldn't resume the commit of recipe card job {job_id} ({type(e).__name__})")

    # and the events of finished commits that weren't recorded as sent
    try:
        resend_recipe_events(now)
    except Exception as e:
        logger.error(f"Couldn't send the late recipe_created events ({type(e).__name__})")
    return resumed
