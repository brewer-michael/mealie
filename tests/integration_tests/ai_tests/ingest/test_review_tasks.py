"""
Review saves meeting the runner's results (docs/ai/PHASE2.md §3.1, §3.3, §6.6), through the review routes and the real
`finalize`: a re-extract replaces a draft nobody edited and becomes a whole-card proposal on an edited one, and a
task's finalize racing a save loses neither. Runs on SQLite and PostgreSQL.
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import banana_draft, fake_compute_flags, job_row, job_url, seed_job

from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import IngestQueue, utcnow
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardFlag,
    CardProposal,
    CardProposalKind,
    ExtractionMeta,
    FlagResolution,
    PageMeta,
    ProposalTarget,
)
from mealie.services.ai.ingest.pipeline import flags as card_flags
from mealie.services.ai.ingest.runner import finalize
from mealie.services.ai.ingest.runner.types import ExtractResult, RereadResult
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # the stand-in rules of the other review tests, for the saves and for the kept draft of a proposal
    monkeypatch.setattr(card_flags, "compute_flags", fake_compute_flags)
    monkeypatch.setattr(finalize, "compute_flags", fake_compute_flags)


def _claim(job_id: UUID) -> UUID:
    """What the dispatcher does with the job's queued task: take it with a new lease token"""
    token = uuid4()
    with session_context() as session:
        assert IngestQueue(session).claim(job_id, token=token, owner="test", now=utcnow())
    return token


def _reading(job_id: UUID, name: str) -> ExtractResult:
    """A re-extract's result: the card read again, under a different name"""
    draft = banana_draft(name=name)
    return ExtractResult(
        draft=draft,
        flags=fake_compute_flags(draft, None, {}),
        transcription=f"{name}\n1 T. coconut oil (melted)",
        extraction=ExtractionMeta(read_path="image", provider="Claude", model="claude-sonnet"),
        pages=[PageMeta.model_validate(page) for page in job_row(job_id)["pages"]],
    )


def _finalize_extract(job_id: UUID, token: UUID, name: str) -> finalize.Applied:
    with session_context() as session:
        return finalize.finalize_extract(session, job_id, token, _reading(job_id, name)).applied


def _get(api_client: TestClient, user: TestUser, job_id: UUID) -> dict[str, Any]:
    response = api_client.get(job_url(job_id), headers=user.token)
    assert response.status_code == 200, response.text
    return response.json()


def _put(api_client: TestClient, user: TestUser, job_id: UUID, body: dict[str, Any]) -> dict[str, Any]:
    response = api_client.put(job_url(job_id), json=body, headers=user.token)
    assert response.status_code == 200, response.text
    return response.json()


def _reextract(api_client: TestClient, user: TestUser, job_id: UUID) -> None:
    assert api_client.post(job_url(job_id, "reextract"), headers=user.token).status_code == 202


def test_a_reextract_replaces_an_unedited_draft_and_proposes_on_an_edited_one(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    job = _get(api_client, user, job_id)

    # keeping the blank as written doesn't edit the draft: it still counts as unedited
    [blank] = [flag for flag in job["flags"] if flag["kind"] == "blank"]
    body = {"draftVersion": 1, "draft": job["draft"], "flagResolutions": {blank["id"]: "kept"}}
    assert _put(api_client, user, job_id, body)["draftVersion"] == 1

    _reextract(api_client, user, job_id)
    assert _finalize_extract(job_id, _claim(job_id), "Banana Bread") == finalize.Applied.draft
    job = _get(api_client, user, job_id)
    assert (job["draft"]["name"], job["title"], job["draftVersion"]) == ("Banana Bread", "Banana Bread", 2)
    assert job["proposals"] == []
    assert job["task"] is None

    # an edit: the next reading is offered, not applied
    body = {"draftVersion": 2, "draft": {**job["draft"], "name": "Banana Bread for Two"}}
    assert _put(api_client, user, job_id, body)["draftVersion"] == 3
    _reextract(api_client, user, job_id)
    assert _finalize_extract(job_id, _claim(job_id), "Banana Loaf") == finalize.Applied.proposal

    job = _get(api_client, user, job_id)
    assert (job["draft"]["name"], job["draftVersion"]) == ("Banana Bread for Two", 3)
    [proposal] = job["proposals"]
    assert (proposal["kind"], proposal["draft"]["name"]) == ("full", "Banana Loaf")

    # using it is an ordinary save, which removes it
    body = {"draftVersion": 3, "draft": proposal["draft"], "resolvedProposalIds": [proposal["id"]]}
    assert _put(api_client, user, job_id, body)["draftVersion"] == 4
    job = _get(api_client, user, job_id)
    assert (job["draft"]["name"], job["proposals"]) == ("Banana Loaf", [])


def test_a_finalize_racing_a_save_loses_neither(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped

    # 1. A re-extract's finalize reads the unedited draft, then a save lands before it writes: the reading becomes a
    # proposal beside the saved edit
    job_id = seed_job(user)
    draft = _get(api_client, user, job_id)["draft"]
    _reextract(api_client, user, job_id)
    token = _claim(job_id)
    counts = finalize._counts
    saves: list[int] = []

    def counts_while_a_save_lands(flags: list[CardFlag]) -> dict[str, int]:
        if not saves:
            body = {"draftVersion": 1, "draft": {**draft, "name": "Grandma's Banana Mug Cake"}}
            saves.append(_put(api_client, user, job_id, body)["draftVersion"])
        return counts(flags)

    monkeypatch.setattr(finalize, "_counts", counts_while_a_save_lands)
    assert _finalize_extract(job_id, token, "Banana Bread") == finalize.Applied.proposal
    monkeypatch.setattr(finalize, "_counts", counts)

    assert saves == [2]
    job = _get(api_client, user, job_id)
    assert (job["draft"]["name"], job["draftVersion"]) == ("Grandma's Banana Mug Cake", 2)
    assert [proposal["draft"]["name"] for proposal in job["proposals"]] == ["Banana Bread"]
    assert job["task"] is None

    # 2. A save reads the row, then a re-read's finalize lands before it writes: the save reads again and keeps the
    # proposal, and the edit is saved
    job_id = seed_job(user)
    draft = _get(api_client, user, job_id)["draft"]
    region = {"page": 0, "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.1, "target": {"field": "name"}}
    assert api_client.post(job_url(job_id, "reread"), json=region, headers=user.token).status_code == 202
    token = _claim(job_id)
    reading = CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="name"), text="Banana Cake")
    landed: list[finalize.Applied] = []

    def flags_while_a_reread_lands(
        draft: CardDraft, extraction: ExtractionMeta | None, resolutions: Mapping[str, FlagResolution], **kwargs: Any
    ) -> list[CardFlag]:
        if not landed:
            with session_context() as session:
                landed.append(finalize.finalize_reread(session, job_id, token, RereadResult(proposal=reading)).applied)
        return fake_compute_flags(draft, extraction, resolutions, **kwargs)

    monkeypatch.setattr(card_flags, "compute_flags", flags_while_a_reread_lands)
    saved = _put(api_client, user, job_id, {"draftVersion": 1, "draft": {**draft, "name": "Edited"}})

    assert landed == [finalize.Applied.proposal]
    assert saved["draftVersion"] == 2
    job = _get(api_client, user, job_id)
    assert job["draft"]["name"] == "Edited"
    assert [proposal["id"] for proposal in job["proposals"]] == [str(reading.id)]
    assert job["task"] is None

    # 3. A re-extract replaces the unedited draft while a save is in flight: the save is refused with the version it
    # missed (the page offers a reload), never written over the new reading
    monkeypatch.setattr(card_flags, "compute_flags", fake_compute_flags)
    job_id = seed_job(user)
    draft = _get(api_client, user, job_id)["draft"]
    _reextract(api_client, user, job_id)
    token = _claim(job_id)
    replaced: list[finalize.Applied] = []

    def flags_while_a_reextract_lands(
        draft: CardDraft, extraction: ExtractionMeta | None, resolutions: Mapping[str, FlagResolution], **kwargs: Any
    ) -> list[CardFlag]:
        if not replaced:
            replaced.append(_finalize_extract(job_id, token, "Banana Bread"))
        return fake_compute_flags(draft, extraction, resolutions, **kwargs)

    monkeypatch.setattr(card_flags, "compute_flags", flags_while_a_reextract_lands)
    response = api_client.put(
        job_url(job_id), json={"draftVersion": 1, "draft": {**draft, "name": "Edited"}}, headers=user.token
    )

    assert replaced == [finalize.Applied.draft]
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "version_conflict", "current": 2}
    row = job_row(job_id)
    assert (row["draft"]["name"], row["draft_version"], row["task_state"]) == ("Banana Bread", 2, None)
