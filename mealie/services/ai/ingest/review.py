"""
The review side of a recipe card job (docs/ai/PHASE2.md §3.1, §3.5, §6, §9, §14): listing, counts, the job and its
state, optimistic draft saves (`draftVersion`), re-read, re-extract, retry, cancel, rotate, discard and the page images.

Everything here is scoped to the user's household through `IngestRepos`, so another household's job (its images
included) is simply not found. Refusals are raised as `JobActionError`, which the routes turn into
`{"detail": {"code": ..., **params}}` bodies; the codes the review page handles itself (`version_conflict`, `busy`,
`unresolved_flags`) never carry a `message`.

**Writes follow the job table's rules (§3.3):** a draft save is a read-modify-write through `update_job_json`, written
`WHERE id AND row_version=:rv AND draft_version=:v AND status='ready'`, so a task's proposal landing between the read
and the write only makes the save re-read and retry, while a stale `draftVersion` is a 409. New tasks go through
`enqueue_task`, conditional on the job having none. Rotate and discard run inside the ingest write lock, which their
callers hold.
"""

import asyncio
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi import status
from pydantic import ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy.engine import RowMapping

from mealie.core.root_logger import get_logger
from mealie.db.models.recipe.recipe import RecipeModel
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestRepos, JobConflict, enqueue_task
from mealie.schema.household.household import HouseholdInDB
from mealie.schema.recipe.recipe import create_recipe_slug
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftSaved,
    CardDraftUpdate,
    CardFlag,
    CardFlagSeverity,
    CardProposal,
    CardProposalKind,
    ExtractionMeta,
    FlagResolution,
    IngestErrorCode,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
    PageOut,
    PageRotationSource,
    RecipeIngestionJobCounts,
    RecipeIngestionJobError,
    RecipeIngestionJobOut,
    RecipeIngestionJobPagination,
    RecipeIngestionJobPermissions,
    RecipeIngestionJobState,
    RecipeIngestionJobSummary,
    RecipeIngestionJobTask,
    RecipeIngestionRecipeRef,
    RereadRequest,
)
from mealie.schema.user.user import PrivateUser
from mealie.services.ai.ingest import flag_rules, images, limits, storage
from mealie.services.ai.ingest.eval_export import EXPORTABLE_STATUSES
from mealie.services.ai.ingest.i18n import translator_for
from mealie.services.ai.ingest.intake import source_sha256
from mealie.services.ai.ingest.matching import IngestMatcher
from mealie.services.ai.ingest.pipeline import flags as card_flags
from mealie.services.ai.ingest.pipeline.cardtext import markers_in
from mealie.services.ai.ingest.pipeline.ingredients import IngredientLine, normalize_lines
from mealie.services.ai.ingest.runner.dispatcher import dispatcher
from mealie.services.ai.ingest.settings import get_ingest_settings

logger = get_logger(__name__)

Job = RecipeIngestionJob

# ==========================================
# Refusals


NOT_FOUND = "not_found"
"""404: no such job (or page) in the user's household"""
VERSION_CONFLICT = "version_conflict"
"""409 with `current`: the draft was saved from somewhere else since this client read it"""
BUSY = "busy"
"""409: the job already has a task (a re-read, re-extract or first extraction)"""
INVALID_STATUS = "invalid_status"
"""409 with `status`: the job's status doesn't allow this (§3.1)"""
UNRESOLVED_FLAGS = "unresolved_flags"
"""422 with `flags`: errors that must be fixed or kept before commit"""
COMMIT_INVALID = "commit_invalid"
"""422 with `fields`: the draft no longer validates into a recipe"""
FORBIDDEN = "forbidden"
"""403: e.g. discarding someone else's card without `can_manage_household`"""
FILES_MISSING = IngestErrorCode.files_missing.value
"""409: the card's photos are missing on disk"""
UNKNOWN_PAGE = "unknown_page"
"""422: a re-read names a page the card doesn't have"""
UNKNOWN_TARGET = "unknown_target"
"""422: a re-read targets an ingredient or step `ref` the draft doesn't have"""
GROUP_LOCAL_ONLY = "group_local_only"
"""409: the group keeps every card on this server, so none can be read with a cloud provider"""
TOO_MANY_PAGES = "too_many_pages"
"""409 with `max`: merging would give the card more pages than a card may have"""
SAME_CARD = "same_card"
"""422: a card can't be added to itself"""
PURGED = "purged"
"""409: the card's photos and draft were removed after the retention period, so it can't go back to review"""
RECIPE_EDITED = "recipe_edited"
"""409: the recipe was edited after the card was added; undoing the commit would lose that (send `force`)"""
NOT_CLEAN = "not_clean"
"""A bulk commit left the card for review: it has a highlighted problem nobody resolved"""
PAUSED_FOR_RESTORE = "paused_for_restore"
"""A bulk commit stopped: a backup restore paused recipe cards"""

UNCOMMIT_GRACE = timedelta(seconds=5)
"""A recipe updated later than this after its commit was edited (the commit's own cover update is within it)"""


class JobActionError(Exception):
    """A request the job refuses; the routes answer `status_code` with `{"detail": {"code": code, **params}}`"""

    def __init__(self, status_code: int, code: str, **params: Any) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.params = params


def not_found() -> JobActionError:
    return JobActionError(status.HTTP_404_NOT_FOUND, NOT_FOUND)


def busy() -> JobActionError:
    return JobActionError(status.HTTP_409_CONFLICT, BUSY)


def invalid_status(current: str) -> JobActionError:
    return JobActionError(status.HTTP_409_CONFLICT, INVALID_STATUS, status=current)


def version_conflict(current: int) -> JobActionError:
    return JobActionError(status.HTTP_409_CONFLICT, VERSION_CONFLICT, current=current)


# ==========================================
# Reading stored JSON


def parse_pages(raw: Any) -> list[PageMeta]:
    """The `pages` column as `PageMeta`s, in page order"""
    pages = [PageMeta.model_validate(page) for page in raw or []]
    return sorted(pages, key=lambda page: page.index)


def parse_flags(raw: Any) -> list[CardFlag]:
    """The `flags` column; a flag this version can't read (a kind from a newer version) is skipped"""
    flags: list[CardFlag] = []
    for item in raw or []:
        try:
            flags.append(CardFlag.model_validate(item))
        except ValidationError:
            logger.warning("Skipped a recipe card flag this version can't read")
    return flags


def _parse_proposals(raw: Any) -> list[CardProposal]:
    proposals: list[CardProposal] = []
    for item in raw or []:
        try:
            proposals.append(CardProposal.model_validate(item))
        except ValidationError:
            logger.warning("Skipped a recipe card proposal this version can't read")
    return proposals


def _parse_draft(raw: Any) -> CardDraft | None:
    return CardDraft.model_validate(raw) if raw else None


def _parse_extraction(raw: Any) -> ExtractionMeta | None:
    if not raw:
        return None
    try:
        return ExtractionMeta.model_validate(raw)
    except ValidationError:
        return None


def _task(job: RecipeIngestionJob) -> RecipeIngestionJobTask | None:
    if job.task_state is None or job.task_kind is None:
        return None
    return RecipeIngestionJobTask(
        kind=IngestTaskKind(job.task_kind),
        state=IngestTaskState(job.task_state),
        progress_key=job.progress_key,
        cancel_requested=job.cancel_requested,
    )


def _error(job: RecipeIngestionJob) -> RecipeIngestionJobError | None:
    if not job.error_code:
        return None
    try:
        code = IngestErrorCode(job.error_code)
    except ValueError:
        code = IngestErrorCode.internal_error
    params = job.error_params if isinstance(job.error_params, dict) else {}
    return RecipeIngestionJobError(code=code, params=params)


def is_slimmed(job: RecipeIngestionJob) -> bool:
    """A committed job whose files and card text the retention purge has removed (§16)"""
    return job.status == IngestStatus.committed.value and job.draft is None and job.flags is None


READABLE_STATUSES = frozenset({IngestStatus.processing.value, IngestStatus.ready.value, IngestStatus.failed.value})
"""A job in these may still run a task (a first read, a retry, a re-read or re-extract), under the group's policy"""


# ==========================================
# Draft saves


def resolve_flags(
    draft: CardDraft,
    extraction: ExtractionMeta | None,
    resolutions: Mapping[str, FlagResolution],
    *,
    transcription: str | None = None,
    previous: Sequence[CardFlag] | None = None,
) -> list[CardFlag]:
    """
    The draft's flags with the reviewer's resolutions applied (§4.6), as stored and returned on every save. Only
    errors of the kinds that can be kept as written can be `kept`, and only warnings can be `dismissed`; anything else
    is dropped, as are resolutions of flags the draft no longer raises.

    `transcription` and `previous` are the job's stored transcription and flags, as `compute_flags` takes them on a
    save: the checks against what the card says (`not_on_card`, `marker_dropped`) are made again, and reading flags
    are kept only where they were raised before, so the reviewer's own edits never raise one.
    """
    flags = card_flags.compute_flags(draft, extraction, resolutions, transcription=transcription, previous=previous)
    resolved: list[CardFlag] = []
    for flag in flags:
        resolution = flag.resolution or resolutions.get(flag.id)
        if resolution == FlagResolution.kept and not (
            flag.severity == CardFlagSeverity.error and flag.kind in flag_rules.KEEPABLE_KINDS
        ):
            resolution = None
        elif resolution == FlagResolution.dismissed and flag.severity != CardFlagSeverity.warning:
            resolution = None
        resolved.append(flag if flag.resolution == resolution else flag.model_copy(update={"resolution": resolution}))
    return resolved


def _adopted_reading_flags(
    draft: CardDraft,
    proposals: Iterable[CardProposal],
    extraction: ExtractionMeta | None,
    transcription: str | None,
    *,
    pages: Sequence[PageMeta] = (),
    units: Iterable[str] = (),
) -> list[CardFlag]:
    """
    The flags of each whole-card proposal (a re-extract of an edited draft) that `draft` took up, computed as the
    re-extract computed them: against the job's transcription, extraction and pages, which are that reading's, and the
    group's `units` (`IngestMatcher.unit_names`). A save that accepts the proposal passes them as `previous`, so the
    new reading's reading flags (Tesseract's check of a printed card's numbers, a lost unit that is one of the group's
    own, included) are raised on the draft it became (its ingredient and step ids are new, so none of the stored flags
    matches them). Accepting keeps the proposal's ingredient and step ids; a dismissed one shares none with the draft.
    """
    flags: list[CardFlag] = []
    units = list(units)
    read_path = extraction.read_path if extraction else None
    ocr_lines = card_flags.ocr_check_lines([page.ocr for page in pages], read_path, transcription)
    ids = {ingredient.reference_id for ingredient in draft.ingredients} | {step.id for step in draft.steps}
    for proposal in proposals:
        proposed = proposal.draft
        if proposal.kind != CardProposalKind.full or proposed is None:
            continue
        proposed_ids = {ingredient.reference_id for ingredient in proposed.ingredients}
        proposed_ids |= {step.id for step in proposed.steps}
        if ids & proposed_ids or (not proposed_ids and proposed == draft):
            flags.extend(
                card_flags.compute_flags(
                    proposed, extraction, {}, transcription=transcription, units=units, ocr_lines=ocr_lines
                )
            )
    return flags


def _has_parts(ingredient: CardDraftIngredient) -> bool:
    """Whether a line has an amount, unit or food (the page's `isParsedIngredient`), rather than only its text"""
    return (
        ingredient.quantity is not None
        or bool(ingredient.unit and ingredient.unit.name.strip())
        or bool(ingredient.food and ingredient.food.name.strip())
    )


def _text_to_parse(ingredient: CardDraftIngredient, stored: Mapping[UUID, CardDraftIngredient]) -> str | None:
    """
    The text of a line the reviewer wrote as text, to parse as a freshly read line: a line with no amount, unit or
    food whose text (its note) isn't the stored line's, since a blank was filled, the text edited or the line added.
    None for anything else: the reviewer's own amount, unit or food, a line nobody changed, a marker still in it.
    """
    text = ingredient.note.strip()
    if _has_parts(ingredient) or not text or markers_in(text):
        return None
    before = stored.get(ingredient.reference_id)
    if before is not None and before.note.strip() == text:
        return None
    return text


def _stored_form(draft: CardDraft) -> Any:
    """The draft as the `draft` column stores it (and reads it back)"""
    return to_jsonable_python(draft, by_alias=False, inf_nan_mode="null")


def _with_unique_ids(draft: CardDraft) -> CardDraft:
    """
    Flags are keyed to ingredient `reference_id`s, step ids and note ids, so a draft holding a duplicate (a pasted row)
    gets a fresh id for each repeat. A note sent without an id already has one (`CardDraft` gives it `note_id_for` its
    place and text), and keeps it while it's saved unchanged.
    """
    seen: set[UUID] = set()
    ingredients = []
    for ingredient in draft.ingredients:
        if ingredient.reference_id in seen:
            ingredient = ingredient.model_copy(update={"reference_id": _fresh_id(seen)})
        seen.add(ingredient.reference_id)
        ingredients.append(ingredient)

    steps = []
    for step in draft.steps:
        if step.id in seen:
            step = step.model_copy(update={"id": _fresh_id(seen)})
        seen.add(step.id)
        steps.append(step)

    notes = []
    for note in draft.notes:
        if note.id in seen:
            note = note.model_copy(update={"id": _fresh_id(seen)})
        seen.add(note.id)
        notes.append(note)

    if ingredients == draft.ingredients and steps == draft.steps and notes == draft.notes:
        return draft
    return draft.model_copy(update={"ingredients": ingredients, "steps": steps, "notes": notes})


def _fresh_id(taken: set[UUID]) -> UUID:
    new_id = uuid4()
    while new_id in taken:
        new_id = uuid4()
    return new_id


def _title(draft: CardDraft) -> str | None:
    name = draft.name.strip()
    return name[:255] if name else None


def recipes_public(household: HouseholdInDB | None) -> bool:
    """
    New recipes in the household can be seen without a login, so a card photo on one could be too: upstream's explore
    routes need both a household that isn't private and recipes that are public by default. A new install has a private
    household whose recipes are "public", so the card is attached there.
    """
    preferences = household.preferences if household else None
    return bool(preferences and not preferences.private_household and preferences.recipe_public)


def attaches_card_photo(draft: CardDraft, household: HouseholdInDB | None) -> bool:
    """
    Whether commit attaches the card's photos to the recipe: the draft's switch, else the household's default, which
    keeps them off recipes that are public when created (assets are served without a login)
    """
    if draft.attach_card_photo is not None:
        return draft.attach_card_photo
    return not recipes_public(household)


# ==========================================
# Page images


PAGE_MEDIA_TYPES = {"page": "image/jpeg", "view": "image/jpeg", "thumb": "image/webp"}


@dataclass(frozen=True)
class PageImage:
    """A page image to serve, and what its response headers need"""

    path: Path
    media_type: str
    etag: str
    """Changes whenever the page is rewritten (its `page_sha256`) or turned"""
    version: str
    """The `?v=` the page's URLs carry"""
    stat: Any
    """`os.stat_result` of the file, read before responding"""


# ==========================================
# The service


class ReviewService:
    """Recipe card review for one user, scoped to their group and household (§9)"""

    def __init__(self, repos: IngestRepos, user: PrivateUser) -> None:
        if repos.household_id is None:
            raise ValueError("Recipe card review needs repositories scoped to a household")
        self.repos = repos
        self.session = repos.session
        self.user = user
        self.group_id: UUID = repos.group_id
        self.household_id: UUID = repos.household_id

    @cached_property
    def _group_local_only(self) -> bool:
        return self.repos.settings.get().local_only

    # ==========================================
    # Reading

    def job(self, job_id: UUID) -> RecipeIngestionJob:
        """The household's job; 404 `not_found` when there's none"""
        job = self.repos.jobs.get(job_id)
        if job is None:
            raise not_found()
        return job

    def _recipe_refs(self, recipe_ids: Iterable[UUID]) -> dict[UUID, RecipeIngestionRecipeRef]:
        ids = {recipe_id for recipe_id in recipe_ids if recipe_id}
        if not ids:
            return {}
        stmt = sa.select(RecipeModel.id, RecipeModel.slug, RecipeModel.name).where(
            RecipeModel.id.in_(ids), RecipeModel.group_id == self.group_id
        )
        return {
            row.id: RecipeIngestionRecipeRef(id=row.id, slug=row.slug, name=row.name)
            for row in self.session.execute(stmt)
        }

    def _summary_fields(
        self, job: RecipeIngestionJob, recipes: Mapping[UUID, RecipeIngestionRecipeRef]
    ) -> dict[str, Any]:
        pages = parse_pages(job.pages)
        thumb_url = None
        if pages and not is_slimmed(job):
            thumb_url = PageOut.from_meta(job.id, pages[0]).thumb_url

        recipe = None
        if job.recipe_id:
            recipe = recipes.get(job.recipe_id) or RecipeIngestionRecipeRef(id=job.recipe_id)

        return {
            "id": job.id,
            "batch_id": job.batch_id,
            "position": job.position,
            "status": IngestStatus(job.status),
            "source": IngestSource(job.source),
            "source_name": job.source_name,
            "title": job.title,
            "page_count": len(pages),
            "thumb_url": thumb_url,
            "error_count": job.error_count,
            "warning_count": job.warning_count,
            "task": _task(job),
            "error": _error(job),
            "recipe": recipe,
            "local_only": self._local_only(job),
            "can_discard": self.can_discard(job),
            "created_at": job.created_at,
            "committed_at": job.committed_at,
            "auto_retry_at": job.auto_retry_at if job.status == IngestStatus.failed.value else None,
            "expires_at": self._expires_at(job),
        }

    @staticmethod
    def _expires_at(job: RecipeIngestionJob) -> datetime | None:
        """
        When the retention purge removes a failed card (§16): `AI_INGEST_RETENTION_DAYS` after its last change, or
        after its automatic retry for one waiting for a monthly limit to reset (the purge's own cutoff)
        """
        if job.status != IngestStatus.failed.value:
            return None
        since = job.auto_retry_at or job.update_at or job.created_at
        return since + timedelta(days=get_ingest_settings().RETENTION_DAYS) if since else None

    def _local_only(self, job: RecipeIngestionJob) -> bool:
        """
        The policy the job's reads run under: its own `local_only`, or its group's setting as it is now, which the
        worker applies to every task it runs (§10). A committed card isn't read again, so only its own counts.
        """
        return bool(job.local_only) or (job.status in READABLE_STATUSES and self._group_local_only)

    def list_jobs(
        self,
        *,
        statuses: Sequence[IngestStatus] | None = None,
        batch_id: UUID | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> RecipeIngestionJobPagination:
        """A page of the household's jobs, newest first; `per_page=-1` gives them all"""
        page = max(page, 1)
        jobs, total = self.repos.jobs.page(statuses=statuses or None, batch_id=batch_id, page=page, per_page=per_page)
        recipes = self._recipe_refs(job.recipe_id for job in jobs if job.recipe_id)
        items = [RecipeIngestionJobSummary(**self._summary_fields(job, recipes)) for job in jobs]

        size = per_page if per_page > 0 else max(total, 1)
        return RecipeIngestionJobPagination(
            page=page if per_page > 0 else 1,
            per_page=per_page,
            total=total,
            total_pages=max(math.ceil(total / size), 1) if total else 0,
            items=items,
        )

    def counts(self) -> RecipeIngestionJobCounts:
        return self.repos.jobs.counts()

    def _permissions(self, job: RecipeIngestionJob) -> RecipeIngestionJobPermissions:
        return RecipeIngestionJobPermissions(
            can_create_foods=bool(self.user.can_organize),
            can_create_organizers=bool(self.user.can_organize),
            can_discard=self.can_discard(job),
            can_export_eval=bool(self.user.can_manage) and self.exportable(job),
            can_read_with_cloud=self.can_read_with_cloud(job),
            can_uncommit=self.can_uncommit(job),
            can_merge=self.can_merge(job),
        )

    def _uploaded(self, job: RecipeIngestionJob) -> bool:
        return job.created_by is not None and job.created_by == self.user.id

    def can_read_with_cloud(self, job: RecipeIngestionJob) -> bool:
        """
        A card that failed because it had to stay local and nothing local could read it, sent so (its own
        `local_only`) while its group doesn't keep cards local: its uploader or a household manager may have it read by
        any of the group's providers
        """
        return (self._uploaded(job) or bool(self.user.can_manage_household)) and self._cloud_readable(job)

    def _cloud_readable(self, job: RecipeIngestionJob) -> bool:
        return (
            job.status == IngestStatus.failed.value
            and job.error_code == IngestErrorCode.local_only_unavailable.value
            and bool(job.local_only)
            and not self._group_local_only
        )

    def can_merge(self, job: RecipeIngestionJob) -> bool:
        """A card being reviewed or failed, with no task, that the user uploaded or manages can become another's back"""
        return (
            (self._uploaded(job) or bool(self.user.can_manage_household))
            and job.status in (IngestStatus.ready.value, IngestStatus.failed.value)
            and job.task_state is None
        )

    def can_uncommit(self, job: RecipeIngestionJob) -> bool:
        """
        A committed card, its files and draft still kept, can go back to review for its committer or a household
        manager, who may also delete its recipe as upstream allows (its owner, or an admin), unless it's gone already
        """
        if job.status != IngestStatus.committed.value or is_slimmed(job):
            return False
        if job.committed_by != self.user.id and not self.user.can_manage_household:
            return False
        recipe = self._recipe_row(job.recipe_id)
        return recipe is None or bool(self.user.admin) or recipe.user_id == self.user.id

    def _recipe_row(self, recipe_id: UUID | None) -> Any:
        if recipe_id is None:
            return None
        stmt = sa.select(RecipeModel.id, RecipeModel.slug, RecipeModel.name, RecipeModel.user_id).where(
            RecipeModel.id == recipe_id, RecipeModel.group_id == self.group_id
        )
        return self.session.execute(stmt).one_or_none()

    @staticmethod
    def exportable(job: RecipeIngestionJob) -> bool:
        """
        Whether the card can be saved as an eval case (§11.6): `ready` or `committed` with its draft and pages, which
        a committed card loses to the retention purge. A page file gone missing is only found when exporting.
        """
        return job.status in EXPORTABLE_STATUSES and bool(job.draft) and bool(job.pages) and not is_slimmed(job)

    def can_discard(self, job: RecipeIngestionJob) -> bool:
        """
        §9: the uploader; any household member for a card from the inbox or sent with an API token (Home Assistant or
        a Shortcut, often under one shared user); otherwise the household's managers
        """
        if self._uploaded(job):
            return True
        if job.source in (IngestSource.inbox.value, IngestSource.api.value):
            return True
        return bool(self.user.can_manage_household)

    def _duplicate_of(self, job: RecipeIngestionJob, draft: CardDraft | None) -> RecipeIngestionRecipeRef | None:
        """A group recipe whose slug matches the draft's name: committing would make "Name (1)" (§6.4)"""
        if draft is None or job.status != IngestStatus.ready.value or not draft.name.strip():
            return None
        slug = create_recipe_slug(draft.name)
        if not slug:
            return None
        stmt = sa.select(RecipeModel.id, RecipeModel.slug, RecipeModel.name).where(
            RecipeModel.group_id == self.group_id, RecipeModel.slug == slug
        )
        row = self.session.execute(stmt.limit(1)).one_or_none()
        if row is None or row.id == job.recipe_id:
            return None
        return RecipeIngestionRecipeRef(id=row.id, slug=row.slug, name=row.name)

    @cached_property
    def _household_recipes_public(self) -> bool:
        repos = get_repositories(self.session, group_id=self.group_id, household_id=self.household_id)
        return recipes_public(repos.households.get_one(self.household_id))

    def get_job(self, job_id: UUID) -> RecipeIngestionJobOut:
        """The whole job for the review page, with its permissions and the possible duplicate"""
        job = self.job(job_id)
        draft = _parse_draft(job.draft)
        extraction = _parse_extraction(job.extraction)
        recipes = self._recipe_refs([job.recipe_id] if job.recipe_id else [])
        out = RecipeIngestionJobOut(
            **self._summary_fields(job, recipes),
            draft_version=job.draft_version,
            pages=[] if is_slimmed(job) else [PageOut.from_meta(job.id, page) for page in parse_pages(job.pages)],
            transcription=job.transcription,
            read=extraction.read_info() if extraction else None,
            draft=draft,
            flags=parse_flags(job.flags),
            proposals=_parse_proposals(job.proposals),
            permissions=self._permissions(job),
            duplicate_of=self._duplicate_of(job, draft),
            household_recipes_public=self._household_recipes_public,
            card_photo_default=not self._household_recipes_public,
        )
        return out

    def state_of(self, job: RecipeIngestionJob) -> RecipeIngestionJobState:
        return RecipeIngestionJobState(
            draft_version=job.draft_version,
            status=IngestStatus(job.status),
            task=_task(job),
            proposal_ids=[proposal.id for proposal in _parse_proposals(job.proposals)],
            error=_error(job),
        )

    def get_state(self, job_id: UUID) -> RecipeIngestionJobState:
        """What the review page polls while a task runs"""
        return self.state_of(self.job(job_id))

    def next_ready_job_id(self, batch_id: UUID, after: UUID) -> UUID | None:
        """
        The batch's next `ready` card after `after` in review order (`position`, then arrival), wrapping round to the
        ones before it: what Commit & next opens (§6.1)
        """
        stmt = (
            sa.select(Job.id, Job.status)
            .where(Job.batch_id == batch_id, *self.repos.jobs.scope)
            .order_by(Job.position, Job.created_at, Job.id)
        )
        rows = self.session.execute(stmt).all()
        ids = [row.id for row in rows]
        start = ids.index(after) + 1 if after in ids else 0
        for row in rows[start:] + rows[: max(start - 1, 0)]:
            if row.id != after and row.status == IngestStatus.ready.value:
                return row.id
        return None

    # ==========================================
    # Draft saves

    def save_draft(self, job_id: UUID, update: CardDraftUpdate) -> CardDraftSaved:
        """
        Saves the review page's draft with its flag resolutions, removes the proposals it used or dismissed, and
        recomputes the flags (§6.6). A stale `draft_version` is a 409 `version_conflict`; a change to the row that
        isn't a draft save (a task's proposal) is retried on the server. `draft_version` is bumped only when the draft
        changed (§3.3): a save that only resolves flags or proposals, or dismisses the banner, keeps it, so the draft
        still counts as unedited for a re-extract and another device's next save doesn't conflict. A save that changes
        the draft and uses a proposal that is no longer there (another device settled it) is a 409 too.
        """
        draft = self._parse_text_lines(job_id, _with_unique_ids(update.draft), update.draft_version)
        resolved_proposals = {str(proposal_id) for proposal_id in update.resolved_proposal_ids}
        units = self._unit_names() if resolved_proposals else []

        def mutate(row: RowMapping) -> dict[str, Any] | None:
            # compared as read, so a draft stored by an older schema version (notes without ids) isn't "changed" by
            # the ids and version reading it gives it; the save writes it in the current form either way
            stored = _parse_draft(row["draft"])
            changed = stored is None or _stored_form(draft) != _stored_form(stored)
            if changed and resolved_proposals - {str(p.get("id")) for p in row["proposals"] or []}:
                # it uses a proposal another device already settled, which kept `draft_version`: that proposal (and
                # with it a whole-card reading's flags) is gone, so this is a 409 and the client reloads
                return None

            stored_flags = parse_flags(row["flags"])
            resolutions: dict[str, FlagResolution] = {
                flag.id: flag.resolution for flag in stored_flags if flag.resolution is not None
            }
            for flag_id, resolution in update.flag_resolutions.items():
                if resolution is None:
                    resolutions.pop(flag_id, None)
                else:
                    resolutions[flag_id] = resolution

            extraction = _parse_extraction(row["extraction"])
            transcription = row["transcription"]
            adopted = [p for p in _parse_proposals(row["proposals"]) if str(p.id) in resolved_proposals]
            pages = parse_pages(row["pages"]) if adopted else []
            adopted_flags = _adopted_reading_flags(draft, adopted, extraction, transcription, pages=pages, units=units)
            previous = [*stored_flags, *adopted_flags]
            flags = resolve_flags(draft, extraction, resolutions, transcription=transcription, previous=previous)
            errors, warnings = flag_rules.count_unresolved(flags)
            values: dict[str, Any] = {
                "draft": draft,
                "flags": flags,
                "title": _title(draft),
                "error_count": errors,
                "warning_count": warnings,
                "draft_version": row["draft_version"] + 1 if changed else row["draft_version"],
            }
            if resolved_proposals:
                proposals = row["proposals"] or []
                values["proposals"] = [p for p in proposals if str(p.get("id")) not in resolved_proposals]
            if update.clear_error:
                values["error_code"] = None
                values["error_params"] = None
            return values

        where = [Job.status == IngestStatus.ready.value, Job.draft_version == update.draft_version]
        try:
            written = self.repos.jobs.update_job_json(job_id, mutate, where=where)
        except JobConflict:
            written = None

        if written is None:
            job = self.job(job_id)
            if job.status != IngestStatus.ready.value:
                raise invalid_status(job.status)
            raise version_conflict(job.draft_version)

        return CardDraftSaved(
            draft_version=written.values["draft_version"],
            flags=written.values["flags"],
            error_count=written.values["error_count"],
            warning_count=written.values["warning_count"],
        )

    def _unit_names(self) -> list[str]:
        """The group's unit names for `unit_unclear` (`IngestMatcher.unit_names`), read before a draft's write"""
        repos = get_repositories(self.session, group_id=self.group_id, household_id=self.household_id)
        units = IngestMatcher(repos).unit_names()
        if self.session.in_transaction():
            self.session.commit()  # the write reads the row again: no snapshot stays open meanwhile
        return units

    def _parse_text_lines(self, job_id: UUID, draft: CardDraft, draft_version: int) -> CardDraft:
        """
        `draft` with each line the reviewer wrote as text (`_text_to_parse`) parsed and linked as extraction does (§5):
        the shorthand written out, then the NLP parser with the group's foods and units. A line the parser can't split
        stays as sent, and nothing is parsed on a card that isn't in English, or for a save that will be refused.

        The page keeps the line as it typed it until it reloads, and sends that with its next saves: it differs from
        the stored (parsed) line's note, so it's parsed again into the same line, and the draft doesn't change.
        Blocking: the parse runs here, before the draft's write.
        """
        job = self.repos.jobs.get(job_id)
        if self.session.in_transaction():
            self.session.commit()  # the write reads the row again: no snapshot stays open meanwhile
        if job is None or job.status != IngestStatus.ready.value or job.draft_version != draft_version:
            return draft
        extraction = _parse_extraction(job.extraction)
        language = extraction.language if extraction else None
        if not card_flags.is_english(language):
            return draft

        stored_draft = _parse_draft(job.draft)
        stored = {line.reference_id: line for line in stored_draft.ingredients} if stored_draft else {}
        lines = [
            IngredientLine(text=text, title=ingredient.title, reference_id=ingredient.reference_id)
            for ingredient in draft.ingredients
            if (text := _text_to_parse(ingredient, stored)) is not None
        ]
        if not lines:
            return draft

        repos = get_repositories(self.session, group_id=self.group_id, household_id=self.household_id)
        try:
            parsed = asyncio.run(
                normalize_lines(
                    lines,
                    repos=repos,
                    translator=translator_for(job.locale),
                    matcher=IngestMatcher(repos),
                    language=language,
                )
            )
        except Exception as e:
            # the lines are saved as they were written; the parse is a help, never a reason to lose an edit. No
            # traceback or message: a database error's text holds its parameters, here food names from the card (§10)
            logger.warning(
                f"Couldn't parse the ingredient lines edited on recipe card job {job_id} ({type(e).__qualname__})"
            )
            if self.session.in_transaction():
                self.session.rollback()
            return draft
        by_ref = {line.reference_id: line for line in parsed if _has_parts(line)}
        if not by_ref:
            return draft
        ingredients = [by_ref.get(ingredient.reference_id, ingredient) for ingredient in draft.ingredients]
        return draft.model_copy(update={"ingredients": ingredients})

    # ==========================================
    # Tasks

    def _refuse_enqueue(self, job_id: UUID, wanted: IngestStatus) -> JobActionError:
        """Why a conditional enqueue matched nothing"""
        job = self.job(job_id)
        if job.status != wanted.value:
            return invalid_status(job.status)
        return busy()

    def _queued(self, job_id: UUID) -> RecipeIngestionJobState:
        dispatcher.wake()
        return self.get_state(job_id)

    def reread(self, job_id: UUID, request: RereadRequest) -> RecipeIngestionJobState:
        """Queues a region re-read (§4.7) in the re-read slot; its result arrives as a proposal"""
        job = self.job(job_id)
        if job.status != IngestStatus.ready.value:
            raise invalid_status(job.status)
        if job.task_state is not None:
            raise busy()
        if request.page not in {page.index for page in parse_pages(job.pages)}:
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, UNKNOWN_PAGE)
        self._check_target(_parse_draft(job.draft), request)

        payload = request.model_dump(mode="json")
        where = [Job.status == IngestStatus.ready.value]
        if not self.repos.jobs.enqueue_task(
            job_id, IngestTaskKind.reread, payload, limits.PRIORITY_REREAD, where=where
        ):
            raise self._refuse_enqueue(job_id, IngestStatus.ready)
        return self._queued(job_id)

    @staticmethod
    def _check_target(draft: CardDraft | None, request: RereadRequest) -> None:
        """
        An ingredient or step target names a line the draft has by its `ref`, or none for a new line (a line the
        reading missed, or the first of an empty section): the review page adds the reading as one.
        """
        target = request.target
        field = target.field.strip()
        if not field or len(field) > 64:
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, UNKNOWN_TARGET)

        refs: set[str] | None = None
        if field == "ingredients":
            refs = {str(i.reference_id) for i in draft.ingredients} if draft else set()
        elif field == "steps":
            refs = {str(s.id) for s in draft.steps} if draft else set()
        if refs is not None and target.ref is not None and target.ref not in refs:
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, UNKNOWN_TARGET)

    def reextract(self, job_id: UUID) -> RecipeIngestionJobState:
        """
        Reads the whole card again (§3.1): it replaces a draft nobody edited, and becomes a whole-card proposal on an
        edited one
        """
        job = self.job(job_id)
        if job.status != IngestStatus.ready.value:
            raise invalid_status(job.status)
        if job.task_state is not None:
            raise busy()

        where = [Job.status == IngestStatus.ready.value]
        if not self.repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT, where=where):
            raise self._refuse_enqueue(job_id, IngestStatus.ready)
        return self._queued(job_id)

    def retry(self, job_id: UUID) -> RecipeIngestionJobState:
        """A failed first extraction goes back to `processing` with a fresh extract task"""
        job = self.job(job_id)
        if job.status != IngestStatus.failed.value:
            raise invalid_status(job.status)

        queued = self.repos.jobs.enqueue_task(
            job_id,
            IngestTaskKind.extract,
            None,
            limits.PRIORITY_EXTRACT,
            where=[Job.status == IngestStatus.failed.value],
            values={"status": IngestStatus.processing.value, "error_code": None, "error_params": None},
        )
        if not queued:
            raise self._refuse_enqueue(job_id, IngestStatus.failed)
        return self._queued(job_id)

    def read_with_cloud(self, job_id: UUID) -> RecipeIngestionJobState:
        """
        Reads a failed local-only card again, this time with any of the group's providers (`can_read_with_cloud`): one
        update clears the card's `local_only` and queues the retry, conditional on the card still being in that state.
        The worker still applies the group's setting when the task starts, so a switch-on meanwhile keeps it local.
        """
        job = self.job(job_id)
        if not (self._uploaded(job) or self.user.can_manage_household):
            raise JobActionError(status.HTTP_403_FORBIDDEN, FORBIDDEN)
        if self._group_local_only:
            raise JobActionError(status.HTTP_409_CONFLICT, GROUP_LOCAL_ONLY)
        if not self._cloud_readable(job):
            raise invalid_status(job.status)

        where = [
            Job.status == IngestStatus.failed.value,
            Job.error_code == IngestErrorCode.local_only_unavailable.value,
            Job.local_only.is_(True),
        ]
        values = {
            "status": IngestStatus.processing.value,
            "error_code": None,
            "error_params": None,
            "local_only": False,
        }
        queued = self.repos.jobs.enqueue_task(
            job_id, IngestTaskKind.extract, None, limits.PRIORITY_EXTRACT, where=where, values=values
        )
        if not queued:
            current = self.job(job_id)
            if current.task_state is not None and current.status == IngestStatus.failed.value:
                raise busy()
            raise invalid_status(current.status)
        return self._queued(job_id)

    def cancel(self, job_id: UUID) -> RecipeIngestionJobState:
        """
        §3.5: a queued task is cleared (a `processing` job fails with `cancelled`, a `ready` one stays ready); a
        running one is asked to stop, which it does within a heartbeat
        """
        self.job(job_id)
        self.repos.jobs.cancel_task(job_id)
        return self.get_state(job_id)

    # ==========================================
    # Files (the caller holds the ingest write lock)

    def rotate(self, job_id: UUID, index: int, degrees: int) -> PageOut:
        """
        Turns one page clockwise (§4.4): rewrites its files, then stores its new metadata conditional on the job still
        having no task. 409 `busy` while a task is active. The caller holds the ingest write lock.
        """
        job = self.job(job_id)
        pages = {page.index: page for page in parse_pages(job.pages)}
        if index not in pages:
            raise not_found()
        if job.task_state is not None:
            raise busy()
        if job.status not in (IngestStatus.ready.value, IngestStatus.failed.value):
            raise invalid_status(job.status)

        before = pages[index]
        page_dir = storage.page_dir(self.group_id, job_id, index)
        try:
            turned = images.rotate_page_files(page_dir, before, degrees, PageRotationSource.user)
        except FileNotFoundError as e:
            raise not_found() from e

        def mutate(row: RowMapping) -> dict[str, Any] | None:
            stored = [dict(page) for page in row["pages"] or []]
            for position, page in enumerate(stored):
                if page.get("index") == index:
                    stored[position] = turned.model_dump(mode="json")
                    return {"pages": stored}
            return None

        where = [
            Job.task_state.is_(None),
            Job.status.in_([IngestStatus.ready.value, IngestStatus.failed.value]),
        ]
        try:
            written = self.repos.jobs.update_job_json(job_id, mutate, where=where)
        except JobConflict:
            written = None

        if written is None:
            # a task started (or the job went) while the files were being turned: turn them back
            try:
                images.rotate_page_files(page_dir, turned, (360 - degrees) % 360, before.rotation_source)
            except FileNotFoundError:
                raise not_found() from None
            raise self._refuse_enqueue(job_id, IngestStatus(job.status))

        return PageOut.from_meta(job_id, turned)

    def discard(self, job_id: UUID) -> None:
        """
        Deletes the job's row and its files (§3.1, §9): the uploader, anyone for an inbox card, otherwise the
        household's managers. Deleting the row clears any task with it, so a running one stops within a heartbeat.
        The caller holds the ingest write lock.
        """
        job = self.job(job_id)
        if not self.can_discard(job):
            raise JobActionError(status.HTTP_403_FORBIDDEN, FORBIDDEN)
        discardable = [IngestStatus.processing.value, IngestStatus.ready.value, IngestStatus.failed.value]
        if job.status not in discardable:
            raise invalid_status(job.status)

        if not self.repos.jobs.delete(job_id, where=[Job.status.in_(discardable)]):
            current = self.job(job_id)
            raise invalid_status(current.status)
        storage.remove_job_dir(self.group_id, job_id)

    def merge(self, job_id: UUID, into_job_id: UUID) -> RecipeIngestionJobState:
        """
        Adds a card's photos to another card of the household as its next pages (a back sent as a card of its own),
        deletes the card, and reads the other one again: an unedited draft is replaced, an edited one gets a proposal.
        Both must be ready or failed with no task, the user must have uploaded both or manage the household, and the
        pages must fit in one card. The files move first, then one transaction writes the target and deletes the
        source, fenced on both rows' `row_version`; when that matches nothing the files move back. The caller holds
        the ingest write lock.
        """
        if job_id == into_job_id:
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, SAME_CARD)
        source, target = self.job(job_id), self.job(into_job_id)
        movable = (IngestStatus.ready.value, IngestStatus.failed.value)
        for job in (source, target):
            if not (self._uploaded(job) or self.user.can_manage_household):
                raise JobActionError(status.HTTP_403_FORBIDDEN, FORBIDDEN)
        for job in (source, target):
            if job.status not in movable:
                raise invalid_status(job.status)
            if job.task_state is not None:
                raise busy()
        source_pages, target_pages = parse_pages(source.pages), parse_pages(target.pages)
        if len(source_pages) + len(target_pages) > limits.MAX_PAGES_PER_CARD:
            raise JobActionError(status.HTTP_409_CONFLICT, TOO_MANY_PAGES, max=limits.MAX_PAGES_PER_CARD)

        first = max((page.index for page in target_pages), default=-1) + 1
        moves = [
            (
                storage.page_dir(self.group_id, job_id, page.index),
                storage.page_dir(self.group_id, into_job_id, first + offset),
                page.model_copy(update={"index": first + offset}),
            )
            for offset, page in enumerate(source_pages)
        ]
        if not all(origin.is_dir() for origin, _, _ in moves) or any(dest.exists() for _, dest, _ in moves):
            raise JobActionError(status.HTTP_409_CONFLICT, FILES_MISSING)

        done: list[tuple[Path, Path]] = []
        try:
            for origin, dest, _ in moves:
                os.rename(origin, dest)
                done.append((origin, dest))
            merged = [*target_pages, *(page for _, _, page in moves)]
            written = self._write_merge(source, target, merged)
        except BaseException:
            self._move_back(done)
            raise
        if not written:
            self._move_back(done)
            current = self.repos.jobs.get(into_job_id), self.repos.jobs.get(job_id)
            if any(job is not None and job.task_state is not None for job in current):
                raise busy()
            raise invalid_status(next((job.status for job in current if job is not None), IngestStatus.ready.value))

        storage.remove_job_dir(self.group_id, job_id)
        return self._queued(into_job_id)

    def _write_merge(self, source: RecipeIngestionJob, target: RecipeIngestionJob, pages: list[PageMeta]) -> bool:
        """
        One transaction: the target gets the pages and an extract task (a failed one goes back to `processing`), and
        the source is deleted, each only if its `row_version`, status and idle task are as read. Whether both happened.
        """
        movable = [IngestStatus.ready.value, IngestStatus.failed.value]
        failed = target.status == IngestStatus.failed.value
        values: dict[str, Any] = {
            "pages": [page.model_dump(mode="json") for page in pages],
            "source_sha256": source_sha256(pages),
        }
        if failed:
            values |= {"status": IngestStatus.processing.value, "error_code": None, "error_params": None}
        where = [Job.row_version == target.row_version, Job.status == target.status]
        try:
            queued = enqueue_task(
                self.session,
                target.id,
                self.household_id,
                IngestTaskKind.extract,
                None,
                limits.PRIORITY_EXTRACT,
                where=where,
                values=values,
                commit=False,
            )
            deleted = queued and self.session.execute(
                sa.delete(Job).where(
                    Job.id == source.id,
                    *self.repos.jobs.scope,
                    Job.row_version == source.row_version,
                    Job.status.in_(movable),
                    Job.task_state.is_(None),
                ),
                execution_options={"synchronize_session": False},
            )
            if not queued or getattr(deleted, "rowcount", 0) != 1:
                self.session.rollback()
                return False
        except BaseException:
            self.session.rollback()
            raise
        self.session.commit()
        return True

    @staticmethod
    def _move_back(done: Sequence[tuple[Path, Path]]) -> None:
        for origin, dest in reversed(done):
            try:
                os.rename(dest, origin)
            except OSError:
                logger.error("Couldn't move a merged card's page back; the card may be missing a page")

    def page_image(self, job_id: UUID, index: int, kind: str) -> PageImage:
        """One of a page's images, after the household check (§9)"""
        job = self.job(job_id)
        pages = {page.index: page for page in parse_pages(job.pages)}
        if index not in pages or kind not in PAGE_MEDIA_TYPES or is_slimmed(job):
            raise not_found()

        page = pages[index]
        path = images.page_file(storage.page_dir(self.group_id, job_id, index), kind)
        try:
            stat = path.stat()
        except FileNotFoundError as e:
            raise not_found() from e

        version = page.page_sha256[:12]
        return PageImage(
            path=path,
            media_type=PAGE_MEDIA_TYPES[kind],
            etag=f'"{page.page_sha256[:20]}-r{page.rotation}-{kind}"',
            version=version,
            stat=stat,
        )
