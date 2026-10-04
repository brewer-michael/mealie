"""
Readings a backup restore cut off (docs/ai/PHASE2.md §3.9): a task whose lease a restore took keeps its result (or the
provider answers it got) for the card's next task, which applies them without asking a provider again; only for the
same kind, payload and pages, within a day. The dispatcher lets such a task finish, and the next task waits for it.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from ingest_runner_testing import FakeHandlers, Jobs, blocking, extract_result, reread_result, run, settle, wait_for

from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import TASK_CLEARED
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate
from mealie.schema.openai.general import OpenAIText
from mealie.schema.recipe_ingest import (
    CardDraftIngredient,
    CardProposalOrigin,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
)
from mealie.services.ai.ingest import limits, storage, tasks
from mealie.services.ai.ingest.pipeline.attachments import CardImage
from mealie.services.ai.ingest.runner import results
from mealie.services.ai.ingest.runner.answers import KeptAnswers
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.results import TaskKey
from mealie.services.ai.ingest.runner.types import ExtractResult, ParseLinesResult, TaskContext
from tests.unit_tests.services_tests.ai.test_ai_provider_fallback import FakeProviders
from tests.utils.fixture_schemas import TestUser

PAGES = ("b" * 64,)
"""The page hashes of `ingest_runner_testing.page()`, which every test job has"""


@pytest.fixture(autouse=True)
def results_folder() -> Iterator[Path]:
    """The results folder as each test leaves it: no restore recorded, nothing kept"""
    folder = storage.results_dir()
    before = storage.restored_at()
    yield folder
    if folder.is_dir():
        for path in folder.iterdir():
            if path.name != storage.RESTORED_NAME:
                path.unlink(missing_ok=True)
        if before is None:
            (folder / storage.RESTORED_NAME).unlink(missing_ok=True)
        else:
            storage.mark_restored(before)


def _key(job_id: UUID, kind: str = "extract", payload: Any = None, pages: tuple[str, ...] = PAGES) -> TaskKey:
    return TaskKey.of(job_id, kind, payload, pages)


# ==========================================
# Keeping and taking


def test_a_kept_result_is_taken_only_for_the_same_task_and_pages(results_folder: Path):
    job_id = uuid4()
    result = extract_result("Banana Mug Cake")
    assert results.keep(_key(job_id), result=result)

    path = results_folder / f"{job_id}.extract.json"
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)  # card text: the owner only
    kept = results.take(_key(job_id))
    assert kept is not None and isinstance(kept.result, ExtractResult)
    assert kept.result.draft == result.draft and kept.result.pages == result.pages and kept.result.flags == result.flags
    assert path.exists()  # until the next task's outcome is stored
    results.forget(job_id, "extract")
    assert not path.exists()

    # another payload, other pages, another kind: not this task's; the kept file goes
    for other in (_key(job_id, payload={"mode": "rebuild"}), _key(job_id, pages=("c" * 64,))):
        results.keep(_key(job_id), result=result)
        assert results.take(other) is None
        assert not path.exists()
    results.keep(_key(job_id), result=result)
    assert results.take(_key(job_id, kind="reread")) is None
    assert path.exists()  # a re-read's file is another one


def test_a_reread_keeps_its_proposal_for_the_pages_it_read(results_folder: Path):
    job_id = uuid4()
    payload = {"page": 0, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1, "target": {"field": "ingredients"}}
    result = reread_result("1/4 tsp salt")
    assert results.keep(_key(job_id, "reread", payload), result=result)
    kept = results.take(_key(job_id, "reread", dict(reversed(list(payload.items())))))  # key order doesn't count
    assert kept is not None and kept.result == result


def test_an_old_or_unreadable_kept_result_is_removed(results_folder: Path, monkeypatch: pytest.MonkeyPatch):
    job_id = uuid4()
    results.keep(_key(job_id), result=extract_result())
    path = results_folder / f"{job_id}.extract.json"
    content = json.loads(path.read_text())
    content["created_at"] = time.time() - limits.KEPT_RESULT_TTL - 1
    path.write_text(json.dumps(content))
    assert results.take(_key(job_id)) is None
    assert not path.exists()

    path.write_text("{not json")
    assert results.take(_key(job_id)) is None
    assert not path.exists()


def test_answers_are_kept_when_there_is_no_result(results_folder: Path):
    job_id = uuid4()
    answers = KeptAnswers({"k": {"answer": {"text": "Banana Mug Cake"}, "answered": ["Vision", "m"], "usage": []}})
    assert not results.keep(_key(job_id), answers=KeptAnswers())  # nothing to keep
    assert results.keep(_key(job_id), answers=answers)
    kept = results.take(_key(job_id))
    assert kept is not None and kept.result is None and kept.answers.entries == answers.entries


def test_the_purge_removes_what_no_task_used_within_a_day(results_folder: Path):
    fresh, old = uuid4(), uuid4()
    results.keep(_key(fresh), result=extract_result())
    results.keep(_key(old), result=extract_result())
    storage.mark_restored()
    day_ago = time.time() - limits.KEPT_RESULT_TTL - 60
    os.utime(results_folder / f"{old}.extract.json", (day_ago, day_ago))

    assert results.purge() == 1
    assert (results_folder / f"{fresh}.extract.json").exists()
    assert not (results_folder / f"{old}.extract.json").exists()
    assert (results_folder / storage.RESTORED_NAME).exists()  # the last restore's time stays


# ==========================================
# Tasks in flight


def test_a_task_in_flight_since_before_a_restore_is_waited_for(results_folder: Path):
    job_id, mine, theirs = uuid4(), uuid4(), uuid4()
    mark = results.begin(job_id, theirs)
    assert mark is not None and mark.exists()
    assert results.cut_off_in_flight(job_id, mine) == []  # no restore since it began: nothing to wait for

    storage.mark_restored()
    (cut_off,) = results.cut_off_in_flight(job_id, mine)
    assert cut_off.token == str(theirs)
    assert results.cut_off_in_flight(job_id, theirs) == []  # its own mark doesn't count
    results.end(mark)
    assert results.cut_off_in_flight(job_id, mine) == []


def test_the_mark_of_a_task_whose_process_is_gone_is_removed(results_folder: Path):
    job_id = uuid4()
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(30)
    mark = results.begin(job_id, uuid4())
    assert mark is not None
    content = json.loads(mark.read_text())
    content.update(pid=process.pid, began=time.time() - 60)
    mark.write_text(json.dumps(content))
    storage.mark_restored()

    assert results.cut_off_in_flight(job_id, uuid4()) == []
    assert not mark.exists()


def test_another_hosts_task_is_waited_for_until_its_deadline_could_have_passed(results_folder: Path):
    job_id = uuid4()
    mark = results.begin(job_id, uuid4())
    assert mark is not None
    content = json.loads(mark.read_text())
    content.update(host="another-host/boot/1", began=time.time() - 60)
    mark.write_text(json.dumps(content))
    storage.mark_restored()
    assert len(results.cut_off_in_flight(job_id, uuid4())) == 1

    content["began"] = time.time() - limits.TASK_DEADLINE - limits.LEASE - 60
    mark.write_text(json.dumps(content))
    assert results.cut_off_in_flight(job_id, uuid4()) == []


def test_the_next_task_waits_for_the_reading_still_running(results_folder: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "KEPT_RESULT_POLL", 0.01)
    job_id = uuid4()
    mark = results.begin(job_id, uuid4())
    storage.mark_restored()

    def answers_later() -> None:
        time.sleep(0.2)
        results.keep(_key(job_id), result=extract_result("Banana Bread"))
        results.end(mark)

    threading.Thread(target=answers_later, daemon=True).start()
    started = time.monotonic()
    kept = run(results.wait_for_kept(_key(job_id), uuid4(), deadline=time.monotonic() + 10, stopping=lambda: False))
    assert kept is not None and kept.result is not None and kept.result.draft.name == "Banana Bread"
    assert time.monotonic() - started >= 0.2


def test_the_wait_is_bounded(results_folder: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "KEPT_RESULT_POLL", 0.01)
    monkeypatch.setattr(limits, "KEPT_RESULT_WAIT", 0.1)
    job_id = uuid4()
    mark = results.begin(job_id, uuid4())  # never ends
    storage.mark_restored()
    try:
        kept = run(results.wait_for_kept(_key(job_id), uuid4(), deadline=time.monotonic() + 10, stopping=lambda: False))
        assert kept is None
        assert (
            run(results.wait_for_kept(_key(job_id), uuid4(), deadline=time.monotonic(), stopping=lambda: True)) is None
        )
    finally:
        results.end(mark)


# ==========================================
# The runner


def test_a_reading_a_restore_cut_off_is_applied_by_the_cards_next_task(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    results_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    The task is reading when a restore queues it again: the dispatcher lets it finish (its lease is gone), its
    result is kept, and the card's next task applies it without calling the handler
    """
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    monkeypatch.setattr(limits, "KEPT_RESULT_POLL", 0.01)
    gate = threading.Event()
    handlers.default = blocking(gate, lambda: extract_result("Banana Bread"))
    job_id = jobs.create()

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        # what a restore does once it has replaced the database: record itself, then queue every running task again
        storage.mark_restored()
        jobs.update(
            job_id,
            task_state=IngestTaskState.queued.value,
            lease_token=None,
            lease_owner=None,
            lease_expires_at=None,
            task_started_at=None,
            attempts=0,
        )
        await dispatcher.run_once()  # the heartbeat finds the lease gone, and claims the queued task again
        await wait_for(lambda: len(dispatcher.running_tasks) == 2)
        await asyncio.sleep(0.1)
        assert len(handlers.calls) == 1  # the next task waits for the one still reading
        gate.set()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    assert (row["status"], row["title"], row["task_state"], row["attempts"]) == (
        IngestStatus.ready,
        "Banana Bread",
        None,
        1,
    )
    assert len(handlers.calls) == 1  # read once in all
    assert [path.name for path in results_folder.iterdir() if path.name.startswith(str(job_id))] == []


def test_without_a_restore_a_task_whose_lease_is_gone_is_stopped_and_nothing_is_kept(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    results_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0)
    job_id = jobs.ready(kind=IngestTaskKind.extract, state=IngestTaskState.queued)
    handlers.default = blocking(threading.Event())

    async def scenario() -> None:
        await dispatcher.run_once()
        await wait_for(lambda: len(handlers.calls) == 1)
        jobs.update(job_id, status=IngestStatus.committing.value, **TASK_CLEARED)  # a commit
        await dispatcher.run_once()
        await settle(dispatcher, timeout=5)

    run(scenario())
    assert len(handlers.calls) == 1
    assert not (results_folder / f"{job_id}.extract.json").exists()


def test_a_result_dropped_for_another_reason_is_not_kept(
    dispatcher: IngestDispatcher, jobs: Jobs, handlers: FakeHandlers, results_folder: Path
):
    """A card discarded or committed while it was read: its result goes, and nothing is kept for later"""
    job_id = jobs.create()

    async def committed_meanwhile(ctx: TaskContext) -> Any:
        jobs.update(job_id, status=IngestStatus.committing.value, **TASK_CLEARED)
        return extract_result()

    handlers.default = committed_meanwhile

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    assert not (results_folder / f"{job_id}.extract.json").exists()


# ==========================================
# Replaying a provider's answers


def _provider(user: TestUser, name: str) -> Any:
    return user.repos.group_ai_providers.create(AIProviderCreate(name=name, model="m", api_key="k"))


def test_an_answer_got_before_a_restore_is_replayed_for_the_same_request(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    vision = _provider(user, "Vision")
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=vision.id, image_provider_id=vision.id, audio_provider_id=None),
    )
    fake = FakeProviders().install(monkeypatch)
    image = tmp_path / "view.jpg"
    image.write_bytes(b"\xff\xd8\xff card")
    answers = KeptAnswers()

    async def ask(service: Any, picture: Path, message: str = "card") -> Any:
        return await service.get_response(
            "read the card", message, response_schema=OpenAIText, attachments=[CardImage(path=picture)]
        )

    with session_context() as session:
        repos = get_repositories(session, group_id=user.repos.group_id, household_id=user.repos.household_id)
        first = tasks._KeptAnswersService(repos, answers)
        assert run(ask(first, image)) == OpenAIText(text="from Vision")
        assert fake.calls == ["Vision"] and len(answers) == 1

        # the card's next task, with what the first one got
        again = tasks._KeptAnswersService(repos, KeptAnswers(answers.entries))
        assert run(ask(again, image)) == OpenAIText(text="from Vision")
        assert fake.calls == ["Vision"]  # answered from the kept answers
        assert again.answers.replayed == 1
        assert again.runtime.answered_by("OpenAIText") == ("Vision", "m")
        (tally,) = again.runtime.usage
        assert (tally.provider, tally.requests, tally.prompt_tokens) == ("Vision", 1, 12)

        # another picture, or another message, is another request
        other = tmp_path / "other.jpg"
        other.write_bytes(b"\xff\xd8\xff another card")
        run(ask(again, other))
        run(ask(again, image, message="something else"))
        assert fake.calls == ["Vision", "Vision", "Vision"]


def test_a_parse_with_ai_and_a_rebuild_are_kept_as_they_were(results_folder: Path):
    job_id = uuid4()
    line = CardDraftIngredient(original_text="200 g Mehl", quantity=200, note="")
    parsed = ParseLinesResult(
        ingredients=[line],
        sent={str(line.reference_id): "200 g Mehl"},
        units=["g", "gram"],
        linked={uuid4(): ["Mehl", "Weizenmehl"]},  # what the flags of the parsed lines are judged with
    )
    payload = {"mode": "parse_lines", "lines": [{"ref": str(line.reference_id), "text": "200 g Mehl"}]}
    assert results.keep(_key(job_id, payload=payload), result=parsed)
    kept = results.take(_key(job_id, payload=payload))
    assert kept is not None and kept.result == parsed

    rebuilt = extract_result("Banana Bread")
    rebuilt.origin = CardProposalOrigin.rebuild
    other = uuid4()
    results.keep(_key(other, payload={"mode": "rebuild", "transcription": "Banana Bread"}), result=rebuilt)
    kept = results.take(_key(other, payload={"mode": "rebuild", "transcription": "Banana Bread"}))
    assert kept is not None and isinstance(kept.result, ExtractResult)
    assert kept.result.origin == CardProposalOrigin.rebuild
