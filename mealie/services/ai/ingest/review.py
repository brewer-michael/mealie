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

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
from mealie.repos.repository_recipe_ingest import IngestRepos, JobConflict
from mealie.schema.recipe.recipe import create_recipe_slug
from mealie.schema.recipe_ingest import (
    CardDraft,
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
from mealie.services.ai.ingest.pipeline import flags as card_flags
from mealie.services.ai.ingest.runner.dispatcher import dispatcher

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
UNKNOWN_PAGE = "unknown_page"
"""422: a re-read names a page the card doesn't have"""
UNKNOWN_TARGET = "unknown_target"
"""422: a re-read targets an ingredient or step the draft doesn't have"""


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
) -> list[CardFlag]:
    """
    The flags of each whole-card proposal (a re-extract of an edited draft) that `draft` took up, computed as the
    re-extract computed them: against the job's transcription and extraction, which are that reading's. A save that
    accepts the proposal passes them as `previous`, so the new reading's reading flags are raised on the draft it
    became (its ingredient and step ids are new, so none of the stored flags matches them). Accepting keeps the
    proposal's ingredient and step ids; a dismissed one shares none with the draft.
    """
    flags: list[CardFlag] = []
    ids = {ingredient.reference_id for ingredient in draft.ingredients} | {step.id for step in draft.steps}
    for proposal in proposals:
        proposed = proposal.draft
        if proposal.kind != CardProposalKind.full or proposed is None:
            continue
        proposed_ids = {ingredient.reference_id for ingredient in proposed.ingredients}
        proposed_ids |= {step.id for step in proposed.steps}
        if ids & proposed_ids or (not proposed_ids and proposed == draft):
            flags.extend(card_flags.compute_flags(proposed, extraction, {}, transcription=transcription))
    return flags


def _stored_form(draft: CardDraft) -> Any:
    """The draft as the `draft` column stores it (and reads it back)"""
    return to_jsonable_python(draft, by_alias=False, inf_nan_mode="null")


def _with_unique_ids(draft: CardDraft) -> CardDraft:
    """
    Flags are keyed to ingredient `reference_id`s and step ids, so a draft holding a duplicate (a pasted row) gets a
    fresh id for each repeat
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

    if ingredients == draft.ingredients and steps == draft.steps:
        return draft
    return draft.model_copy(update={"ingredients": ingredients, "steps": steps})


def _fresh_id(taken: set[UUID]) -> UUID:
    new_id = uuid4()
    while new_id in taken:
        new_id = uuid4()
    return new_id


def _title(draft: CardDraft) -> str | None:
    name = draft.name.strip()
    return name[:255] if name else None


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
            "local_only": job.local_only,
            "created_at": job.created_at,
        }

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
            can_discard=self.can_discard(job),
            can_export_eval=bool(self.user.can_manage),
        )

    def can_discard(self, job: RecipeIngestionJob) -> bool:
        """§9: the uploader; anyone for inbox cards; otherwise the household's managers"""
        if job.created_by is not None and job.created_by == self.user.id:
            return True
        if job.source == IngestSource.inbox.value:
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

    def _household_recipes_public(self) -> bool:
        repos = get_repositories(self.session, group_id=self.group_id, household_id=self.household_id)
        household = repos.households.get_one(self.household_id)
        return bool(household and household.preferences and household.preferences.recipe_public)

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
            household_recipes_public=self._household_recipes_public(),
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
        still counts as unedited for a re-extract and another device's next save doesn't conflict.
        """
        draft = _with_unique_ids(update.draft)
        resolved_proposals = {str(proposal_id) for proposal_id in update.resolved_proposal_ids}

        def mutate(row: RowMapping) -> dict[str, Any]:
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
            previous = [*stored_flags, *_adopted_reading_flags(draft, adopted, extraction, transcription)]
            flags = resolve_flags(draft, extraction, resolutions, transcription=transcription, previous=previous)
            errors, warnings = flag_rules.count_unresolved(flags)
            changed = _stored_form(draft) != row["draft"]
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
        target = request.target
        field = target.field.strip()
        if not field or len(field) > 64:
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, UNKNOWN_TARGET)

        refs: set[str] | None = None
        if field == "ingredients":
            refs = {str(i.reference_id) for i in draft.ingredients} if draft else set()
        elif field == "steps":
            refs = {str(s.id) for s in draft.steps} if draft else set()
        if refs is not None and (target.ref is None or target.ref not in refs):
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
