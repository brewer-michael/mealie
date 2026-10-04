"""Re-reading one region of a card (docs/ai/PHASE2.md §4.7): only the crop is sent, or Tesseract reads it"""

import io
from pathlib import Path

import pytest
from PIL import Image

from mealie.core import exceptions
from mealie.schema.recipe_ingest import CardProposalKind, ProposalTarget
from mealie.services import ocr
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.images import Region
from mealie.services.ai.ingest.pipeline import reread as reread_module
from mealie.services.ai.ingest.pipeline import reread_region
from mealie.services.ai.ingest.pipeline.service import JobOpenAIService
from mealie.services.ai.policy import ai_call_policy
from mealie.services.openai import OpenAINotEnabledException
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    FakeCardAI,
    banana_answers,
    card_image,
    configure,
    create_provider,
    job_session,
    make_pages,
    rate_limited,
)
from tests.utils.fixture_schemas import TestUser

REGION = Region(0.1, 0.2, 0.3, 0.05)
TARGET = ProposalTarget(field="ingredients", ref="line-1")


@pytest.fixture()
def crops(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Every crop `reread_region` builds"""
    built: list[bytes] = []
    real = reread_module.images.crop_region

    def crop_region(path: Path, region: Region, **kwargs) -> bytes:
        crop = real(path, region, **kwargs)
        built.append(crop)
        return crop

    monkeypatch.setattr(reread_module.images, "crop_region", crop_region)
    return built


def _files(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()}


@pytest.mark.asyncio
async def test_only_the_crop_goes_to_the_image_slot(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, crops: list[bytes]
):
    user = unique_user_fn_scoped
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))
    answer = {
        "readable": True,
        "text": "1/3 C. almond flour",
        "alternatives": ["1/2 C. almond flour", "1/3 C. almond flour"],
    }
    fake = FakeCardAI(banana_answers(OpenAIRecipeCardRegion=answer)).install(monkeypatch)
    (page,) = make_pages(tmp_path, data=card_image(size=(1200, 1600)))
    files = _files(tmp_path)

    with job_session(user) as (_, repos):
        proposal = await reread_region(page, REGION, TARGET, "1/3 C. almond fluor", ai=JobOpenAIService(repos))

    assert (proposal.kind, proposal.target, proposal.text, proposal.readable, proposal.via_ocr) == (
        CardProposalKind.region,
        TARGET,
        "1/3 C. almond flour",
        True,
        False,
    )
    assert proposal.alternatives == ["1/2 C. almond flour"]

    (call,) = fake.calls
    assert (call.provider, call.schema, call.images) == ("Vision", "OpenAIRecipeCardRegion", 1)
    assert '"""\n1/3 C. almond fluor\n"""' in call.message
    assert "one ingredient line" in call.message

    # a small region is cropped from page.jpg with a margin and upscaled; nothing is written
    (crop,) = crops
    with Image.open(io.BytesIO(crop)) as image:
        assert max(image.size) >= limits.REREAD_MIN_SIDE
    assert _files(tmp_path) == files


@pytest.mark.asyncio
async def test_without_an_image_provider_tesseract_reads_the_crop(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    configure(user, default=create_provider(user, "Text"))
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    (page,) = make_pages(tmp_path, data=card_image(size=(1200, 1600)))
    read: list[Path] = []

    def extract_text(path: Path, *, min_ratio: float = 1.0) -> ocr.OCRResult:
        read.append(path)
        with Image.open(path) as image:
            assert max(image.size) >= limits.REREAD_MIN_SIDE
        return ocr.OCRResult(text="1/3 C.\nalmond flour", confidence=70.0)

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", extract_text)

    with job_session(user) as (_, repos):
        proposal = await reread_region(page, REGION, TARGET, None, ai=JobOpenAIService(repos))

    assert (proposal.text, proposal.readable, proposal.via_ocr) == ("1/3 C. almond flour", True, True)
    assert fake.calls == []
    assert not read[0].is_relative_to(tmp_path)  # the crop never goes near the job's files
    assert not read[0].exists()


@pytest.mark.asyncio
async def test_a_local_only_card_is_reread_by_tesseract_rather_than_a_cloud_provider(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    configure(user, image=create_provider(user, "Cloud vision"), default=create_provider(user, "Cloud text"))
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    (page,) = make_pages(tmp_path)
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", lambda path, *, min_ratio=1.0: ocr.OCRResult(text="", confidence=0.0))

    with job_session(user) as (_, repos), ai_call_policy(local_only=True):
        proposal = await reread_region(page, REGION, TARGET, None, ai=JobOpenAIService(repos))

    assert (proposal.text, proposal.readable, proposal.via_ocr) == ("", False, True)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_rate_limited_reread_waits_rather_than_falling_back(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))
    FakeCardAI(banana_answers(), failures={"Vision": rate_limited()}).install(monkeypatch)
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    (page,) = make_pages(tmp_path)

    with job_session(user) as (_, repos), pytest.raises(exceptions.RateLimitError):
        await reread_region(page, REGION, TARGET, None, ai=JobOpenAIService(repos))


@pytest.mark.asyncio
async def test_unreadable_and_unavailable(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))
    answers = banana_answers(OpenAIRecipeCardRegion={"readable": False, "text": "", "alternatives": []})
    FakeCardAI(answers).install(monkeypatch)
    (page,) = make_pages(tmp_path)

    with job_session(user) as (_, repos):
        proposal = await reread_region(page, REGION, ProposalTarget(field="name"), "Banana", ai=JobOpenAIService(repos))
    assert (proposal.readable, proposal.text) == (False, "")

    configure(user, default=create_provider(user, "Only text"))
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    with job_session(user) as (_, repos), pytest.raises(OpenAINotEnabledException):
        await reread_region(page, REGION, ProposalTarget(field="name"), "Banana", ai=JobOpenAIService(repos))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "read", "alternatives", "text", "kept"),
    [
        # a step's own list number isn't part of it (it would be held against the card as a number)
        ("steps", "4. Cool on a rack.", ["4) Cool on the rack."], "Cool on a rack.", ["Cool on the rack."]),
        ("steps", "Bake 20 minutes.", [], "Bake 20 minutes.", []),
        # the attribution field is labelled "From"
        ("attribution", "From Grandma Jo", ["From: Grandma Joe"], "Grandma Jo", ["Grandma Joe"]),
        ("attribution", "Aunt May's", [], "Aunt May's", []),
        # an ingredient's amount stays
        ("ingredients", "2. c. sugar", [], "2. c. sugar", []),
    ],
)
async def test_a_reading_is_kept_as_its_field_holds_it(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    read: str,
    alternatives: list[str],
    text: str,
    kept: list[str],
):
    user = unique_user_fn_scoped
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))
    answer = {"readable": True, "text": read, "alternatives": alternatives}
    FakeCardAI(banana_answers(OpenAIRecipeCardRegion=answer)).install(monkeypatch)
    (page,) = make_pages(tmp_path)
    target = ProposalTarget(field=field, ref="ref-1" if field in ("steps", "ingredients") else None)

    with job_session(user) as (_, repos):
        proposal = await reread_region(page, REGION, target, None, ai=JobOpenAIService(repos))

    assert (proposal.text, proposal.alternatives) == (text, kept)
