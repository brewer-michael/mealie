"""
Recipe card ingestion's tables (docs/ai/PHASE2.md §13, §17): they're deleted along with their group, household or
notifier, and every row, column and file survives a backup and restore unchanged. Runs on SQLite and PostgreSQL.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.db.models.group.ai_providers import AIProvider
from mealie.db.models.group.ai_routing import AIUsageLog
from mealie.db.models.household.events import GroupEventNotifierModel
from mealie.db.models.recipe_ingest import (
    AIEventNotifierOptions,
    RecipeIngestionBatch,
    RecipeIngestionJob,
    RecipeIngestionSettings,
)
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.schema.household.group_events import GroupEventNotifierSave
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
    IngestErrorCode,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    PageMeta,
    ProposalTarget,
    RecipeIngestionSettingsUpdate,
)
from mealie.services.ai.ingest import storage
from mealie.services.backups_v2.backup_v2 import BackupV2
from tests.utils import api_routes, random_string
from tests.utils.fixture_schemas import TestUser


def _page(index: int) -> PageMeta:
    return PageMeta(
        index=index,
        width=1536,
        height=2048,
        view_width=1536,
        view_height=2048,
        rotation=90 if index else 0,
        raw_sha256=f"{index}" * 64,
        page_sha256="f" * 64,
        format="mpo",
        raw_bytes=2_100_000,
        ocr={"text": "Banana Mug Cake\\n1 T. coconut oil", "confidence": 49.0},
    )


def _draft() -> CardDraft:
    food_id, step_id = uuid4(), uuid4()
    return CardDraft(
        name="Banana Mug Cake",
        attribution="From Grandma Jo",
        ingredients=[
            CardDraftIngredient(
                original_text="1 T. coconut oil (melted)",
                quantity=1,
                unit=CardDraftRef(id=uuid4(), name="tablespoon"),
                food=CardDraftRef(id=food_id, name="coconut oil"),
                note="melted",
                display="1 tbsp coconut oil (melted)",
                parse_confidence=0.97,
                extracted_hash="e" * 16,
            )
        ],
        steps=[CardDraftStep(id=step_id, text="Microwave for [blank] minutes")],
        tags=[CardDraftRef(id=uuid4(), name="Dessert")],
    )


def _json_columns(status: IngestStatus) -> dict[str, Any]:
    """JSON holding dashed UUIDs and `created_at` keys, which a restore must leave as they are (F15)"""
    draft = _draft()
    step = draft.steps[0]
    return {
        "draft": draft,
        "flags": [
            CardFlag(
                id=f"blank:steps:{step.id}",
                kind=CardFlagKind.blank,
                severity=CardFlagSeverity.error,
                source=CardFlagSource.marker,
                field="steps",
                ref=str(step.id),
                params={"created_at": "2026-10-03T12:00:00", "job": str(uuid4())},
            )
        ],
        "proposals": [
            CardProposal(
                kind=CardProposalKind.region,
                target=ProposalTarget(field="steps", ref=str(step.id)),
                text="Microwave for 2 minutes",
                alternatives=["1 1/2"],
            )
        ],
        "extraction": ExtractionMeta(read_path="image", language="en", provider="Claude", model="claude-sonnet"),
        "transcription": "Banana Mug Cake\n1 T. coconut oil (melted)\nMicrowave for [blank] minutes",
        "error_params": {"detail": "AuthenticationError (HTTP 401)", "created_at": str(uuid4())}
        if status == IngestStatus.failed
        else None,
    }


def _seed(user: TestUser) -> dict[str, Any]:
    """One batch with a job in every state and every column set, the group's settings and a notifier's options"""
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    now = utcnow().replace(microsecond=0)
    seeded: dict[str, Any] = {"jobs": [], "files": {}}

    with session_context() as session:
        repos = IngestRepos(session, group_id, household_id)
        batch_id = repos.batches.create(source=IngestSource.app, created_by=user.user_id, locale="en-US", now=now)
        session.execute(
            sa.update(RecipeIngestionBatch)
            .where(RecipeIngestionBatch.id == batch_id)
            .values(sealed_at=now + timedelta(minutes=1), notified_at=now + timedelta(minutes=2))
        )
        session.commit()
        seeded["batch"] = batch_id

        for position, status in enumerate(IngestStatus):
            running = status in (IngestStatus.processing, IngestStatus.ready)
            job_id = repos.jobs.create(
                {
                    "batch_id": batch_id,
                    "position": position,
                    "created_by": user.user_id,
                    "committed_by": user.user_id if status == IngestStatus.committed else None,
                    "source": IngestSource.app.value,
                    # a UUID-named upload: the prefix keeps a restore from reformatting it
                    "source_name": f"upload/{uuid4()}",
                    "integration_id": "generic",
                    "locale": "en-US",
                    "local_only": position % 2 == 0,
                    "status": status.value,
                    "title": "Banana Mug Cake",
                    "draft_version": 3,
                    "extracted_version": 2,
                    "row_version": 7,
                    "error_count": 1,
                    "warning_count": 2,
                    "task_kind": IngestTaskKind.reread.value if running else None,
                    "task_state": IngestTaskState.running.value if running else None,
                    "task_priority": 0,
                    "attempts": 2,
                    "rate_limit_retries": 1,
                    "task_payload": {"page": 0, "x": 0.1, "target": {"field": "steps", "ref": str(uuid4())}},
                    "not_before": now + timedelta(seconds=30),
                    "lease_expires_at": now + timedelta(seconds=120),
                    "task_started_at": now,
                    "lease_token": uuid4() if running else None,
                    "lease_owner": "mealie:1:abc",
                    "cancel_requested": status == IngestStatus.processing,
                    "progress_key": "recipe-ingest.progress.reading-card",
                    "pages": [_page(0), _page(1)],
                    "source_sha256": f"{position}" * 64,
                    "error_code": IngestErrorCode.provider_failed.value if status == IngestStatus.failed else None,
                    # a waiting card's lift backoff: its count and its time come back as they were
                    "lift_retries": 2 if status == IngestStatus.failed else 0,
                    "lift_retry_at": now + timedelta(minutes=20) if status == IngestStatus.failed else None,
                    "commit_recipe_id": uuid4()
                    if status in (IngestStatus.committing, IngestStatus.committed)
                    else None,
                    "recipe_id": uuid4() if status == IngestStatus.committed else None,
                    "commit_asset_token": "Zk3vQ9-_aB1cD2eF3gH4iA" if status != IngestStatus.processing else None,
                    "commit_started_at": now if status in (IngestStatus.committing, IngestStatus.committed) else None,
                    "committed_at": now + timedelta(seconds=1) if status == IngestStatus.committed else None,
                    **_json_columns(status),
                }
            )
            seeded["jobs"].append(job_id)

            with storage.ingest_write():
                page_dir = storage.create_job_dir(group_id, job_id, 1) / "pages" / "0"
                storage.atomic_write_bytes(page_dir / "page.jpg", f"page of {job_id}".encode())
            seeded["files"][page_dir / "page.jpg"] = f"page of {job_id}".encode()

        repos.settings.upsert(RecipeIngestionSettingsUpdate(local_only=True, cross_read=True))

        household_repos = get_repositories(session, group_id=group_id, household_id=household_id)
        notifier = household_repos.group_event_notifier.create(
            GroupEventNotifierSave(
                name=random_string(),
                apprise_url="jsons://ha.local/api/webhook/x",
                group_id=group_id,
                household_id=household_id,
            )
        )
        repos.notifier_options.set(notifier.id, recipe_ingestion_ready=True)
        seeded["notifier"] = notifier.id

        provider = household_repos.group_ai_providers.create(
            AIProviderCreate(
                name=random_string(), model="qwen3-vl", api_key="k", base_url="http://127.0.0.1/v1", runs_locally=True
            )
        )
        seeded["provider"] = provider.id
        usage = household_repos.group_ai_usage.create(
            AIUsageLogCreate(
                provider_id=provider.id,
                provider_name=provider.name,
                model=provider.model,
                protocol=provider.protocol,
                slot=AIProviderSlot.image,
                feature="OpenAIRecipeCardTranscription",
                success=True,
                job_id=seeded["jobs"][0],
            )
        )
        seeded["usage"] = usage.id

    return seeded


def _rows(model: type, *criteria: sa.ColumnElement[bool]) -> list[dict[str, Any]]:
    with session_context() as session:
        rows = session.execute(sa.select(*model.__table__.columns).where(*criteria)).mappings().all()
        return sorted((dict(row) for row in rows), key=lambda row: str(row["id"]))


def _snapshot(user: TestUser, seeded: dict[str, Any]) -> dict[str, Any]:
    group_id = UUID(user.group_id)
    return {
        "jobs": _rows(RecipeIngestionJob, RecipeIngestionJob.group_id == group_id),
        "batches": _rows(RecipeIngestionBatch, RecipeIngestionBatch.group_id == group_id),
        "settings": _rows(RecipeIngestionSettings, RecipeIngestionSettings.group_id == group_id),
        "notifier": _rows(AIEventNotifierOptions, AIEventNotifierOptions.notifier_id == seeded["notifier"]),
        "usage": _rows(AIUsageLog, AIUsageLog.id == seeded["usage"]),
        "runs_locally": _rows_runs_locally(seeded["provider"]),
    }


def _rows_runs_locally(provider_id: UUID) -> bool:
    with session_context() as session:
        return session.execute(sa.select(AIProvider.runs_locally).where(AIProvider.id == provider_id)).scalar_one()


def test_ingest_rows_and_files_survive_a_backup(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    seeded = _seed(user)
    before = _snapshot(user, seeded)

    assert len(before["jobs"]) == len(IngestStatus)
    assert {row["status"] for row in before["jobs"]} == {status.value for status in IngestStatus}
    assert all(row["source_name"].startswith("upload/") for row in before["jobs"])
    assert before["usage"][0]["job_id"] == seeded["jobs"][0]
    assert before["runs_locally"] is True

    backup_v2 = BackupV2(get_app_settings().DB_URL)
    try:
        backup_v2.restore(backup_v2.backup())
    finally:
        backup_v2.db_exporter.engine.dispose()

    after = _snapshot(user, seeded)
    # every column comes back as it was, except that a task running when the backup was taken is queued again with
    # no lease and its attempt given back (`IngestQueue.requeue_all_running`, §3.9), which touches `update_at`
    requeued = {
        "task_state": IngestTaskState.queued.value,
        "lease_token": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "task_started_at": None,
        "progress_key": None,
    }
    expected = dict(before)
    expected["jobs"] = []
    for row, restored in zip(before["jobs"], after["jobs"], strict=True):
        if row["task_state"] == IngestTaskState.running.value:
            assert restored["update_at"] >= row["update_at"]
            row = {**row, **requeued, "attempts": row["attempts"] - 1, "update_at": restored["update_at"]}
        expected["jobs"].append(row)
    assert sum(row["task_state"] == IngestTaskState.queued.value for row in expected["jobs"]) == 2
    assert after == expected
    # the strings a restore would reformat if it took them for UUIDs came back as they were
    for row in after["jobs"]:
        assert UUID(row["source_name"].removeprefix("upload/"))
        assert "-" in row["source_name"]
        draft = CardDraft.model_validate(row["draft"])
        assert draft.steps[0].text == "Microwave for [blank] minutes"
        assert row["flags"][0]["params"]["created_at"] == "2026-10-03T12:00:00"
    for path, content in seeded["files"].items():
        assert path.read_bytes() == content
    assert not storage.is_paused()


def _admin_group(api_client: TestClient, admin_user: TestUser) -> tuple[UUID, UUID]:
    response = api_client.post(api_routes.admin_groups, json={"name": random_string()}, headers=admin_user.token)
    assert response.status_code == 201
    group = response.json()
    return UUID(group["id"]), UUID(group["households"][0]["id"])


def _count(model: type, *criteria: sa.ColumnElement[bool]) -> int:
    with session_context() as session:
        return session.execute(sa.select(sa.func.count()).select_from(model).where(*criteria)).scalar_one()


def _fill(group_id: UUID, household_id: UUID) -> UUID:
    with session_context() as session:
        repos = IngestRepos(session, group_id, household_id)
        batch_id = repos.batches.create(source=IngestSource.api, created_by=None)
        repos.jobs.create(
            {
                "batch_id": batch_id,
                "source": IngestSource.api.value,
                "status": IngestStatus.ready.value,
                "pages": [_page(0)],
                "source_sha256": "a" * 64,
            }
        )
        notifier = get_repositories(session, group_id=group_id, household_id=household_id).group_event_notifier.create(
            GroupEventNotifierSave(name="HA", apprise_url="json://ha", group_id=group_id, household_id=household_id)
        )
        repos.notifier_options.set(notifier.id, recipe_ingestion_ready=True)
        return notifier.id


def test_rows_go_with_their_household_and_group(api_client: TestClient, admin_user: TestUser):
    group_id, first = _admin_group(api_client, admin_user)
    response = api_client.post(
        api_routes.admin_households, json={"groupId": str(group_id), "name": random_string()}, headers=admin_user.token
    )
    assert response.status_code == 201
    second = UUID(response.json()["id"])

    first_notifier = _fill(group_id, first)
    second_notifier = _fill(group_id, second)
    with session_context() as session:
        IngestRepos(session, group_id, None).settings.upsert(RecipeIngestionSettingsUpdate(local_only=True))

    response = api_client.delete(api_routes.admin_households_item_id(second), headers=admin_user.token)
    assert response.status_code == 200
    assert _count(RecipeIngestionJob, RecipeIngestionJob.household_id == second) == 0
    assert _count(RecipeIngestionBatch, RecipeIngestionBatch.household_id == second) == 0
    assert _count(AIEventNotifierOptions, AIEventNotifierOptions.notifier_id == second_notifier) == 0
    assert _count(RecipeIngestionJob, RecipeIngestionJob.household_id == first) == 1

    response = api_client.delete(api_routes.admin_groups_item_id(group_id), headers=admin_user.token)
    assert response.status_code == 200
    for model in (RecipeIngestionJob, RecipeIngestionBatch, RecipeIngestionSettings):
        assert _count(model, model.group_id == group_id) == 0
    assert _count(AIEventNotifierOptions, AIEventNotifierOptions.notifier_id == first_notifier) == 0
    assert _count(GroupEventNotifierModel, GroupEventNotifierModel.group_id == group_id) == 0
