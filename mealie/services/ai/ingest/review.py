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

**Turning a page is crash-safe (§4.4).** `rotate` stages the turned files beside the page's (`images.stage_rotation`),
stores the new metadata, then swaps the staged files in (`images.apply_staged`); a refused write discards them. A stop
in between leaves the stored metadata naming either the files in place or the staged ones, and whatever reads the
page's files next settles it first (`settle_turns`): its image, another turn, a commit and an eval export. All of
that holds the page's turn lock (`page_turn_lock`), so settling never discards a turn that's still being stored, and
two turns of one page both land.
"""

import asyncio
import errno
import fcntl
import json
import math
import os
import re
import shutil
import threading
import time
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
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
from rapidfuzz import fuzz
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from mealie.core.exceptions import SlugError
from mealie.core.root_logger import get_logger
from mealie.db.models.recipe.recipe import RecipeModel
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.db.models.users.users import User
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_factory import AllRepositories
from mealie.repos.repository_recipe_ingest import IngestRepos, JobConflict, JobOrder, enqueue_task, title_key
from mealie.schema.household.household import HouseholdInDB
from mealie.schema.recipe.recipe import create_recipe_slug
from mealie.schema.recipe.recipe_ingredient import CreateIngredientFood, CreateIngredientUnit, RecipeIngredient
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftSaved,
    CardDraftUpdate,
    CardFlag,
    CardFlagKind,
    CardFlagSeverity,
    CardProposal,
    CardProposalKind,
    ExtractionMeta,
    FlagResolution,
    IngestErrorCode,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskMode,
    IngestTaskState,
    PageMeta,
    PageOut,
    PageRotationSource,
    ProposalTarget,
    RecipeIngestionJobCounts,
    RecipeIngestionJobError,
    RecipeIngestionJobOut,
    RecipeIngestionJobPagination,
    RecipeIngestionJobPermissions,
    RecipeIngestionJobRef,
    RecipeIngestionJobState,
    RecipeIngestionJobSummary,
    RecipeIngestionJobTask,
    RecipeIngestionRecipeRef,
    RegionHintOut,
    RereadRequest,
)
from mealie.schema.user.user import PrivateUser
from mealie.services.ai.errors import IngestPaused
from mealie.services.ai.ingest import flag_rules, images, limits, retention, storage, tasks
from mealie.services.ai.ingest.eval_export import EXPORTABLE_STATUSES
from mealie.services.ai.ingest.i18n import translator_for
from mealie.services.ai.ingest.intake import lock_household_intake, source_sha256
from mealie.services.ai.ingest.matching import IngestMatcher
from mealie.services.ai.ingest.pipeline import flags as card_flags
from mealie.services.ai.ingest.pipeline.cardtext import MARKER_RE, canonical_markers, markers_in
from mealie.services.ai.ingest.pipeline.ingredients import IngredientLine, normalize_lines
from mealie.services.ai.ingest.pipeline.regions import region_hint
from mealie.services.ai.ingest.pipeline.reread import field_name
from mealie.services.ai.ingest.runner.dispatcher import dispatcher
from mealie.services.ai.ingest.shorthand import QTY

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

NEAR_NAME_RATIO = 90
"""
A household recipe whose name is at least this like the draft's (rapidfuzz `ratio` of the two as `title_key` compares
names) is a possible duplicate: "Bananna Bread" for "Banana Bread", not "Banana Bread Muffins"
"""
NAME_SUFFIXES = 10
"""How many "Name (n)" one look for a free recipe name checks at a time (`suffixed_name`)"""
MAX_NAME_SUFFIX = 1000
"""
The highest "Name (n)" commit gives a recipe: with every one taken, the card's name must change first. Upstream's
create only tries "(1)" to "(9)" itself, so commit picks the name before it.
"""


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
    kind = IngestTaskKind(job.task_kind)
    mode, refs = _task_mode(job.task_payload) if kind == IngestTaskKind.extract else (None, [])
    return RecipeIngestionJobTask(
        kind=kind,
        state=IngestTaskState(job.task_state),
        mode=mode,
        refs=refs,
        progress_key=job.progress_key,
        cancel_requested=job.cancel_requested,
    )


def _task_mode(payload: Any) -> tuple[IngestTaskMode | None, list[str]]:
    """
    What an extract task does, by its `task_payload` as the runner reads it (no mode: the whole card is read), and the
    lines a `parse_lines` task parses, so a page loaded while it runs (a reload, another device) can say so; never the
    payload's text. An unknown mode is None.
    """
    raw = payload.get("mode") if isinstance(payload, dict) else None
    try:
        mode = IngestTaskMode(raw) if raw is not None else IngestTaskMode.reextract
    except ValueError:
        return None, []
    refs: list[str] = []
    if mode == IngestTaskMode.parse_lines and isinstance(lines := payload.get("lines"), list):
        refs = [line["ref"] for line in lines if isinstance(line, dict) and isinstance(line.get("ref"), str)]
    return mode, refs


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
    linked: Mapping[UUID, Collection[str]] | None = None,
) -> list[CardFlag]:
    """
    The draft's flags with the reviewer's resolutions applied (§4.6), as stored and returned on every save. Only
    errors of the kinds that can be kept as written can be `kept`, and only warnings can be `dismissed`; anything else
    is dropped, as are resolutions of flags the draft no longer raises.

    `transcription` and `previous` are the job's stored transcription and flags, as `compute_flags` takes them on a
    save: the checks against what the card says (`not_on_card`, `marker_dropped`) are made again, and reading flags
    are kept only where they were raised before, so the reviewer's own edits never raise one. `linked` is every name
    of the foods and units the draft links (`IngestMatcher.linked_names`), so a line parsed on the save, or taken
    from a proposal, is checked for a link that isn't an exact name match (`linked_fuzzy`) like an extracted one.
    """
    flags = card_flags.compute_flags(
        draft, extraction, resolutions, transcription=transcription, previous=previous, linked=linked
    )
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
    matches them). Accepting keeps the proposal's ingredient, step and note ids; a dismissed one shares none with the
    draft.
    """
    flags: list[CardFlag] = []
    units = list(units)
    read_path = extraction.read_path if extraction else None
    ocr_lines = card_flags.ocr_check_lines([page.ocr for page in pages], read_path, transcription)
    ids = {ingredient.reference_id for ingredient in draft.ingredients} | {step.id for step in draft.steps}
    ids |= {note.id for note in draft.notes}
    for proposal in proposals:
        proposed = proposal.draft
        if proposal.kind != CardProposalKind.full or proposed is None:
            continue
        proposed_ids = {ingredient.reference_id for ingredient in proposed.ingredients}
        proposed_ids |= {step.id for step in proposed.steps} | {note.id for note in proposed.notes}
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


# ==========================================
# Lines kept as written with a marker (§4.6)


AMOUNT_STAND_IN = "1"
"""
Stands for an amount the card leaves blank or unreadable while the line is parsed: card shorthand is read after an
amount only ("[blank] C. sugar" is parsed as "1 C. sugar"), and the amount is dropped again
"""

_LEADING_AMOUNT = re.compile(rf"^{QTY}")


def kept_flag_ids(flags: Iterable[CardFlag], changes: Mapping[str, FlagResolution | None] | None = None) -> set[str]:
    """The ids of the flags resolved `kept`, as stored, with a save's resolution `changes` applied"""
    kept = {flag.id for flag in flags if flag.resolution == FlagResolution.kept}
    for flag_id, resolution in (changes or {}).items():
        if resolution == FlagResolution.kept:
            kept.add(flag_id)
        else:
            kept.discard(flag_id)
    return kept


@dataclass(frozen=True)
class KeptLine:
    """
    A text-only ingredient line whose every marker the reviewer kept as written ("1 C. [illegible]"), ready for the
    parser: it reads the line without its markers, and they go back into the parsed line's note, which commit
    writes out ("(unreadable)", "___"). The line keeps its flags and their resolutions, so keeping can be taken back.
    """

    text: str
    """The line as written, its markers in their canonical form"""
    parse_text: str
    """What the parser reads: the line without its markers (`AMOUNT_STAND_IN` for one standing for the amount)"""
    markers: tuple[str, ...]
    amount_marker: bool
    """A marker led the line, where its amount goes: the parsed amount is the stand-in's"""

    def ingredient(self, parsed: CardDraftIngredient) -> CardDraftIngredient | None:
        """The parsed line with its markers back in its note; None when the parser read no amount, unit or food"""
        quantity = None if self.amount_marker else parsed.quantity
        unit = parsed.unit if parsed.unit and parsed.unit.name.strip() else None
        food = parsed.food if parsed.food and parsed.food.name.strip() else None
        if quantity is None and unit is None and food is None:
            return None

        note = ", ".join(part for part in (" ".join(self.markers), parsed.note.strip()) if part)
        ingredient = parsed.model_copy(update={"original_text": self.text, "quantity": quantity, "note": note})
        # as the recipe will read: a unit whose amount is the marker is named after it (`amount_marker_note`)
        marker_note = amount_marker_note(ingredient, unit.name.strip()) if unit else None
        display = RecipeIngredient(
            quantity=quantity,
            unit=CreateIngredientUnit(name=unit.name) if unit and marker_note is None else None,
            food=CreateIngredientFood(name=food.name) if food else None,
            note=marker_note or note,
        ).display
        ingredient.display = display or self.text
        # as read: its flags are a parsed line's, against the line as written (`flags.ingredient_line`)
        ingredient.extracted_hash = card_flags.ingredient_hash(ingredient)
        return ingredient


def amount_marker_note(line: CardDraftIngredient, unit: str) -> str | None:
    """
    For a line whose amount is a marker ("[blank] C. sugar", as `KeptLine` parses it: the marker leads the line as read
    and its note, and the line has a unit but no amount), its note with `unit` (the unit as the recipe names it) right
    after that marker: "[blank] cup". Upstream's recipe page shows a unit only with an amount
    (`RecipeIngredientBase._format_display`, the frontend's `useParsedIngredientText`), so commit puts it there and
    links no unit: the recipe reads "sugar ___ cup". None for any other line.
    """
    note = line.note.strip()
    marker = MARKER_RE.match(note)
    if (
        marker is None
        or line.quantity
        or not unit.strip()
        or line.unit is None
        or not line.unit.name.strip()
        or MARKER_RE.search(line.unit.name)
        or not MARKER_RE.match(line.original_text.strip())
    ):
        return None
    return f"{note[: marker.end()]} {unit.strip()}{note[marker.end() :]}"


def kept_line(ingredient: CardDraftIngredient, kept: Collection[str]) -> KeptLine | None:
    """
    The line as a `KeptLine` when it has no amount, unit or food, holds a marker, and every marker on it is resolved
    kept (the flag ids in `kept`); None otherwise, or when nothing but markers is written on it
    """
    if _has_parts(ingredient):
        return None
    text = canonical_markers(ingredient.note.strip())
    markers = tuple(match.group(0) for match in MARKER_RE.finditer(text))
    if not markers:
        return None
    ref = str(ingredient.reference_id)
    kinds = {*markers_in(text), *markers_in(ingredient.title)}
    if any(card_flags.flag_id(CardFlagKind(kind), card_flags.FIELD_INGREDIENTS, ref) not in kept for kind in kinds):
        return None

    rest = " ".join(MARKER_RE.sub(" ", text).split())
    if not any(character.isalnum() for character in rest):
        return None
    amount_marker = MARKER_RE.match(text) is not None and not _LEADING_AMOUNT.match(rest)
    parse_text = f"{AMOUNT_STAND_IN} {rest}" if amount_marker else rest
    return KeptLine(text=text, parse_text=parse_text, markers=markers, amount_marker=amount_marker)


def parse_written_lines(
    draft: CardDraft,
    chosen: Mapping[UUID, str | KeptLine],
    *,
    repos: AllRepositories,
    locale: str | None,
    language: str | None,
    job_id: UUID,
) -> tuple[CardDraft, list[CardDraftIngredient]]:
    """
    `draft` with the `chosen` lines (by `reference_id`: a line's text, or a `KeptLine`) parsed and linked as extraction
    does (§5): the shorthand written out, then the parser with the group's foods and units (`normalize_lines`; only the
    NLP parser runs, for a card in English or of unknown language). A line the parser can't split stays as it is. Also
    the lines that changed, as they stand in the new draft.

    The parse is a help, never a reason to lose an edit or stop a commit: when it fails, a warning naming the job and
    the error's type is logged, and nothing changes. Blocking: call it from a worker thread.
    """
    lines = [
        IngredientLine(
            text=choice.parse_text if isinstance(choice, KeptLine) else choice,
            title=ingredient.title,
            reference_id=ingredient.reference_id,
        )
        for ingredient in draft.ingredients
        if (choice := chosen.get(ingredient.reference_id)) is not None
    ]
    if not lines:
        return draft, []

    try:
        parsed = asyncio.run(
            normalize_lines(
                lines,
                repos=repos,
                translator=translator_for(locale),
                matcher=IngestMatcher(repos),
                language=language,
            )
        )
    except Exception as e:
        # no traceback or message: a database error's text holds its parameters, here food names from the card (§10)
        logger.warning(
            f"Couldn't parse the ingredient lines written on recipe card job {job_id} ({type(e).__qualname__})"
        )
        if repos.session.in_transaction():
            repos.session.rollback()
        return draft, []

    by_ref: dict[UUID, CardDraftIngredient] = {}
    for line in parsed:
        choice = chosen.get(line.reference_id)
        result = choice.ingredient(line) if isinstance(choice, KeptLine) else line if _has_parts(line) else None
        if result is not None:
            by_ref[line.reference_id] = result
    if not by_ref:
        return draft, []
    ingredients = [by_ref.get(ingredient.reference_id, ingredient) for ingredient in draft.ingredients]
    changed = [line for line in ingredients if line.reference_id in by_ref]
    return draft.model_copy(update={"ingredients": ingredients}), changed


def parse_kept_lines(repos: AllRepositories, job: RecipeIngestionJob, draft: CardDraft) -> CardDraft:
    """
    At commit (§7): the draft's text-only lines whose every marker was kept as written (by the job's stored flags),
    parsed around their markers as the save that keeps them parses them (`KeptLine`), for a draft whose markers were
    kept before saves did that, and for lines a save couldn't split. A card in another language keeps them as text, as
    its other text lines. Blocking.
    """
    extraction = _parse_extraction(job.extraction)
    language = extraction.language if extraction else None
    if not card_flags.is_english(language):
        return draft
    kept = kept_flag_ids(parse_flags(job.flags))
    chosen: dict[UUID, str | KeptLine] = {
        ingredient.reference_id: line for ingredient in draft.ingredients if (line := kept_line(ingredient, kept))
    }
    return parse_written_lines(draft, chosen, repos=repos, locale=job.locale, language=language, job_id=job.id)[0]


def _target_text(draft: CardDraft, target: ProposalTarget) -> str | None:
    """
    What the card says for a re-read's target: an ingredient's line as read (`original_text`; as it reads now for a
    line the reviewer added), a step's or note's text (a note by its id, or by its place for a client from before note
    ids), or a single field's; None for a line the draft hasn't, or an empty field
    """
    field = field_name(target.field.strip())
    text: str | None = None
    if field in card_flags.DRAFT_TEXT_FIELDS:
        text = getattr(draft, card_flags.DRAFT_TEXT_FIELDS[field])
    elif field == card_flags.FIELD_INGREDIENTS:
        line = next((line for line in draft.ingredients if str(line.reference_id) == target.ref), None)
        text = (line.original_text.strip() or card_flags.ingredient_line(line)) if line else None
    elif field == card_flags.FIELD_STEPS:
        text = next((step.text for step in draft.steps if str(step.id) == target.ref), None)
    elif field == card_flags.FIELD_NOTES and target.ref:
        note = next((note for note in draft.notes if str(note.id) == target.ref), None)
        if note is None and target.ref.isdigit() and int(target.ref) < len(draft.notes):
            note = draft.notes[int(target.ref)]
        text = note.text if note else None
    return text.strip() if text and text.strip() else None


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
    New recipes in the household can be seen without a login now, so a card photo on one could be too: upstream's
    explore routes need both a household that isn't private and recipes created public. What the review page's warning
    says (`household_recipes_public`); the photo switches' defaults follow `recipes_created_public`.
    """
    preferences = household.preferences if household else None
    return bool(preferences and not preferences.private_household and preferences.recipe_public)


def recipes_created_public(household: HouseholdInDB | None) -> bool:
    """
    The household's new recipes are created public (`recipe_public`, which commit copies into the recipe's settings),
    so a card photo on one is seen without a login once the household isn't private, now or later: the explore routes
    check the household when a recipe is read, and nothing makes a recipe private again when the household changes.
    Upstream creates a private household with `recipe_public` off (a new install's included), so the card photo and
    cover are on by default there.
    """
    preferences = household.preferences if household else None
    return bool(preferences and preferences.recipe_public)


def _slug(name: str) -> str | None:
    """The slug a recipe named `name` gets (`create_recipe_slug`); None for a name without one ("!!!")"""
    try:
        return create_recipe_slug(name)
    except SlugError:
        return None


def _recipe_name(name: str, locale: str | None) -> str:
    """The name commit gives the recipe: the draft's, its kept markers written out in the job's language"""
    name = name.strip()
    if not markers_in(name):
        return name
    from .commit import convert_markers  # imported here: commit builds on this module

    return convert_markers(name, translator_for(locale).t("recipe-ingest.unreadable")).strip()


def _slugs_taken(session: Session, group_id: UUID, slugs: Collection[str]) -> set[str]:
    """Which of `slugs` a recipe of the group has (slugs are unique in a group)"""
    if not slugs:
        return set()
    stmt = sa.select(RecipeModel.slug).where(RecipeModel.group_id == group_id, RecipeModel.slug.in_(set(slugs)))
    return set(session.execute(stmt).scalars())


def suffixed_name(session: Session, group_id: UUID, name: str) -> str | None:
    """
    The first "Name (n)" whose slug no recipe of the group has, as upstream's create numbers a taken name: what
    commit names the recipe while `name`'s slug is taken. None when every one up to `MAX_NAME_SUFFIX` is.
    """
    for start in range(1, MAX_NAME_SUFFIX + 1, NAME_SUFFIXES):
        numbers = range(start, min(start + NAME_SUFFIXES, MAX_NAME_SUFFIX + 1))
        candidates = {candidate: _slug(candidate) for candidate in (f"{name} ({number})" for number in numbers)}
        taken = _slugs_taken(session, group_id, {slug for slug in candidates.values() if slug})
        free = next((candidate for candidate, slug in candidates.items() if slug and slug not in taken), None)
        if free is not None:
            return free
    return None


def free_recipe_name(session: Session, group_id: UUID, name: str) -> str | None:
    """
    The name commit gives a recipe called `name` (§7): `name` while its slug is free in the group, else the first
    free "Name (n)" (`suffixed_name`), which the review page announces (`duplicate_name`). None for a name without a
    slug, or with every "Name (n)" taken.
    """
    slug = _slug(name)
    if slug is None:
        return None
    if not _slugs_taken(session, group_id, {slug}):
        return name
    return suffixed_name(session, group_id, name)


def _near_name_lengths(key: str) -> tuple[int, int]:
    """
    The name lengths that can reach `NEAR_NAME_RATIO` against `key`, widened for spacing and case folding that
    `title_key` changes: a filter for the database, which the ratio then decides
    """
    share = NEAR_NAME_RATIO / (200 - NEAR_NAME_RATIO)  # the shorter name's least share of the longer one's length
    return max(math.floor(len(key) * share) - 2, 1), math.ceil(len(key) / share) + 8


@dataclass(frozen=True)
class PossibleDuplicates:
    """What the review page's possible-duplicate banner shows for a card being reviewed (§6.4)"""

    recipe: RecipeIngestionRecipeRef | None = None
    """
    A group recipe whose slug the draft's name would get (commit names the recipe `name` then), else the household's
    recipe with the most similar name
    """
    job: RecipeIngestionJobRef | None = None
    """Another card of the household, waiting or being read, with the same name"""
    name: str | None = None
    """The name commit would give the recipe while `recipe` holds the slug: the first free "Name (n)" """


def attaches_card_photo(draft: CardDraft, household: HouseholdInDB | None) -> bool:
    """
    Whether commit attaches the card's photos to the recipe: the draft's switch, else the household's default, which
    keeps them off recipes created public (`recipes_created_public`: assets are served without a login)
    """
    if draft.attach_card_photo is not None:
        return draft.attach_card_photo
    return not recipes_created_public(household)


def uses_card_as_cover(draft: CardDraft, household: HouseholdInDB | None) -> bool:
    """
    Whether commit makes the front of the card the recipe's image: the draft's switch, else the household's default,
    which keeps the card off recipes created public (`recipes_created_public`: the image is served without a login, as
    the assets are), whether the card was reviewed or added with its batch's clean cards
    """
    if draft.use_card_as_cover is not None:
        return draft.use_card_as_cover
    return not recipes_created_public(household)


# ==========================================
# A page's turn lock, and settling a turn a stop left staged (§4.4)


TURN_LOCK_FILE = ".turn.lock"
"""The empty file in a page's directory whose `flock` is the page's turn lock; it stays once made"""
TURN_LOCK_WAIT = 30.0
"""Seconds to wait for another turn of the same page to end (a turn takes a second or two)"""
_TURN_LOCK_POLL = 0.02

_UNSUPPORTED_LOCK_ERRORS = frozenset({errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS})
"""What `flock` raises where the filesystem has no locks (as `storage` treats them)"""

_turn_gates: dict[str, tuple[threading.Lock, int]] = {}
"""This process's lock for each page being turned or settled, by directory, and how many threads want it"""
_turn_gates_guard = threading.Lock()
_turn_lock_warned = False


@contextmanager
def _page_gate(key: str) -> Iterator[threading.Lock]:
    """The page's lock in this process, kept while any of its threads waits for it or holds it"""
    with _turn_gates_guard:
        entry = _turn_gates.get(key)
        gate = entry[0] if entry else threading.Lock()
        _turn_gates[key] = (gate, (entry[1] if entry else 0) + 1)
    try:
        yield gate
    finally:
        with _turn_gates_guard:
            held, users = _turn_gates[key]
            if users > 1:
                _turn_gates[key] = (held, users - 1)
            else:
                del _turn_gates[key]


def _warn_turn_lock_unsupported(error: OSError) -> None:
    global _turn_lock_warned
    with _turn_gates_guard:
        if _turn_lock_warned:
            return
        _turn_lock_warned = True
    logger.warning(
        f"File locks aren't supported for recipe card pages ({errno.errorcode.get(error.errno or 0, error.errno)}): "
        "turning a page is kept to one at a time within each worker process only"
    )


def _flock(fd: int, deadline: float) -> None:
    """An exclusive `flock` on `fd` by `deadline`, else `TimeoutError`; nothing where the filesystem has no locks"""
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Another turn of this page is still in progress") from None
            time.sleep(_TURN_LOCK_POLL)
        except OSError as e:
            if e.errno not in _UNSUPPORTED_LOCK_ERRORS:
                raise
            _warn_turn_lock_unsupported(e)
            return


@contextmanager
def page_turn_lock(page_dir: Path, *, wait: float = TURN_LOCK_WAIT) -> Iterator[None]:
    """
    Holds a page's turn lock, so one turn of a page runs at a time across threads and worker processes. A turn holds it
    from settling what an earlier one left (`images.stage_rotation` does that first) through staging, storing the
    metadata and swapping the files in or discarding them; `settle_turns` holds it too, so it never discards the staged
    files of a turn whose metadata is still being stored.

    The lock is an exclusive `flock` on the page's `TURN_LOCK_FILE`, opened for writing (NFS emulates `flock` with
    write locks), behind the page's lock in this process, for platforms whose `flock` belongs to the process. Where the
    filesystem has no locks, the process's lock alone (one warning is logged).

    Waits up to `wait` seconds, then raises `TimeoutError`; `FileNotFoundError` when the page's directory is gone.
    Callers hold `storage.ingest_write()`: the lock file is made under `groups/`.
    """
    deadline = time.monotonic() + wait
    with _page_gate(os.path.abspath(page_dir)) as gate:
        if not gate.acquire(timeout=max(deadline - time.monotonic(), 0)):
            raise TimeoutError("Another turn of this page is still in progress")
        try:
            fd = os.open(page_dir / TURN_LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                _flock(fd, deadline)
                yield
            finally:
                os.close(fd)  # which releases the flock
        finally:
            gate.release()


def settle_turns(
    repos: IngestRepos, job: RecipeIngestionJob, indexes: Collection[int] | None = None
) -> tuple[RecipeIngestionJob, bool]:
    """
    Settles what a stop left between staging a page's turn and swapping it in (`images.recover_staged`), for the
    job's pages with staged files (only `indexes`, when given): the swap is finished when the stored metadata names
    the staged page, and the staged files are discarded when it doesn't. Each page is settled under its turn lock,
    against its metadata read again there, and not while a task runs: the runner owns a running task's pages (it
    settles them when the task starts, and may be turning one now).

    A merge a stop left half done (`settle_merges`) is settled first, so a page another card was holding is home.

    Returns the job as last read (`job` itself when nothing was staged) and whether none of the pages asked about is
    left staged. Raises `JobActionError` `not_found` when the job is gone, `FileNotFoundError` when a page's directory
    is, and `TimeoutError` when a turn holds a page past `TURN_LOCK_WAIT`. Callers hold `storage.ingest_write()`.
    """
    job = settle_merges(repos, job)
    staged = [
        page.index
        for page in parse_pages(job.pages)
        if (indexes is None or page.index in indexes)
        and images.has_staged(storage.page_dir(job.group_id, job.id, page.index))
    ]
    settled = True
    for index in staged:
        page_dir = storage.page_dir(job.group_id, job.id, index)
        with page_turn_lock(page_dir):
            current = repos.jobs.get(job.id)
            if current is None:
                raise not_found()
            job = current
            stored = next((page for page in parse_pages(job.pages) if page.index == index), None)
            if stored is None or job.task_state == IngestTaskState.running.value:
                settled = settled and not images.has_staged(page_dir)
                continue
            outcome = images.recover_staged(page_dir, stored)
            if outcome != "none":
                logger.info(f"Recipe card job {job.id}: the staged turn of page {index} a stop left was {outcome}")
    return job, settled


# ==========================================
# Merging cards: the household's lock, and settling a merge a stop left


_merge_gates: dict[UUID, threading.Lock] = {}
"""This process's merge lock per household, taken before the database's (`household_merge_lock`)"""
_merge_gates_guard = threading.Lock()

MERGE_MARKER_PREFIX = ".merge-"
"""
A merge's note in the target's folder, `.merge-<source id>.json`, written before the source's pages move there and
removed once the merge is over: what a stop in between leaves for `settle_merges`
"""


def _merge_gate(household_id: UUID) -> threading.Lock:
    with _merge_gates_guard:
        return _merge_gates.setdefault(household_id, threading.Lock())


@contextmanager
def household_merge_lock(session: Session, household_id: UUID) -> Iterator[None]:
    """
    Holds the household's lock for merging cards (and discarding one, or settling a merge a stop left): this process's
    lock, then the household's intake lock (`intake.lock_household_intake`) in a fresh transaction of `session`, so
    every row read inside is as the last merge left it. The transaction is the lock's: whatever the body hasn't
    committed is committed at the end, or rolled back when it raises. Nothing may be pending in `session` before.
    """
    with _merge_gate(household_id):
        if session.in_transaction():
            session.commit()
        try:
            lock_household_intake(session, household_id)
            yield
        except BaseException:
            session.rollback()
            raise
        session.commit()


def _merge_marker(group_id: UUID, target_id: UUID, source_id: UUID) -> Path:
    return storage.job_dir(group_id, target_id) / f"{MERGE_MARKER_PREFIX}{source_id}.json"


@dataclass(frozen=True)
class _MergeMarker:
    path: Path
    source_id: UUID
    target_id: UUID
    moves: list[tuple[int, int]]
    """Each moved page's index on the source, then on the target"""

    @classmethod
    def write(cls, group_id: UUID, source_id: UUID, target_id: UUID, moves: list[tuple[int, int]]) -> _MergeMarker:
        path = _merge_marker(group_id, target_id, source_id)
        body = {"source": str(source_id), "target": str(target_id), "moves": [list(move) for move in moves]}
        storage.atomic_write_bytes(path, json.dumps(body).encode())
        return cls(path, source_id, target_id, moves)

    @classmethod
    def read(cls, path: Path) -> _MergeMarker | None:
        try:
            body = json.loads(path.read_bytes())
            moves = [(int(origin), int(dest)) for origin, dest in body["moves"]]
            return cls(path, UUID(body["source"]), UUID(body["target"]), moves)
        except FileNotFoundError:
            return None  # settled meanwhile
        except OSError, ValueError, KeyError, TypeError:
            logger.warning(f"Removed a recipe card merge note that can't be read ({path.name})")
            path.unlink(missing_ok=True)
            return None


def _merge_markers(job: RecipeIngestionJob) -> list[Path]:
    """
    The merge notes `job` is in: as the target, the notes in its folder; as the source, while one of its pages is
    missing (a card that can be merged: ready or failed, or being committed since), a note naming it in another card's
    folder
    """
    paths = sorted(storage.job_dir(job.group_id, job.id).glob(f"{MERGE_MARKER_PREFIX}*.json"))
    statuses = (IngestStatus.ready.value, IngestStatus.failed.value, IngestStatus.committing.value)
    if job.status in statuses and any(
        not storage.page_dir(job.group_id, job.id, page.index).is_dir() for page in parse_pages(job.pages)
    ):
        paths += sorted(storage.ingest_root(job.group_id).glob(f"*/{MERGE_MARKER_PREFIX}{job.id}.json"))
    return paths


def _remove_merged_source(group_id: UUID, source_id: UUID) -> None:
    """What is left of a merged card's folder once its pages moved: only empty folders go, never files"""
    folder = storage.job_dir(group_id, source_id)
    for path in (folder / "pages", folder):
        try:
            path.rmdir()
        except OSError:
            pass  # gone already, or not empty: the orphan purge removes a folder without a row later


def _settle_marker(repos: IngestRepos, marker: _MergeMarker) -> None:
    """
    Settles one merge a stop left, by what the database says, holding the household's merge lock (so no merge is
    under way): the merge deletes the source and gives the target its pages in one transaction, after moving them.
    - The source is still there: the merge never happened, so its pages go back.
    - The source is gone and the target lists the pages: the merge happened; the pages are where they belong.
    - The source is gone and the target doesn't list them (it was discarded or purged after a merge that never
      happened): the pages are nobody's and go.
    """
    group_id = repos.group_id
    source, target = repos.jobs.get(marker.source_id), repos.jobs.get(marker.target_id)
    listed = {page.index for page in parse_pages(target.pages)} if target is not None else set()
    for origin_index, dest_index in marker.moves:
        origin = storage.page_dir(group_id, marker.source_id, origin_index)
        dest = storage.page_dir(group_id, marker.target_id, dest_index)
        if dest_index in listed or not dest.is_dir():
            continue  # the target's page now, or never moved
        if source is None:
            shutil.rmtree(dest, ignore_errors=True)
        elif origin.exists() or not origin.parent.is_dir():
            logger.error(f"Recipe card job {marker.source_id}: couldn't put back page {origin_index} of a merge")
        else:
            os.rename(dest, origin)
    marker.path.unlink(missing_ok=True)
    if source is None:
        _remove_merged_source(group_id, marker.source_id)
    outcome = "undone" if source is not None else "finished"
    logger.info(f"Recipe card job {marker.target_id}: a merge a stop left half done was {outcome}")


def settle_merges(repos: IngestRepos, job: RecipeIngestionJob) -> RecipeIngestionJob:
    """
    Settles what a stop left of a merge `job` was in, as the target or the source (`_merge_markers`, `_settle_marker`),
    under the household's merge lock; returns the job read again then, or `job` itself when there was nothing to
    settle. Raises `JobActionError` `not_found` when the job is gone (a source whose merge happened). Ends the session's
    transaction, which must have nothing pending. Callers hold `storage.ingest_write()`.
    """
    if not _merge_markers(job):
        return job
    with household_merge_lock(repos.session, repos.household_id or job.household_id):
        for path in _merge_markers(job):
            if (marker := _MergeMarker.read(path)) is not None:
                _settle_marker(repos, marker)
        current = repos.jobs.get(job.id)
    if current is None:
        raise not_found()
    return current


def settle_merges_into(session: Session, group_id: UUID, job_id: UUID, *, locked: UUID | None = None) -> None:
    """
    Settles what a stop left of merges into the card `job_id` before the card or its folder is deleted (a discard, the
    purge): each note in its folder (`_settle_marker`), so a source card that is still there gets its pages back rather
    than losing them with the folder. The card's row may be gone already (a folder without one).

    `locked` is the household whose merge lock (`household_merge_lock`) the caller holds, the card's: a merge's two
    cards are of one household. Without it, each note is settled under the merge lock of its source's household, taken
    here; a note whose source is gone has nothing to give back (its pages go with the folder). Callers hold
    `storage.ingest_write()`, with nothing pending in `session`.
    """
    for path in sorted(storage.job_dir(group_id, job_id).glob(f"{MERGE_MARKER_PREFIX}*.json")):
        if (marker := _MergeMarker.read(path)) is None:
            continue
        if locked is not None:
            _settle_marker(IngestRepos(session, group_id, locked), marker)
            continue
        source = sa.select(Job.household_id).where(Job.id == marker.source_id, Job.group_id == group_id)
        household_id = session.execute(source).scalar_one_or_none()
        session.commit()
        if household_id is not None:
            with household_merge_lock(session, household_id):
                _settle_marker(IngestRepos(session, group_id, household_id), marker)


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
    settled: bool = True
    """
    False when a turn of the page may still be swapping in (a running task is turning it, or a backup restore pauses
    writes): the file is served as it is, and never cached
    """


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
            "draft_version": job.draft_version,
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
            # when the retention purge removes a failed card (§16), by the purge's own rule
            "expires_at": retention.failed_card_expires_at(job),
            "household_recipes_public": self._household_recipes_public,
        }

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
        committed_since: datetime | None = None,
        order: JobOrder = "created",
        page: int = 1,
        per_page: int = 50,
    ) -> RecipeIngestionJobPagination:
        """
        A page of the household's jobs, newest first, or by commit time (`order="committed"`: the latest added first,
        then the cards not added newest first); `committed_since` keeps the cards added since then (a naive time is
        UTC); `per_page=-1` gives them all
        """
        page = max(page, 1)
        jobs, total = self.repos.jobs.page(
            statuses=statuses or None,
            batch_id=batch_id,
            committed_since=committed_since,
            order=order,
            page=page,
            per_page=per_page,
        )
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

    def possible_duplicates(self, job: RecipeIngestionJob, draft: CardDraft | None) -> PossibleDuplicates:
        """
        A card being reviewed (§6.4): the group recipe whose slug its name would get, and then the name commit would
        give the recipe, as upstream's create picks it (`duplicate_name`); else the household's recipe whose name is
        most like it (`NEAR_NAME_RATIO`); and the household's oldest other card waiting or being read with the same
        name (`IngestJobsRepo.same_title`). Nothing for a card in any other state, or a blank name.
        """
        if draft is None or job.status != IngestStatus.ready.value:
            return PossibleDuplicates()
        return self._duplicates(job.id, draft, recipe_id=job.recipe_id, locale=job.locale)

    def _duplicates(
        self, job_id: UUID, draft: CardDraft, *, recipe_id: UUID | None, locale: str | None
    ) -> PossibleDuplicates:
        """`possible_duplicates` of a card being reviewed, by its id, its recipe (if any) and its language"""
        if not draft.name.strip():
            return PossibleDuplicates()
        name = _recipe_name(draft.name, locale)
        recipe, suffixed = self._same_name(name, recipe_id)
        return PossibleDuplicates(
            recipe=recipe or self._near_name(name, recipe_id),
            job=self.repos.jobs.same_title(draft.name, exclude_id=job_id),
            name=suffixed,
        )

    def _same_name(self, name: str, own_recipe: UUID | None) -> tuple[RecipeIngestionRecipeRef | None, str | None]:
        """
        The group recipe holding the slug `name` gets (slugs are unique in a group), and the first free "Name (n)",
        which commit names the recipe (`suffixed_name`); (None, None) when the slug is free or held by the card's own
        recipe
        """
        slug = _slug(name)
        if slug is None:
            return None, None
        stmt = sa.select(RecipeModel.id, RecipeModel.slug, RecipeModel.name).where(
            RecipeModel.group_id == self.group_id, RecipeModel.slug == slug
        )
        row = self.session.execute(stmt.limit(1)).one_or_none()
        if row is None or row.id == own_recipe:
            return None, None
        ref = RecipeIngestionRecipeRef(id=row.id, slug=row.slug, name=row.name)
        return ref, suffixed_name(self.session, self.group_id, name)

    def _near_name(self, name: str, own_recipe: UUID | None) -> RecipeIngestionRecipeRef | None:
        """The household's recipe whose name is most like `name`, at least `NEAR_NAME_RATIO`; the oldest of a tie"""
        key = title_key(name)
        if not key:
            return None
        shortest, longest = _near_name_lengths(key)
        stmt = (
            sa.select(RecipeModel.id, RecipeModel.slug, RecipeModel.name)
            .join(User, User.id == RecipeModel.user_id)
            .where(
                RecipeModel.group_id == self.group_id,
                User.household_id == self.household_id,
                sa.func.length(RecipeModel.name).between(shortest, longest),
            )
            .order_by(RecipeModel.created_at, RecipeModel.id)
        )
        best: tuple[float, Any] | None = None
        for row in self.session.execute(stmt):
            if row.id == own_recipe or not row.name:
                continue
            score = fuzz.ratio(key, title_key(row.name), score_cutoff=NEAR_NAME_RATIO)
            if score and (best is None or score > best[0]):
                best = (score, row)
        if best is None:
            return None
        row = best[1]
        return RecipeIngestionRecipeRef(id=row.id, slug=row.slug, name=row.name)

    @cached_property
    def _household(self) -> HouseholdInDB | None:
        repos = get_repositories(self.session, group_id=self.group_id, household_id=self.household_id)
        return repos.households.get_one(self.household_id)

    @property
    def _household_recipes_public(self) -> bool:
        return recipes_public(self._household)

    def get_job(self, job_id: UUID) -> RecipeIngestionJobOut:
        """The whole job for the review page, with its permissions and the possible duplicate"""
        job = self.job(job_id)
        draft = _parse_draft(job.draft)
        extraction = _parse_extraction(job.extraction)
        recipes = self._recipe_refs([job.recipe_id] if job.recipe_id else [])
        duplicates = self.possible_duplicates(job, draft)
        out = RecipeIngestionJobOut(
            **self._summary_fields(job, recipes),
            pages=[] if is_slimmed(job) else [PageOut.from_meta(job.id, page) for page in parse_pages(job.pages)],
            transcription=job.transcription,
            read=extraction.read_info() if extraction else None,
            draft=draft,
            flags=parse_flags(job.flags),
            proposals=_parse_proposals(job.proposals),
            permissions=self._permissions(job),
            duplicate_of=duplicates.recipe,
            duplicate_job=duplicates.job,
            duplicate_name=duplicates.name,
            card_photo_default=not recipes_created_public(self._household),
            card_cover_default=not recipes_created_public(self._household),
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

        The answer carries the lines this save parsed (`_parse_text_lines`), as stored, so the page shows their amount,
        unit and food at once, and the possible duplicates for the saved name (`possible_duplicates`), so the banner
        follows a rename.
        """
        draft, parsed = self._parse_text_lines(job_id, _with_unique_ids(update.draft), update)
        resolved_proposals = {str(proposal_id) for proposal_id in update.resolved_proposal_ids}
        units = self._unit_names() if resolved_proposals else []
        linked = self._linked_names(draft)

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
            flags = resolve_flags(
                draft, extraction, resolutions, transcription=transcription, previous=previous, linked=linked
            )
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

        before = written.before
        duplicates = self._duplicates(job_id, draft, recipe_id=before["recipe_id"], locale=before["locale"])
        return CardDraftSaved(
            draft_version=written.values["draft_version"],
            flags=written.values["flags"],
            error_count=written.values["error_count"],
            warning_count=written.values["warning_count"],
            ingredients=parsed or None,
            duplicate_of=duplicates.recipe,
            duplicate_job=duplicates.job,
            duplicate_name=duplicates.name,
        )

    def _unit_names(self) -> list[str]:
        """The group's unit names for `unit_unclear` (`IngestMatcher.unit_names`), read before a draft's write"""
        repos = get_repositories(self.session, group_id=self.group_id, household_id=self.household_id)
        units = IngestMatcher(repos).unit_names()
        if self.session.in_transaction():
            self.session.commit()  # the write reads the row again: no snapshot stays open meanwhile
        return units

    def _linked_names(self, draft: CardDraft) -> dict[UUID, list[str]]:
        """
        Every name of the group's foods and units the draft links, for `linked_fuzzy` (`IngestMatcher.linked_names`,
        only the linked rows), read before a draft's write
        """
        repos = get_repositories(self.session, group_id=self.group_id, household_id=self.household_id)
        linked = IngestMatcher(repos).linked_names(draft.ingredients)
        if self.session.in_transaction():
            self.session.commit()  # the write reads the row again: no snapshot stays open meanwhile
        return linked

    def _parse_text_lines(
        self, job_id: UUID, draft: CardDraft, update: CardDraftUpdate
    ) -> tuple[CardDraft, list[CardDraftIngredient]]:
        """
        `draft` with each line the reviewer wrote as text (`_text_to_parse`) parsed and linked as extraction does (§5),
        and each text-only line whose every marker this save keeps as written (`kept_line`, with the save's
        resolutions) parsed around its markers (`parse_written_lines`); and the lines parsed, as the draft now holds
        them. A line the parser can't split stays as sent, and nothing is parsed on a card that isn't in English, or
        for a save that will be refused. A kept line is parsed when it's kept, written again or added, not on every
        save; commit parses one that never was (`parse_kept_lines`).

        A page that didn't take the answer's lines sends the line as it typed it with its next saves: it differs from
        the stored (parsed) line's note, so it's parsed again into the same line, and the draft doesn't change.
        Blocking: the parse runs here, before the draft's write.
        """
        job = self.repos.jobs.get(job_id)
        if self.session.in_transaction():
            self.session.commit()  # the write reads the row again: no snapshot stays open meanwhile
        if job is None or job.status != IngestStatus.ready.value or job.draft_version != update.draft_version:
            return draft, []
        extraction = _parse_extraction(job.extraction)
        language = extraction.language if extraction else None
        if not card_flags.is_english(language):
            return draft, []

        stored_draft = _parse_draft(job.draft)
        stored = {line.reference_id: line for line in stored_draft.ingredients} if stored_draft else {}
        stored_flags = parse_flags(job.flags)
        kept_before = kept_flag_ids(stored_flags)
        kept_now = kept_flag_ids(stored_flags, update.flag_resolutions)
        chosen: dict[UUID, str | KeptLine] = {}
        for ingredient in draft.ingredients:
            if (text := _text_to_parse(ingredient, stored)) is not None:
                chosen[ingredient.reference_id] = text
            elif (line := kept_line(ingredient, kept_now)) is not None:
                before = stored.get(ingredient.reference_id)
                if (
                    before is None
                    or before.note.strip() != ingredient.note.strip()
                    or not kept_line(ingredient, kept_before)
                ):
                    chosen[ingredient.reference_id] = line
        if not chosen:
            return draft, []

        repos = get_repositories(self.session, group_id=self.group_id, household_id=self.household_id)
        return parse_written_lines(draft, chosen, repos=repos, locale=job.locale, language=language, job_id=job_id)

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

    def rebuild(self, job_id: UUID, transcription: str) -> RecipeIngestionJobState:
        """
        Builds the recipe again from the card's text as the reviewer corrected it (no photo is read): like a
        re-extract, it replaces a draft nobody edited, else becomes a whole-card proposal marked as a rebuild (§3.1).
        A card being reviewed with no task; it runs under the card's policy, as every task does.
        """
        self._check_idle(self.job(job_id))
        return self._enqueue(job_id, tasks.rebuild_payload(transcription), limits.PRIORITY_EXTRACT)

    def parse_lines(self, job_id: UUID, refs: Sequence[UUID]) -> RecipeIngestionJobState:
        """
        Parses the draft's ingredient lines `refs` with the AI ingredient parser, in any language ("Parse with AI"),
        each with its text as it reads now; the result is written into the lines still as they were (§5). A line kept
        as written with a marker is parsed around its markers (`KeptLine`). A card being reviewed with no task, in the
        re-read slot (a reviewer waits for it); 422 `unknown_target` for a line the draft hasn't.
        """
        job = self.job(job_id)
        self._check_idle(job)
        draft = _parse_draft(job.draft)
        if draft is None:
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, UNKNOWN_TARGET)
        # a line kept as written with a marker is parsed around its markers, as a save parses it (`KeptLine`)
        kept = kept_flag_ids(parse_flags(job.flags))
        marked = {line.reference_id: m for line in draft.ingredients if (m := kept_line(line, kept)) is not None}
        try:
            payload = tasks.parse_lines_payload(draft, refs, marked)
        except KeyError:
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, UNKNOWN_TARGET) from None
        return self._enqueue(job_id, payload, limits.PRIORITY_REREAD)

    @staticmethod
    def _check_idle(job: RecipeIngestionJob) -> None:
        """A task for a card being reviewed: 409 `invalid_status` in any other state, 409 `busy` while it has one"""
        if job.status != IngestStatus.ready.value:
            raise invalid_status(job.status)
        if job.task_state is not None:
            raise busy()

    def _enqueue(self, job_id: UUID, payload: dict[str, Any], priority: int) -> RecipeIngestionJobState:
        """An extract task in the mode `payload` names, for a card still being reviewed with no task"""
        where = [Job.status == IngestStatus.ready.value]
        if not self.repos.jobs.enqueue_task(job_id, IngestTaskKind.extract, payload, priority, where=where):
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
    # Where on the card a field's text is

    def region_hint(self, job_id: UUID, target: ProposalTarget) -> RegionHintOut:
        """
        Where on an upright page the target field's text probably is, for the re-read selection to start there
        (§6.5, `pipeline.region_hint`): by the lines Tesseract found when it oriented the page, else by the text's line
        in the transcription. The text is what the card says for the field: an ingredient's line as read
        (`original_text`; a line added on the page, as it reads now), a step's or note's text, or a single field's.
        404 `not_found` when the draft has no such text or neither finds it (the reviewer typed it, or there's no
        reading to go by), as for a card that isn't there: the page starts the selection as it would without a hint.
        """
        job = self.job(job_id)
        draft = _parse_draft(job.draft)
        pages = [] if is_slimmed(job) else parse_pages(job.pages)
        text = _target_text(draft, target) if draft is not None and pages else None
        hint = region_hint(pages, job.transcription, text) if text else None
        if hint is None:
            raise not_found()
        return RegionHintOut(
            page=hint.page, x=hint.x, y=hint.y, width=hint.width, height=hint.height, source=hint.source
        )

    # ==========================================
    # Files (the caller holds the ingest write lock)

    def rotate(self, job_id: UUID, index: int, degrees: int) -> PageOut:
        """
        Turns one page clockwise (§4.4), crash-safe: the turned files are staged beside the page's
        (`images.stage_rotation`, which first settles a turn a stop left), the new metadata is stored conditional on
        the job still having no task and the page still being the one turned, then the staged files are swapped in
        (`images.apply_staged`). A refused write discards them, leaving the page byte for byte as stored. An error
        while the metadata is written leaves them, as whether it was stored isn't known: the next look at the page
        settles them against what was (`settle_turns`). It all holds the page's turn lock, and reads the job again
        once it has it.

        409 `busy` while a task is active, the page changed meanwhile, or another turn of it takes too long. The
        caller holds the ingest write lock.
        """
        self._check_rotate(self.job(job_id), index)
        page_dir = storage.page_dir(self.group_id, job_id, index)
        try:
            with page_turn_lock(page_dir):
                return self._turn_page(job_id, index, degrees, page_dir)
        except FileNotFoundError as e:
            raise not_found() from e
        except TimeoutError as e:
            raise busy() from e

    @staticmethod
    def _check_rotate(job: RecipeIngestionJob, index: int) -> PageMeta:
        """The page to turn, or why it can't be"""
        pages = {page.index: page for page in parse_pages(job.pages)}
        if index not in pages:
            raise not_found()
        if job.task_state is not None:
            raise busy()
        if job.status not in (IngestStatus.ready.value, IngestStatus.failed.value):
            raise invalid_status(job.status)
        return pages[index]

    def _turn_page(self, job_id: UUID, index: int, degrees: int, page_dir: Path) -> PageOut:
        """`rotate`, holding the page's turn lock"""
        job = self.job(job_id)  # again: another turn of the page may have landed while this one waited
        before = self._check_rotate(job, index)
        turned = images.stage_rotation(page_dir, before, degrees, PageRotationSource.user)

        def mutate(row: RowMapping) -> dict[str, Any] | None:
            stored = [dict(page) for page in row["pages"] or []]
            for position, page in enumerate(stored):
                if page.get("index") == index:
                    if page.get("page_sha256") != before.page_sha256:
                        return None  # the page changed since it was read
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
            # a task started, the page changed or the job went while the turn was staged: the page stays as stored
            images.discard_staged(page_dir)
            raise self._refuse_enqueue(job_id, IngestStatus(job.status))

        images.apply_staged(page_dir)
        return PageOut.from_meta(job_id, turned)

    def discard(self, job_id: UUID) -> None:
        """
        Deletes the job's row and its files (§3.1, §9): the uploader, anyone for an inbox card, otherwise the
        household's managers. Deleting the row clears any task with it, so a running one stops within a heartbeat.
        Holds the household's merge lock, so a merge never moves pages into a folder being deleted, and a merge into
        the card that a stop left half done gives the other card its pages back first (`settle_merges_into`). The
        caller holds the ingest write lock.
        """
        with household_merge_lock(self.session, self.household_id):
            job = self.job(job_id)
            if not self.can_discard(job):
                raise JobActionError(status.HTTP_403_FORBIDDEN, FORBIDDEN)
            discardable = [IngestStatus.processing.value, IngestStatus.ready.value, IngestStatus.failed.value]
            if job.status not in discardable:
                raise invalid_status(job.status)

            settle_merges_into(self.session, self.group_id, job_id, locked=self.household_id)
            if not self.repos.jobs.delete(job_id, where=[Job.status.in_(discardable)]):
                current = self.job(job_id)
                raise invalid_status(current.status)
            storage.remove_job_dir(self.group_id, job_id)

    def merge(self, job_id: UUID, into_job_id: UUID) -> RecipeIngestionJobState:
        """
        Adds a card's photos to another card of the household as its next pages (a back sent as a card of its own),
        deletes the card, and reads the other one again: an unedited draft is replaced, an edited one gets a proposal.
        Both must be ready or failed with no task, the user must have uploaded both or manage the household, and the
        pages must fit in one card. The other card keeps to this server if either was sent so (`local_only`, §10).

        Merges of the household run one at a time (`household_merge_lock`), each reading both cards under the lock, so
        one never moves pages into a card another is deleting. The files move first, under a note in the target's
        folder (`_MergeMarker`), then one transaction writes the target and deletes the source, fenced on both rows'
        `row_version`; when that matches nothing the files move back. Only the moved pages leave the source's folder,
        whose empty remains are removed after. A stop in between is settled from the note (`settle_merges`). The
        caller holds the ingest write lock.
        """
        if job_id == into_job_id:
            raise JobActionError(status.HTTP_422_UNPROCESSABLE_CONTENT, SAME_CARD)
        with household_merge_lock(self.session, self.household_id):
            source, target = self.job(job_id), self.job(into_job_id)
            for job in (source, target):
                for path in _merge_markers(job):  # a merge a stop left, settled before this one counts the pages
                    if (marker := _MergeMarker.read(path)) is not None:
                        _settle_marker(self.repos, marker)
            source, target = self.job(job_id), self.job(into_job_id)
            moves = self._check_merge(source, target)

            marker = _MergeMarker.write(
                self.group_id, job_id, into_job_id, [(page.index, moved.index) for page, _, _, moved in moves]
            )
            done: list[tuple[Path, Path]] = []
            try:
                for _, origin, dest, _ in moves:
                    os.rename(origin, dest)
                    done.append((origin, dest))
                merged = [*parse_pages(target.pages), *(moved for _, _, _, moved in moves)]
                written = self._write_merge(source, target, merged)
            except BaseException:
                self._move_back(done)
                marker.path.unlink(missing_ok=True)
                raise
            if not written:
                self._move_back(done)
                marker.path.unlink(missing_ok=True)
                self.session.rollback()
                current = self.repos.jobs.get(into_job_id), self.repos.jobs.get(job_id)
                if any(job is not None and job.task_state is not None for job in current):
                    raise busy()
                state = next((job.status for job in current if job is not None), IngestStatus.ready.value)
                raise invalid_status(state)

            # a commit that fails leaves the note: the next look settles the pages by what the database holds
            self.session.commit()
            marker.path.unlink(missing_ok=True)
            _remove_merged_source(self.group_id, job_id)
        return self._queued(into_job_id)

    def _check_merge(
        self, source: RecipeIngestionJob, target: RecipeIngestionJob
    ) -> list[tuple[PageMeta, Path, Path, PageMeta]]:
        """
        Why the source can't be added to the target, or its pages' moves: each page, its folder, the folder it moves
        to, and the page as the target's
        """
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
                page,
                storage.page_dir(self.group_id, source.id, page.index),
                storage.page_dir(self.group_id, target.id, first + offset),
                page.model_copy(update={"index": first + offset}),
            )
            for offset, page in enumerate(source_pages)
        ]
        if (
            not moves
            or not storage.page_dir(self.group_id, target.id, 0).parent.is_dir()
            or not all(origin.is_dir() for _, origin, _, _ in moves)
            or any(dest.exists() for _, _, dest, _ in moves)
        ):
            raise JobActionError(status.HTTP_409_CONFLICT, FILES_MISSING)
        return moves

    def _write_merge(self, source: RecipeIngestionJob, target: RecipeIngestionJob, pages: list[PageMeta]) -> bool:
        """
        In the merge's transaction, left to the caller to end: the target gets the pages and an extract task (a failed
        one goes back to `processing`), and the source is deleted, each only if its `row_version`, status and idle
        task are as read. Whether both happened.

        The target keeps to this server when either card was sent so: photos uploaded to stay here never reach a cloud
        provider through the card they join (§10); only "Read with cloud" lifts that, with the user's consent.
        """
        movable = [IngestStatus.ready.value, IngestStatus.failed.value]
        failed = target.status == IngestStatus.failed.value
        values: dict[str, Any] = {
            "pages": [page.model_dump(mode="json") for page in pages],
            "source_sha256": source_sha256(pages),
        }
        if source.local_only and not target.local_only:
            values["local_only"] = True
        if failed:
            values |= {"status": IngestStatus.processing.value, "error_code": None, "error_params": None}
        where = [Job.row_version == target.row_version, Job.status == target.status]
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
        if not queued:
            return False
        deleted = self.session.execute(
            sa.delete(Job).where(
                Job.id == source.id,
                *self.repos.jobs.scope,
                Job.row_version == source.row_version,
                Job.status.in_(movable),
                Job.task_state.is_(None),
            ),
            execution_options={"synchronize_session": False},
        )
        return getattr(deleted, "rowcount", 0) == 1

    @staticmethod
    def _move_back(done: Sequence[tuple[Path, Path]]) -> None:
        for origin, dest in reversed(done):
            try:
                os.rename(dest, origin)
            except OSError:
                logger.error("Couldn't move a merged card's page back; the card may be missing a page")

    def page_image(self, job_id: UUID, index: int, kind: str) -> PageImage:
        """
        One of a page's images, after the household check (§9). A turn a stop left staged is settled first
        (`settle_turns`), so the file served is the one the page's metadata, and so its ETag, describes; while that
        can't be done (a running task is turning the page, or a backup restore pauses writes) the file is served as
        it is, with `settled` false. A page that isn't in its folder may be in another card's, where a merge a stop
        left half done put it: that is settled first too (`settle_merges`).
        """
        job = self.job(job_id)
        pages = {page.index: page for page in parse_pages(job.pages)}
        if index not in pages or kind not in PAGE_MEDIA_TYPES or is_slimmed(job):
            raise not_found()

        page_dir = storage.page_dir(self.group_id, job_id, index)
        settled = True
        if images.has_staged(page_dir) or not page_dir.is_dir():
            job, settled = self._settle_to_read(job, index)
            pages = {page.index: page for page in parse_pages(job.pages)}
            if index not in pages:
                raise not_found()

        page = pages[index]
        path = images.page_file(page_dir, kind)
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
            settled=settled,
        )

    def _settle_to_read(self, job: RecipeIngestionJob, index: int) -> tuple[RecipeIngestionJob, bool]:
        """`settle_turns` for one page about to be read, in a write section of its own; unsettled while paused"""
        try:
            with storage.ingest_write():
                return settle_turns(self.repos, job, [index])
        except IngestPaused:
            return job, False  # a restore is replacing the files anyway
        except TimeoutError:
            return job, False
        except FileNotFoundError as e:
            raise not_found() from e
