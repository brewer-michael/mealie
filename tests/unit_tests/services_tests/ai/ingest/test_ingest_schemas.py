"""
The recipe card schemas this round changed (docs/ai/PHASE2.md §13): draft migrations (notes gained ids in version 2),
round trips of the stored JSON, and the request limits.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from mealie.schema.recipe_ingest import (
    BulkCommitRequest,
    CardDraft,
    CardDraftNote,
    CardProposal,
    CardProposalKind,
    CardProposalOrigin,
    EvalCaseRequest,
    EvalCaseTag,
    EvalCaseUpdate,
    PageMeta,
    ParseLinesRequest,
    RebuildRequest,
)
from mealie.schema.recipe_ingest.ingest_draft import CARD_DRAFT_SCHEMA_VERSION, note_id_for


def _stored(draft: CardDraft) -> Any:
    """As the `draft` column stores it"""
    return to_jsonable_python(draft, by_alias=False)


V1_DRAFT = {
    "schema_version": 1,
    "name": "Banana Mug Cake",
    "notes": [
        {"title": "", "text": "Grandma's favourite"},
        {"title": "Tip", "text": "Use a big mug"},
        {"title": "", "text": "Grandma's favourite"},
    ],
}


def test_a_version_1_draft_gets_the_same_note_ids_on_every_read():
    first, second = CardDraft.model_validate(V1_DRAFT), CardDraft.model_validate(dict(V1_DRAFT))

    assert first.schema_version == CARD_DRAFT_SCHEMA_VERSION == 2
    assert [note.id for note in first.notes] == [note.id for note in second.notes]
    # the same text at another position is another note
    assert len({note.id for note in first.notes}) == 3
    assert all(note.id.version == 4 for note in first.notes)
    assert first.notes[1].id == note_id_for(1, {"title": "Tip", "text": "Use a big mug"})


def test_a_camel_case_draft_from_the_page_is_migrated_too():
    draft = CardDraft.model_validate({"schemaVersion": 1, "name": "X", "notes": [{"title": "", "text": "a"}]})
    assert draft.schema_version == 2
    assert draft.notes[0].id == note_id_for(0, {"title": "", "text": "a"})


def test_stored_note_ids_are_kept_and_a_new_note_without_one_gets_one():
    kept = uuid4()
    draft = CardDraft.model_validate({"schema_version": 2, "notes": [{"id": str(kept), "text": "a"}, {"text": "b"}]})
    assert draft.notes[0].id == kept
    assert draft.notes[1].id == note_id_for(1, {"text": "b"})


def test_a_draft_round_trips_through_its_stored_form():
    draft = CardDraft.model_validate(V1_DRAFT).model_copy(update={"attach_card_photo": False})
    again = CardDraft.model_validate(_stored(draft))
    assert again == draft
    assert _stored(again) == _stored(draft)
    assert _stored(again)["schema_version"] == 2
    assert again.attach_card_photo is False
    assert CardDraft().attach_card_photo is None


def test_a_draft_from_a_newer_version_is_read_as_it_is():
    draft = CardDraft.model_validate({"schema_version": 7, "name": "X", "notes": [{"text": "a"}], "future": 1})
    assert draft.schema_version == 7
    assert draft.notes[0].id == note_id_for(0, {"text": "a"})


def test_notes_made_in_code_get_fresh_ids():
    assert CardDraftNote(text="a").id != CardDraftNote(text="a").id


def test_a_proposal_says_where_it_came_from():
    proposal = CardProposal(kind=CardProposalKind.full, draft=CardDraft(name="X"))
    assert proposal.origin == CardProposalOrigin.reextract
    stored = to_jsonable_python(proposal.model_copy(update={"origin": CardProposalOrigin.rebuild}), by_alias=False)
    assert CardProposal.model_validate(stored).origin == CardProposalOrigin.rebuild
    # proposals stored before this field existed read as re-extracts
    stored.pop("origin")
    assert CardProposal.model_validate(stored).origin == CardProposalOrigin.reextract


def test_page_meta_keeps_ocr_lines_and_reads_old_pages():
    meta: dict[str, Any] = {
        "index": 0,
        "width": 10,
        "height": 20,
        "view_width": 10,
        "view_height": 20,
        "raw_sha256": "a" * 64,
        "page_sha256": "b" * 64,
        "format": "jpeg",
        "raw_bytes": 1,
        "rotation_source": "model",
        "ocr": {"text": "Bake at 350", "confidence": 91.0},
    }
    page = PageMeta.model_validate(meta)
    assert page.ocr is not None and page.ocr.lines == []
    meta["ocr"]["lines"] = [{"text": "Bake at 350", "x": 0.1, "y": 0.5, "width": 0.6, "height": 0.04}]
    page = PageMeta.model_validate(meta)
    assert page.ocr is not None and page.ocr.lines[0].text == "Bake at 350"
    assert PageMeta.model_validate(to_jsonable_python(page, by_alias=False)) == page


def test_request_limits():
    with pytest.raises(ValidationError):
        RebuildRequest(transcription="")
    with pytest.raises(ValidationError):
        RebuildRequest(transcription="x" * 20_001)
    assert RebuildRequest(transcription="x" * 20_000)

    with pytest.raises(ValidationError):
        ParseLinesRequest(refs=[])
    with pytest.raises(ValidationError):
        ParseLinesRequest(refs=[uuid4() for _ in range(51)])
    ref = uuid4()
    assert ParseLinesRequest(refs=[ref, ref]).refs == [ref]

    jobs = [uuid4(), uuid4()]
    request = BulkCommitRequest.model_validate(
        {"jobIds": [str(jobs[0]), str(jobs[1]), str(jobs[0])], "draftVersions": {str(j): 3 for j in jobs}}
    )
    assert request.job_ids == jobs
    assert request.draft_versions[jobs[1]] == 3
    with pytest.raises(ValidationError):
        BulkCommitRequest(job_ids=jobs, draft_versions={jobs[0]: 1})
    with pytest.raises(ValidationError):
        BulkCommitRequest(job_ids=[], draft_versions={})
    many = [uuid4() for _ in range(101)]
    with pytest.raises(ValidationError):
        BulkCommitRequest(job_ids=many, draft_versions=dict.fromkeys(many, 1))


def test_eval_case_tags_and_notes():
    request = EvalCaseRequest.model_validate({"slug": "card", "tags": ["faded", "printed", "faded"], "notes": "n"})
    assert request.tags == [EvalCaseTag.faded, EvalCaseTag.printed]
    with pytest.raises(ValidationError):
        EvalCaseRequest.model_validate({"slug": "card", "tags": ["sideways"]})  # found from the card, not chosen
    with pytest.raises(ValidationError):
        EvalCaseRequest.model_validate({"slug": "card", "notes": "x" * 2001})

    update = EvalCaseUpdate.model_validate({"verified": True})
    assert update.tags is None and update.notes is None
    with pytest.raises(ValidationError):
        EvalCaseUpdate.model_validate({"slug": "renamed"})


def test_uuid_keys_of_draft_versions_are_parsed():
    job = uuid4()
    request = BulkCommitRequest.model_validate({"jobIds": [str(job)], "draftVersions": {str(job): 1}})
    assert isinstance(next(iter(request.draft_versions)), UUID)
