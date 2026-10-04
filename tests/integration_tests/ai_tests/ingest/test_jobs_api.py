"""
The recipe card job routes (docs/ai/PHASE2.md §14): lists, counts, the job, its state, re-read, re-extract, retry,
cancel, rotate and discard, and their error bodies. Runs on SQLite and PostgreSQL.

The helpers at the top (seeding jobs with real page files, a stand-in for the flag rules, a second household member)
are shared with the other job, review and commit tests in this folder.
"""

import io
import re
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from PIL import ExifTags, Image, ImageDraw

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftRef,
    CardDraftStep,
    CardFlag,
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    CardProposal,
    CardProposalKind,
    ExtractionMeta,
    FlagResolution,
    IngestErrorCode,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    ProposalTarget,
    RecipeIngestionSettingsUpdate,
)
from mealie.services.ai.ingest import flag_rules, images, limits, storage
from mealie.services.ai.ingest.pipeline import flags as card_flags
from mealie.services.ai.ingest.settings import get_ingest_settings
from tests import utils
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser

JOBS = "/api/ai/ingest/jobs"

# ==================================================================================================================
# Shared helpers


def job_url(job_id: UUID | str, *parts: str | int) -> str:
    return "/".join([JOBS, str(job_id), *(str(part) for part in parts)])


def card_photo(width: int = 480, height: int = 640, *, color: tuple[int, int, int] = (250, 245, 230)) -> bytes:
    """A phone-like JPEG with EXIF and GPS, and a dark block in its top-left corner to tell turns apart"""
    image = Image.new("RGB", (width, height), color)
    ImageDraw.Draw(image).rectangle((0, 0, width // 4, height // 6), fill=(20, 20, 20))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[ExifTags.IFD.GPSInfo] = {1: "N", 2: (51.0, 30.0, 0.0)}
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90, exif=exif)
    return buffer.getvalue()


def banana_draft(**changes: Any) -> CardDraft:
    """The banana card as a draft: one blank the card leaves on purpose, and an attribution"""
    draft = CardDraft(
        name="Banana Mug Cake",
        description="A quick cake",
        recipe_yield="1 mug",
        prep_time="5 minutes",
        attribution="From Grandma Jo",
        ingredients=[
            CardDraftIngredient(
                original_text="1 T. coconut oil (melted)",
                quantity=1,
                unit=CardDraftRef(name="tablespoon"),
                food=CardDraftRef(name="coconut oil"),
                note="melted",
                display="1 tablespoon coconut oil melted",
                parse_confidence=0.97,
            ),
            CardDraftIngredient(
                original_text="1/4 t. salt",
                quantity=0.25,
                unit=CardDraftRef(name="teaspoon"),
                food=CardDraftRef(name="salt"),
                display="1/4 teaspoon salt",
                parse_confidence=0.95,
            ),
        ],
        steps=[
            CardDraftStep(text="Mash the banana in a mug and stir in everything else."),
            CardDraftStep(text="Microwave for [blank] minutes."),
        ],
    )
    return draft.model_copy(update=changes)


_MARKER = re.compile(r"\[(illegible|blank)\]")


def _flag(
    kind: CardFlagKind, severity: CardFlagSeverity, source: CardFlagSource, field: str, ref: str | None
) -> CardFlag:
    return CardFlag(
        id=f"{kind.value}:{field}:{ref or ''}", kind=kind, severity=severity, source=source, field=field, ref=ref
    )


def fake_compute_flags(
    draft: CardDraft,
    extraction: ExtractionMeta | None,
    resolutions: Mapping[str, FlagResolution],
    *,
    transcription: str | None = None,
    previous: Sequence[CardFlag] | None = None,
    units: Iterable[str] = (),
    ocr_lines: Sequence[str] | None = None,
    linked: Mapping[UUID, Collection[str]] | None = None,
) -> list[CardFlag]:
    """
    A small stand-in for B1's flag rules, so these tests don't depend on them: markers are errors, a missing name is
    an error, a low parse confidence is a warning and an unlinked food is an info. Resolutions are applied by id; the
    transcription, the previous flags, the group's units, Tesseract's lines and the linked names aren't used
    (`test_review_flags.py` runs the real rules).
    """
    flags: list[CardFlag] = []
    if not draft.name.strip():
        flags.append(_flag(CardFlagKind.missing_name, CardFlagSeverity.error, CardFlagSource.validator, "name", None))
    for ingredient in draft.ingredients:
        ref = str(ingredient.reference_id)
        for match in _MARKER.finditer(f"{ingredient.original_text} {ingredient.note}"):
            kind = CardFlagKind(match.group(1))
            flags.append(_flag(kind, CardFlagSeverity.error, CardFlagSource.marker, "ingredients", ref))
        if ingredient.parse_confidence is not None and ingredient.parse_confidence < flag_rules.REVIEW_CONFIDENCE:
            flags.append(
                _flag(CardFlagKind.check_parse, CardFlagSeverity.warning, CardFlagSource.parser, "ingredients", ref)
            )
        if ingredient.food and ingredient.food.id is None and ingredient.food.name:
            flags.append(_flag(CardFlagKind.new_food, CardFlagSeverity.info, CardFlagSource.parser, "ingredients", ref))
    for step in draft.steps:
        for match in _MARKER.finditer(step.text):
            kind = CardFlagKind(match.group(1))
            flags.append(_flag(kind, CardFlagSeverity.error, CardFlagSource.marker, "steps", str(step.id)))

    unique = {flag.id: flag for flag in flags}
    return [flag.model_copy(update={"resolution": resolutions.get(flag.id)}) for flag in unique.values()]


def use_fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(card_flags, "compute_flags", fake_compute_flags)


def seed_job(
    user: TestUser,
    *,
    status: IngestStatus = IngestStatus.ready,
    draft: CardDraft | None = None,
    page_count: int = 1,
    batch_id: UUID | None = None,
    position: int = 0,
    source: IngestSource = IngestSource.app,
    flags: list[CardFlag] | None = None,
    **columns: Any,
) -> UUID:
    """
    A job of the user's household with real page files (normalized from phone-like photos, as intake makes them). A
    job with a draft gets the fake rules' flags unless `flags` says otherwise. `columns` override any column.
    """
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    job_id = uuid4()
    if draft is None and status != IngestStatus.processing and status != IngestStatus.failed:
        draft = banana_draft()
    if flags is None:
        flags = fake_compute_flags(draft, None, {}) if draft else []
    errors, warnings = flag_rules.count_unresolved(flags)

    with session_context() as session:
        repos = IngestRepos(session, group_id, household_id)
        if batch_id is None:
            batch_id = repos.batches.create(source=source, created_by=user.user_id, locale="en-US")

        with storage.ingest_write():
            storage.create_job_dir(group_id, job_id, page_count)
            pages = [
                images.normalize_page(
                    io.BytesIO(card_photo(480, 640, color=(250, 245 - 10 * index, 230))),
                    storage.page_dir(group_id, job_id, index),
                    index,
                    original_filename=f"IMG_{index}.jpg",
                )
                for index in range(page_count)
            ]

        values: dict[str, Any] = {
            "id": job_id,
            "batch_id": batch_id,
            "position": position,
            "created_by": user.user_id,
            "source": source.value,
            "source_name": "upload/IMG_0.jpg",
            "locale": "en-US",
            "status": status.value,
            "title": draft.name if draft else None,
            "draft_version": 1 if draft else 0,
            "extracted_version": 1 if draft else 0,
            "pages": pages,
            "source_sha256": uuid4().hex * 2,
            "draft": draft,
            "flags": flags,
            "proposals": [],
            "error_count": errors,
            "warning_count": warnings,
            "transcription": "Banana Mug Cake\n1 T. coconut oil (melted)" if draft else None,
            "extraction": ExtractionMeta(read_path="image", provider="Claude", model="claude-sonnet")
            if draft
            else None,
            **columns,
        }
        repos.jobs.create(values)
    return job_id


def job_row(job_id: UUID) -> dict[str, Any]:
    with session_context() as session:
        row = session.execute(
            sa.select(*RecipeIngestionJob.__table__.columns).where(RecipeIngestionJob.id == job_id)
        ).mappings()
        found = row.one_or_none()
        return dict(found) if found else {}


def set_columns(job_id: UUID, **values: Any) -> None:
    with session_context() as session:
        session.execute(sa.update(RecipeIngestionJob).where(RecipeIngestionJob.id == job_id).values(**values))
        session.commit()


def household_member(api_client: TestClient, admin_token: dict, user: TestUser, **permissions: bool) -> TestUser:
    """Another user in `user`'s household; permissions (`canOrganize=True`, ...) default to off"""
    group = api_client.get(api_routes.groups_self, headers=user.token).json()
    household = api_client.get(api_routes.households_self, headers=user.token).json()
    data = {
        "fullName": utils.random_string(),
        "username": utils.random_string(),
        "email": utils.random_email(),
        "password": "useruser",
        "group": group["name"],
        "household": household["name"],
        "admin": False,
        "tokens": [],
        **permissions,
    }
    response = api_client.post(api_routes.admin_users, json=data, headers=admin_token)
    assert response.status_code == 201, response.text
    token = utils.login({"username": data["email"], "password": "useruser"}, api_client)
    me = api_client.get(api_routes.users_self, headers=token).json()
    assert me["householdId"] == user.household_id
    return TestUser(
        email=data["email"],
        user_id=UUID(me["id"]),
        username=data["username"],
        full_name=data["fullName"],
        password="useruser",
        _group_id=UUID(me["groupId"]),
        _household_id=UUID(me["householdId"]),
        token=token,
        repos=user.repos,
    )


def assert_code(response: Any, status_code: int, code: str) -> dict[str, Any]:
    """The `{"detail": {"code": ...}}` body of a refusal the page handles itself: never a `message`"""
    assert response.status_code == status_code, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert "message" not in detail
    return detail


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


# ==================================================================================================================
# Lists and counts


def test_jobs_list_newest_first_with_filters_and_pagination(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    with session_context() as session:
        batch_id = IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).batches.create(
            source=IngestSource.app, created_by=user.user_id
        )
    first = seed_job(user, batch_id=batch_id, position=0)
    second = seed_job(user, status=IngestStatus.failed, batch_id=batch_id, position=1, error_code="no_recipe_found")
    other_batch = seed_job(user, status=IngestStatus.processing)

    response = api_client.get(JOBS, headers=user.token)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [item["id"] for item in body["items"]] == [str(other_batch), str(second), str(first)]
    assert "message" not in body["items"][0]

    ready = next(item for item in body["items"] if item["id"] == str(first))
    assert ready["status"] == "ready"
    assert ready["title"] == "Banana Mug Cake"
    assert ready["pageCount"] == 1
    assert ready["errorCount"] == 1  # the blank microwave time
    assert re.fullmatch(rf"/api/ai/ingest/jobs/{first}/pages/0/thumb\?v=[0-9a-f]{{12}}", ready["thumbUrl"])
    failed = next(item for item in body["items"] if item["id"] == str(second))
    assert failed["error"] == {"code": "no_recipe_found", "params": {}}

    by_batch = api_client.get(JOBS, params={"batchId": str(batch_id)}, headers=user.token).json()
    assert {item["id"] for item in by_batch["items"]} == {str(first), str(second)}

    by_status = api_client.get(JOBS, params=[("status", "ready"), ("status", "failed")], headers=user.token).json()
    assert {item["id"] for item in by_status["items"]} == {str(first), str(second)}

    paged = api_client.get(JOBS, params={"page": 2, "perPage": 2}, headers=user.token).json()
    assert paged["page"] == 2
    assert paged["total_pages"] == 2
    assert [item["id"] for item in paged["items"]] == [str(first)]
    assert paged["previous"]

    everything = api_client.get(JOBS, params={"perPage": -1}, headers=user.token).json()
    assert len(everything["items"]) == 3


def test_cards_added_since_a_time_newest_commit_first(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """
    The cards page's "added in the last 7 days" asks by commit time: a card uploaded 8 days ago and added today is in
    it, newest commit first, and one added 10 days ago isn't
    """
    user = unique_user_fn_scoped
    now = utcnow()
    committed = IngestStatus.committed
    old_upload = seed_job(
        user, status=committed, created_at=now - timedelta(days=8), committed_at=now - timedelta(hours=1)
    )
    new_upload = seed_job(
        user, status=committed, created_at=now - timedelta(hours=5), committed_at=now - timedelta(hours=2)
    )
    long_ago = seed_job(
        user, status=committed, created_at=now - timedelta(days=12), committed_at=now - timedelta(days=10)
    )
    ready = seed_job(user)

    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    query = {"status": "committed", "committedSince": since, "orderBy": "committedAt"}
    response = api_client.get(JOBS, params=query, headers=user.token)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(old_upload), str(new_upload)]
    assert items[0]["committedAt"].startswith((now - timedelta(hours=1)).isoformat()[:16])

    # a time without a zone is UTC, and the filter alone leaves out every card not added
    naive = (now - timedelta(days=7)).isoformat()
    alone = api_client.get(JOBS, params={"committedSince": naive}, headers=user.token).json()
    assert {item["id"] for item in alone["items"]} == {str(old_upload), str(new_upload)}

    # by commit time alone: the added cards, latest commit first, then the others newest first
    ordered = api_client.get(JOBS, params={"orderBy": "committedAt"}, headers=user.token).json()
    assert [item["id"] for item in ordered["items"]] == [str(old_upload), str(new_upload), str(long_ago), str(ready)]

    # pages keep the query
    paged = api_client.get(JOBS, params={**query, "perPage": 1}, headers=user.token).json()
    assert [item["id"] for item in paged["items"]] == [str(old_upload)]
    assert "orderBy=committedAt" in paged["next"] and "committedSince=" in paged["next"]

    assert api_client.get(JOBS, params={"orderBy": "title"}, headers=user.token).status_code == 422


def test_counts(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    seed_job(user)  # has an error: needs attention
    seed_job(user, draft=banana_draft(steps=[CardDraftStep(text="Microwave for 2 minutes.")]))
    seed_job(user, status=IngestStatus.processing)
    seed_job(user, status=IngestStatus.failed)

    response = api_client.get(f"{JOBS}/counts", headers=user.token)
    assert response.status_code == 200
    assert response.json() == {"processing": 1, "ready": 2, "needsAttention": 1, "failed": 1}


def test_routes_answer_503_when_ingest_is_disabled(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(get_ingest_settings(), "ENABLED", False)
    response = api_client.get(f"{JOBS}/counts", headers=unique_user.token)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ingest_disabled"
    assert response.json()["detail"]["message"]


# ==================================================================================================================
# One job


def test_get_job(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user, page_count=2)

    response = api_client.get(job_url(job_id), headers=user.token)
    assert response.status_code == 200
    job = response.json()
    assert job["draftVersion"] == 1
    assert job["draft"]["name"] == "Banana Mug Cake"
    assert [flag["kind"] for flag in job["flags"]] == ["new_food", "new_food", "blank"]
    assert job["read"]["readPath"] == "image"
    assert job["read"]["provider"] == "Claude"
    assert job["transcription"].startswith("Banana Mug Cake")
    assert len(job["pages"]) == 2
    for page in job["pages"]:
        version = re.escape(page["pageUrl"].rsplit("?v=", 1)[1])
        assert page["viewUrl"].endswith(f"/pages/{page['index']}/view?v={version}")
        assert page["thumbUrl"].endswith(f"/pages/{page['index']}/thumb?v={version}")
    # a registered user owns their group and household
    assert job["permissions"] == {
        "canCreateFoods": True,
        "canCreateOrganizers": True,
        "canDiscard": True,
        "canExportEval": True,
        "canReadWithCloud": False,
        "canUncommit": False,
        "canMerge": True,
    }
    assert job["duplicateOf"] is None
    preferences = api_client.get(api_routes.households_preferences, headers=user.token).json()
    assert job["householdRecipesPublic"] is preferences["recipePublic"]


def test_get_job_flags_a_possible_duplicate(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    response = api_client.post(api_routes.recipes, json={"name": "Banana Mug Cake"}, headers=user.token)
    assert response.status_code == 201
    job_id = seed_job(user)

    duplicate = api_client.get(job_url(job_id), headers=user.token).json()["duplicateOf"]
    assert duplicate["slug"] == "banana-mug-cake"
    assert duplicate["name"] == "Banana Mug Cake"


def test_get_job_permissions_of_a_plain_member(
    api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    member = household_member(api_client, admin_token, user)
    job_id = seed_job(user)
    inbox_job = seed_job(user, source=IngestSource.inbox, created_by=None)

    permissions = api_client.get(job_url(job_id), headers=member.token).json()["permissions"]
    assert permissions == {
        "canCreateFoods": False,
        "canCreateOrganizers": False,
        "canDiscard": False,
        "canExportEval": False,
        "canReadWithCloud": False,
        "canUncommit": False,
        "canMerge": False,
    }
    inbox = api_client.get(job_url(inbox_job), headers=member.token).json()["permissions"]
    assert inbox["canDiscard"] is True

    # the queue shows Discard only where it's allowed
    listed = api_client.get(JOBS, headers=member.token).json()["items"]
    assert {item["id"]: item["canDiscard"] for item in listed} == {str(job_id): False, str(inbox_job): True}
    listed = api_client.get(JOBS, headers=user.token).json()["items"]
    assert all(item["canDiscard"] for item in listed)


def test_only_a_card_with_a_draft_and_pages_can_be_exported(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    purged = seed_job(user, status=IngestStatus.committed)
    set_columns(purged, draft=None, flags=None, transcription=None)  # what the retention purge leaves
    exportable = {
        seed_job(user): True,
        seed_job(user, status=IngestStatus.committed): True,
        seed_job(user, status=IngestStatus.processing): False,
        seed_job(user, status=IngestStatus.failed): False,
        purged: False,
    }
    for job_id, expected in exportable.items():
        job = api_client.get(job_url(job_id), headers=user.token).json()
        assert job["permissions"]["canExportEval"] is expected, job["status"]


def test_local_only_shows_the_policy_the_card_is_read_under(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """The worker applies the group's current setting to every task, so a card from before the switch shows it"""
    user = unique_user_fn_scoped
    sent_local = seed_job(user, local_only=True)
    ready = seed_job(user)
    committed = seed_job(user, status=IngestStatus.committed)

    def local_only() -> dict[str, bool]:
        items = api_client.get(JOBS, headers=user.token).json()["items"]
        listed = {item["id"]: item["localOnly"] for item in items}
        for job_id in (sent_local, ready, committed):
            assert api_client.get(job_url(job_id), headers=user.token).json()["localOnly"] is listed[str(job_id)]
        return listed

    assert local_only() == {str(sent_local): True, str(ready): False, str(committed): False}

    with session_context() as session:
        repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
        repos.settings.upsert(RecipeIngestionSettingsUpdate(local_only=True))
    # a committed card was read before, under the policy it had then
    assert local_only() == {str(sent_local): True, str(ready): True, str(committed): False}


def test_state(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    proposal = CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="name"), text="Banana Cake")
    job_id = seed_job(
        user,
        proposals=[proposal],
        task_kind=IngestTaskKind.reread.value,
        task_state=IngestTaskState.running.value,
        progress_key="recipe-ingest.progress.reading-card",
        error_code=IngestErrorCode.provider_failed.value,
        error_params={"detail": "AuthenticationError"},
    )

    response = api_client.get(job_url(job_id, "state"), headers=user.token)
    assert response.status_code == 200
    assert response.json() == {
        "draftVersion": 1,
        "status": "ready",
        "task": {
            "kind": "reread",
            "state": "running",
            "mode": None,  # a region re-read; an extract task names what it does
            "refs": [],
            "progressKey": "recipe-ingest.progress.reading-card",
            "cancelRequested": False,
        },
        "proposalIds": [str(proposal.id)],
        "error": {"code": "provider_failed", "params": {"detail": "AuthenticationError"}},
    }


def test_unknown_job_is_404_without_a_message(api_client: TestClient, unique_user: TestUser):
    for response in (
        api_client.get(job_url(uuid4()), headers=unique_user.token),
        api_client.get(job_url(uuid4(), "state"), headers=unique_user.token),
        api_client.post(job_url(uuid4(), "retry"), headers=unique_user.token),
    ):
        assert_code(response, 404, "not_found")


# ==================================================================================================================
# Tasks


def _region(**changes: Any) -> dict[str, Any]:
    return {"page": 0, "x": 0.1, "y": 0.2, "width": 0.5, "height": 0.1, "target": {"field": "name"}, **changes}


def test_reread_queues_a_priority_task_with_its_region(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    draft = banana_draft()
    job_id = seed_job(user, draft=draft)
    step = draft.steps[1]

    region = _region(target={"field": "steps", "ref": str(step.id)})
    response = api_client.post(job_url(job_id, "reread"), json=region, headers=user.token)
    assert response.status_code == 202
    assert response.json()["task"] == {
        "kind": "reread",
        "state": "queued",
        "mode": None,
        "refs": [],
        "progressKey": None,
        "cancelRequested": False,
    }

    row = job_row(job_id)
    assert row["task_kind"] == "reread"
    assert row["task_priority"] == limits.PRIORITY_REREAD
    assert row["attempts"] == 0
    assert row["task_payload"] == {
        "page": 0,
        "x": 0.1,
        "y": 0.2,
        "width": 0.5,
        "height": 0.1,
        "target": {"field": "steps", "ref": str(step.id)},
    }

    # a second one while the first is pending: the page queues it itself
    assert_code(api_client.post(job_url(job_id, "reread"), json=_region(), headers=user.token), 409, "busy")
    assert_code(api_client.post(job_url(job_id, "reextract"), headers=user.token), 409, "busy")


def test_reread_validates_the_region_and_target(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    url = job_url(job_id, "reread")

    for invalid in (
        _region(x=0.8, width=0.5),  # past the right edge
        _region(height=0.01),  # too thin
        _region(page=-1),
        _region(extra=True),
    ):
        assert api_client.post(url, json=invalid, headers=user.token).status_code == 422

    assert_code(api_client.post(url, json=_region(page=3), headers=user.token), 422, "unknown_page")
    unknown_step = _region(target={"field": "steps", "ref": str(uuid4())})
    assert_code(api_client.post(url, json=unknown_step, headers=user.token), 422, "unknown_target")
    assert job_row(job_id)["task_state"] is None


@pytest.mark.parametrize("field", ["ingredients", "steps"])
def test_a_reread_without_a_ref_is_for_a_new_line(api_client: TestClient, unique_user_fn_scoped: TestUser, field: str):
    """A line the reading missed, or the first of an empty section: the review page adds the reading as a new one"""
    user = unique_user_fn_scoped
    job_id = seed_job(user, draft=banana_draft(**{field: []}))

    response = api_client.post(job_url(job_id, "reread"), json=_region(target={"field": field}), headers=user.token)
    assert response.status_code == 202, response.text
    assert job_row(job_id)["task_payload"]["target"] == {"field": field, "ref": None}


def test_reextract_and_retry(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    ready = seed_job(user, attempts=3, rate_limit_retries=2)
    failed = seed_job(user, status=IngestStatus.failed, error_code=IngestErrorCode.no_recipe_found.value)

    response = api_client.post(job_url(ready, "reextract"), headers=user.token)
    assert response.status_code == 202
    row = job_row(ready)
    assert (row["status"], row["task_kind"], row["task_state"]) == ("ready", "extract", "queued")
    assert row["task_priority"] == limits.PRIORITY_EXTRACT
    assert (row["attempts"], row["rate_limit_retries"]) == (0, 0)  # every task starts clean

    assert_code(api_client.post(job_url(ready, "retry"), headers=user.token), 409, "invalid_status")

    response = api_client.post(job_url(failed, "retry"), headers=user.token)
    assert response.status_code == 202
    assert response.json()["status"] == "processing"
    row = job_row(failed)
    assert (row["status"], row["task_kind"], row["task_state"], row["error_code"]) == (
        "processing",
        "extract",
        "queued",
        None,
    )
    assert_code(api_client.post(job_url(failed, "reextract"), headers=user.token), 409, "invalid_status")


def test_cancel(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    queued_first_read = seed_job(
        user, status=IngestStatus.processing, task_kind="extract", task_state="queued", task_priority=10
    )
    queued_reread = seed_job(user, task_kind="reread", task_state="queued")
    running = seed_job(user, task_kind="reread", task_state="running", lease_token=uuid4())
    idle = seed_job(user)

    state = api_client.post(job_url(queued_first_read, "cancel"), headers=user.token).json()
    assert state["status"] == "failed"
    assert state["error"]["code"] == "cancelled"
    assert state["task"] is None

    state = api_client.post(job_url(queued_reread, "cancel"), headers=user.token).json()
    assert (state["status"], state["task"]) == ("ready", None)

    state = api_client.post(job_url(running, "cancel"), headers=user.token).json()
    assert state["task"]["cancelRequested"] is True

    response = api_client.post(job_url(idle, "cancel"), headers=user.token)
    assert response.status_code == 200
    assert response.json()["task"] is None


# ==================================================================================================================
# Rotate and discard


def test_rotate_rewrites_the_page_and_its_metadata(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    before = api_client.get(job_url(job_id), headers=user.token).json()["pages"][0]

    response = api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token)
    assert response.status_code == 200
    page = response.json()
    assert (page["width"], page["height"]) == (before["height"], before["width"])
    assert (page["rotation"], page["rotationSource"], page["oriented"]) == (90, "user", True)
    assert page["viewUrl"] != before["viewUrl"]

    stored = job_row(job_id)["pages"][0]
    assert stored["rotation"] == 90
    assert stored["ocr"] is None
    with Image.open(storage.page_dir(UUID(user.group_id), job_id, 0) / "page.jpg") as image:
        assert image.size == (page["width"], page["height"])

    assert (
        api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 45}, headers=user.token).status_code
        == 422
    )
    assert_code(
        api_client.post(job_url(job_id, "pages", 5, "rotate"), json={"degrees": 90}, headers=user.token),
        404,
        "not_found",
    )


def test_rotate_is_refused_while_a_task_is_active(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user, task_kind="extract", task_state="queued")
    page_file = storage.page_dir(UUID(user.group_id), job_id, 0) / "page.jpg"
    before = page_file.read_bytes()

    response = api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 180}, headers=user.token)
    assert_code(response, 409, "busy")
    assert page_file.read_bytes() == before

    committed = seed_job(user, status=IngestStatus.committed)
    response = api_client.post(job_url(committed, "pages", 0, "rotate"), json={"degrees": 180}, headers=user.token)
    assert_code(response, 409, "invalid_status")


def test_discard_deletes_the_row_and_its_files(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user, task_kind="reread", task_state="running", lease_token=uuid4())
    job_dir = storage.job_dir(UUID(user.group_id), job_id)
    assert job_dir.is_dir()

    response = api_client.delete(job_url(job_id), headers=user.token)
    assert response.status_code == 204
    assert job_row(job_id) == {}
    assert not job_dir.exists()
    assert_code(api_client.delete(job_url(job_id), headers=user.token), 404, "not_found")

    committing = seed_job(user, status=IngestStatus.committing)
    assert_code(api_client.delete(job_url(committing), headers=user.token), 409, "invalid_status")
    assert storage.job_dir(UUID(user.group_id), committing).is_dir()


def test_file_writing_routes_answer_503_while_paused(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    marker = storage.pause_marker_path()
    marker.write_text(f"{time.time():.3f}")
    try:
        for response in (
            api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token),
            api_client.delete(job_url(job_id), headers=user.token),
            api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1}, headers=user.token),
        ):
            assert response.status_code == 503
            assert response.headers["Retry-After"] == str(limits.PAUSED_RETRY_AFTER)
            assert response.json()["detail"]["code"] == "paused_for_restore"
            assert response.json()["detail"]["message"]
        # reading isn't writing
        assert api_client.get(job_url(job_id), headers=user.token).status_code == 200
    finally:
        marker.unlink(missing_ok=True)
    assert job_row(job_id)["status"] == "ready"
