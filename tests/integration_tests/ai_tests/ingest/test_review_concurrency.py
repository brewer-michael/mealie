"""
Concurrent draft saves (docs/ai/PHASE2.md §3.3, §6.6, §18 Concurrency), on SQLite and PostgreSQL: two saves of the
same `draftVersion` give one 200 and one 409, and a proposal written while a save is in flight survives it.
"""

import threading
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_jobs_api import fake_compute_flags, job_row, job_url, seed_job

from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import update_job_json
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardFlag,
    CardProposal,
    CardProposalKind,
    ExtractionMeta,
    FlagResolution,
    ProposalTarget,
)
from mealie.services.ai.ingest.pipeline import flags as card_flags
from tests.utils.fixture_schemas import TestUser


def test_two_saves_of_the_same_version_give_one_200_and_one_409(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]

    # both saves read the row before either writes: they meet here, between the read and the write
    barrier = threading.Barrier(2, timeout=20)
    calls = 0
    lock = threading.Lock()

    def meeting_flags(
        draft: CardDraft, extraction: ExtractionMeta | None, resolutions: Mapping[str, FlagResolution]
    ) -> list[CardFlag]:
        nonlocal calls
        with lock:
            calls += 1
            first_two = calls <= 2
        if first_two:
            barrier.wait()
        return fake_compute_flags(draft, extraction, resolutions)

    monkeypatch.setattr(card_flags, "compute_flags", meeting_flags)

    results: dict[str, Any] = {}

    def save(name: str) -> None:
        body = {"draftVersion": 1, "draft": {**draft, "name": name}}
        results[name] = api_client.put(job_url(job_id), json=body, headers=user.token)

    threads = [threading.Thread(target=save, args=(name,)) for name in ("Phone", "Desktop")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(response.status_code for response in results.values())
    assert statuses == [200, 409]
    winner = next(name for name, response in results.items() if response.status_code == 200)
    loser = next(response for response in results.values() if response.status_code == 409)
    assert loser.json()["detail"] == {"code": "version_conflict", "current": 2}

    row = job_row(job_id)
    assert row["draft_version"] == 2
    assert row["draft"]["name"] == winner
    assert row["title"] == winner


def test_a_proposal_written_during_a_save_survives_it(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    draft = api_client.get(job_url(job_id), headers=user.token).json()["draft"]
    proposal = CardProposal(kind=CardProposalKind.region, target=ProposalTarget(field="name"), text="Banana Cake")
    calls = 0

    def flags_while_a_reread_lands(
        draft: CardDraft, extraction: ExtractionMeta | None, resolutions: Mapping[str, FlagResolution]
    ) -> list[CardFlag]:
        nonlocal calls
        calls += 1
        if calls == 1:
            # a re-read's finalize, in its own session, between the save's read and its write
            with session_context() as session:
                update_job_json(session, job_id, lambda row: {"proposals": [*(row["proposals"] or []), proposal]})
        return fake_compute_flags(draft, extraction, resolutions)

    monkeypatch.setattr(card_flags, "compute_flags", flags_while_a_reread_lands)

    response = api_client.put(
        job_url(job_id), json={"draftVersion": 1, "draft": {**draft, "name": "Edited"}}, headers=user.token
    )
    assert response.status_code == 200, response.text
    assert response.json()["draftVersion"] == 2
    assert calls == 2  # the save lost the race for row_version once, read the row again and retried

    row = job_row(job_id)
    assert row["draft"]["name"] == "Edited"
    assert [p["id"] for p in row["proposals"]] == [str(proposal.id)]
    assert row["row_version"] == 2
