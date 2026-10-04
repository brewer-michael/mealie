"""Saving a reviewed card as an eval case (docs/ai/PHASE2.md §11.6), and the fixture format it writes"""

import io
import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from PIL import Image, ImageCms
from sqlalchemy.orm import Session

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftRef,
    CardDraftStep,
    ExtractionMeta,
    IngestSource,
    IngestStatus,
    PageRotationSource,
)
from mealie.services.ai.ingest import eval_export, images, storage
from mealie.services.ai.ingest.eval_export import (
    CardFixture,
    EvalCaseExists,
    EvalCaseFilesMissing,
    EvalCaseUnavailable,
    ExpectedIngredient,
    build_eval_case,
    delete_eval_case,
    expected_from_draft,
    list_eval_cases,
    restore_blanks,
    save_eval_case,
)
from tests.utils.fixture_schemas import TestUser

RED = (255, 0, 0)
WHITE = (255, 255, 255)
GPS_IFD = 0x8825

BANANA_STEP = "Microwave in bowl or large mug for [blank] minutes or until firm in center."
TRANSCRIPTION = "\n".join(
    [
        "Banana Mug Cake (Sugar & Gluten Free)",
        "- 1 banana",
        "- 1 T. coconut oil (melted)",
        "- 1/4 t. salt",
        "Mash banana and mix ingredients thoroughly.",
        BANANA_STEP,
    ]
)


@pytest.fixture()
def db() -> Iterator[Session]:
    with session_context() as session:
        yield session


def _srgb_profile() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _card_photo() -> bytes:
    """An upright card: white with its top third red, carrying GPS and an ICC profile, as a phone would send it"""
    image = Image.new("RGB", (300, 400), WHITE)
    image.paste(RED, (0, 0, 300, 133))
    exif = Image.Exif()
    exif[GPS_IFD] = {1: "N", 2: (51.0, 30.0, 0.0)}
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95, exif=exif, icc_profile=_srgb_profile())
    return buffer.getvalue()


def _draft() -> CardDraft:
    return CardDraft(
        name="Banana Mug Cake",
        description="A sugar free, gluten free mug cake.",
        attribution="From Grandma Jo",
        ingredients=[
            CardDraftIngredient(
                original_text="1 T. coconut oil (melted)",
                quantity=1,
                unit=CardDraftRef(id=uuid4(), name="tablespoon"),
                food=CardDraftRef(id=uuid4(), name="coconut oil"),
                note="melted",
            ),
            # the reviewer corrected a misread quantity: the card says 1/4, the model read 1/2
            CardDraftIngredient(
                original_text="1/2 t. salt",
                quantity=0.25,
                unit=CardDraftRef(name="teaspoon"),
                food=CardDraftRef(name="salt"),
            ),
            CardDraftIngredient(original_text="Cinnamon to taste", food=CardDraftRef(name="cinnamon"), note="to taste"),
        ],
        steps=[
            CardDraftStep(text="Mash banana and mix ingredients thoroughly."),
            # the reviewer filled in the blank
            CardDraftStep(text="Microwave in bowl or large mug for 2 minutes or until firm in center."),
        ],
        perform_time="[blank]",
    )


def _seed_job(
    db: Session,
    user: TestUser,
    *,
    status: IngestStatus = IngestStatus.ready,
    rotation: int = 90,
    draft: CardDraft | None = None,
    local_only: bool = False,
) -> tuple[UUID, bytes]:
    """
    A reviewed job whose one page was read sideways and turned `rotation` degrees by orientation. Returns its id and
    the page as intake wrote it, before the turn.
    """
    repos = IngestRepos(db, UUID(user.group_id), UUID(user.household_id))
    batch_id = repos.batches.create(source=IngestSource.app, created_by=None)
    job_id = uuid4()
    page_dir = storage.create_job_dir(UUID(user.group_id), job_id, 1) / "pages" / "0"
    meta = images.normalize_page(io.BytesIO(_card_photo()), page_dir, 0, original_filename="IMG_2503.jpg")
    original = (page_dir / images.PAGE_FILE).read_bytes()
    if rotation:
        meta = images.rotate_page_files(page_dir, meta, rotation, PageRotationSource.ocr)

    repos.jobs.create(
        {
            "id": job_id,
            "batch_id": batch_id,
            "position": 0,
            "source": IngestSource.app.value,
            "status": status.value,
            "local_only": local_only,
            "pages": [meta],
            "source_sha256": uuid4().hex * 2,
            "draft": draft or _draft(),
            "transcription": TRANSCRIPTION,
            "extraction": ExtractionMeta(provider="Claude Sonnet", model="claude-sonnet-5-5"),
        }
    )
    return job_id, original


@pytest.fixture()
def cleanup(unique_user: TestUser) -> Iterator[list[UUID]]:
    jobs: list[UUID] = []
    yield jobs
    group_id = UUID(unique_user.group_id)
    for job_id in jobs:
        storage.remove_job_dir(group_id, job_id)
    shutil.rmtree(storage.eval_cards_dir(group_id), ignore_errors=True)


def _job(db: Session, user: TestUser, job_id: UUID) -> RecipeIngestionJob:
    job = IngestRepos(db, UUID(user.group_id), UUID(user.household_id)).jobs.get(job_id)
    assert job is not None
    return job


def _top_is_red(data: bytes) -> bool:
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        top = image.getpixel((image.width // 2, image.height // 10))
        bottom = image.getpixel((image.width // 2, image.height * 9 // 10))
    assert isinstance(top, tuple) and isinstance(bottom, tuple)
    return top[0] > 200 and top[1] < 80 and bottom[1] > 200


# ==========================================
# The format


def test_v1_fixtures_still_load():
    fixture = CardFixture.model_validate(
        {
            "source": "card.jpg",
            "verified_by_owner": True,
            "expected": {"name": "Pancakes", "ingredients": ["2 C. flour"], "must_not_invent": ["Cook_Time"]},
        }
    )

    assert fixture.schema_version == 1
    assert fixture.sources == ["card.jpg"]
    assert fixture.expected.ingredient_lines == ["2 C. flour"]
    assert fixture.expected.structured_ingredients == [None]
    assert fixture.expected.must_not_invent == ["cook time"]


def test_v2_fixtures_mix_plain_and_structured_lines():
    fixture = CardFixture.model_validate(
        {
            "schema_version": 2,
            "source": ["front.jpg", "back.jpg"],
            "tags": ["handwritten", "two-sided", "handwritten"],
            "local_only": True,
            "expected": {
                "name": "Pancakes",
                "ingredients": ["2 eggs", {"text": "2 C. flour", "quantity": 2, "unit": "cup", "food": "flour"}],
                "attribution": "From Grandma Jo",
                "blanks": [{"field": "steps", "text": "Bake for [blank] minutes."}, {"field": "perform_time"}],
                "recipe_yield": "12 pancakes",
                "times": {"prep_time": "10 min"},
                "must_not_invent": ["attribution"],
            },
        }
    )

    assert fixture.tags == ["handwritten", "two-sided"]
    assert fixture.expected.ingredient_lines == ["2 eggs", "2 C. flour"]
    assert fixture.expected.structured_ingredients[1] == ExpectedIngredient(
        text="2 C. flour", quantity=2, unit="cup", food="flour"
    )


@pytest.mark.parametrize(
    "change, message",
    [
        ({"attributon": "Grandma"}, "attributon"),  # a typo isn't silently dropped
        ({"expected": {"name": "x", "ingredients": [], "instructions": [], "extra": 1}}, "extra"),
        ({"schema_version": 3}, "schema_version"),
        ({"tags": ["blurry"]}, "unknown tag"),
        ({"expected": {"name": "x", "ingredients": [], "blanks": [{"field": "steps"}]}}, "needs the item's text"),
        ({"expected": {"name": "x", "ingredients": [], "blanks": [{"field": "oven"}]}}, "unknown blank field"),
        ({"expected": {"name": "x", "ingredients": [{"text": "1 egg", "brand": "x"}]}}, "brand"),
    ],
)
def test_fixture_mistakes_are_errors(change: dict, message: str):
    data = {"source": "card.jpg", "expected": {"name": "x", "ingredients": ["1 egg"]}, **change}

    with pytest.raises(ValueError, match=message):
        CardFixture.model_validate(data)


# ==========================================
# Blanks and reviewed lines


@pytest.mark.parametrize(
    "card, reviewed, expected",
    [
        # the reviewer filled the blank with a number: the marker goes back, the reviewer's other edits stay
        (BANANA_STEP, "Microwave in a bowl or large mug for 2 minutes, or until firm.", None),
        ("for [blank] minutes", "for 2 1/2 minutes", "for [blank] minutes"),
        # a physical line of a longer step
        ("or large mug for [blank]", "Microwave in bowl or large mug for 3 minutes.", None),
        # words in the gap
        ("Bake for [blank] minutes", "Bake for a few minutes", "Bake for [blank] minutes"),
        # still there
        (BANANA_STEP, BANANA_STEP, BANANA_STEP),
        # no blank on the card
        ("Bake for 10 minutes", "Bake for 12 minutes", "Bake for 12 minutes"),
    ],
)
def test_restore_blanks(card: str, reviewed: str, expected: str | None):
    restored = restore_blanks(card, reviewed)

    if expected is not None:
        assert restored == expected
    else:
        assert "[blank]" in restored
        assert not any(char.isdigit() for char in restored)
        assert restored.startswith("Microwave in")


def test_expected_values_come_from_the_reviewed_draft_with_blanks_kept():
    expected = expected_from_draft(_draft(), TRANSCRIPTION)

    assert expected.name == "Banana Mug Cake"
    assert expected.attribution == "From Grandma Jo"
    assert expected.ingredients == [
        # the card's own wording, since it agrees with the reviewed amounts
        ExpectedIngredient(
            text="1 T. coconut oil (melted)", quantity=1, unit="tablespoon", food="coconut oil", note="melted"
        ),
        # the misread line is written from the corrected fields
        ExpectedIngredient(text="1/4 teaspoon salt", quantity=0.25, unit="teaspoon", food="salt"),
        ExpectedIngredient(text="Cinnamon to taste", food="cinnamon", note="to taste"),
    ]
    assert expected.instructions == ["Mash banana and mix ingredients thoroughly.", BANANA_STEP]
    assert [(blank.field, blank.text) for blank in expected.blanks] == [
        ("steps", BANANA_STEP),
        ("perform_time", None),
    ]
    # a blank time has no value
    assert expected.times is None
    assert expected.description_contains == []


def test_an_unrelated_item_never_takes_a_cards_blank():
    draft = CardDraft(name="Toast", steps=[CardDraftStep(text="Toast 2 slices.")])

    expected = expected_from_draft(draft, "Microwave in bowl or large mug for [blank] minutes or until firm in center.")

    assert expected.instructions == ["Toast 2 slices."]
    assert expected.blanks == []


# ==========================================
# Building and saving a case


def test_build_eval_case_turns_the_pages_back_and_strips_them(db: Session, unique_user: TestUser, cleanup: list[UUID]):
    job_id, original = _seed_job(db, unique_user, rotation=90, local_only=True)
    cleanup.append(job_id)
    job = _job(db, unique_user, job_id)
    stored = (storage.page_dir(job.group_id, job.id, 0) / images.PAGE_FILE).read_bytes()
    assert not _top_is_red(stored)  # orientation turned it

    case = build_eval_case(job, "banana-mug-cake", True, now=datetime(2026, 10, 3, tzinfo=UTC))

    [(name, data)] = case.images
    assert name == "banana-mug-cake-1.jpg"
    with Image.open(io.BytesIO(data)) as image, Image.open(io.BytesIO(original)) as before:
        # turned back to how the page came in, so the eval exercises orientation again
        assert image.size == before.size
        assert not image.getexif()
        assert "exif" not in image.info
        assert image.info.get("icc_profile") == _srgb_profile()
    assert _top_is_red(data)

    fixture = case.fixture
    assert fixture.schema_version == eval_export.FIXTURE_SCHEMA_VERSION
    assert fixture.source == "banana-mug-cake-1.jpg"
    assert fixture.verified_by_owner is True
    assert fixture.local_only is True
    assert fixture.tags == ["sideways", "blank"]
    assert fixture.origin is not None
    assert fixture.origin.job_id == str(job_id)
    assert fixture.origin.drafted_by is not None
    assert (fixture.origin.drafted_by.provider, fixture.origin.drafted_by.model) == (
        "Claude Sonnet",
        "claude-sonnet-5-5",
    )
    assert fixture.origin.exported_at == datetime(2026, 10, 3, tzinfo=UTC)
    assert fixture.expected.instructions[1] == BANANA_STEP

    # what's written validates as a fixture the eval reads
    assert CardFixture.model_validate_json(eval_export.fixture_json(fixture)) == fixture


def test_an_unturned_page_is_copied_as_intake_wrote_it(db: Session, unique_user: TestUser, cleanup: list[UUID]):
    job_id, original = _seed_job(db, unique_user, rotation=0, status=IngestStatus.committed)
    cleanup.append(job_id)

    case = build_eval_case(_job(db, unique_user, job_id), "upright", False)

    assert case.images[0][1] == original
    assert "sideways" not in case.fixture.tags


def test_only_cards_with_a_draft_and_files_can_be_saved(db: Session, unique_user: TestUser, cleanup: list[UUID]):
    job_id, _ = _seed_job(db, unique_user, status=IngestStatus.processing)
    cleanup.append(job_id)
    with pytest.raises(EvalCaseUnavailable):
        build_eval_case(_job(db, unique_user, job_id), "processing", False)

    job_id, _ = _seed_job(db, unique_user)
    cleanup.append(job_id)
    storage.remove_job_dir(UUID(unique_user.group_id), job_id)
    with pytest.raises(EvalCaseFilesMissing):
        build_eval_case(_job(db, unique_user, job_id), "purged", False)


def test_save_list_and_delete(db: Session, unique_user: TestUser, cleanup: list[UUID]):
    group_id = UUID(unique_user.group_id)
    job_id, _ = _seed_job(db, unique_user)
    cleanup.append(job_id)
    job = _job(db, unique_user, job_id)

    out = save_eval_case(group_id, build_eval_case(job, "banana", True))

    assert out.files == ["banana.json", "banana-1.jpg"]
    directory = storage.eval_cards_dir(group_id)
    assert sorted(path.name for path in directory.iterdir()) == ["banana-1.jpg", "banana.json"]
    assert json.loads((directory / "banana.json").read_text())["schema_version"] == 2

    # never overwritten, and nothing is left behind by the refusal
    with pytest.raises(EvalCaseExists):
        save_eval_case(group_id, build_eval_case(job, "banana", False))
    assert sorted(path.name for path in directory.iterdir()) == ["banana-1.jpg", "banana.json"]

    # a case added by hand whose image another case's name would take
    (directory / "banana-1.json").write_text(
        json.dumps({"source": "banana-1-photo.jpg", "expected": {"name": "x", "ingredients": []}})
    )
    (directory / "banana-1-photo.jpg").write_bytes(b"\xff\xd8\xff")
    (directory / "broken.json").write_text("{")

    [banana, banana_1, broken] = list_eval_cases(group_id)
    assert (banana.slug, banana.name, banana.page_count, banana.verified) == ("banana", "Banana Mug Cake", 1, True)
    assert banana.created_at is not None
    assert (banana_1.slug, banana_1.page_count) == ("banana-1", 1)
    assert (broken.slug, broken.name) == ("broken", None)

    assert delete_eval_case(group_id, "banana")
    assert sorted(path.name for path in directory.iterdir()) == ["banana-1-photo.jpg", "banana-1.json", "broken.json"]
    assert not delete_eval_case(group_id, "banana")
    assert not delete_eval_case(group_id, "../banana")
    assert delete_eval_case(group_id, "broken")


def test_a_clashing_image_refuses_the_whole_case(db: Session, unique_user: TestUser, cleanup: list[UUID]):
    group_id = UUID(unique_user.group_id)
    job_id, _ = _seed_job(db, unique_user)
    cleanup.append(job_id)
    directory = storage.eval_cards_dir(group_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pie-1.jpg").write_bytes(b"someone else's")

    with pytest.raises(EvalCaseExists):
        save_eval_case(group_id, build_eval_case(_job(db, unique_user, job_id), "pie", False))

    assert (directory / "pie-1.jpg").read_bytes() == b"someone else's"
    assert not (directory / "pie.json").exists()


def test_mealie_commit_prefers_the_build_commit(monkeypatch: pytest.MonkeyPatch):
    class Settings:
        GIT_COMMIT_HASH = "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(eval_export, "get_app_settings", lambda: Settings())
    eval_export.mealie_commit.cache_clear()
    try:
        assert eval_export.mealie_commit() == Settings.GIT_COMMIT_HASH

        # without one (a development checkout), the checkout's HEAD if git is there
        Settings.GIT_COMMIT_HASH = "unknown"
        eval_export.mealie_commit.cache_clear()
        commit = eval_export.mealie_commit()
        assert commit is None or (len(commit) == 40 and commit != "unknown")
    finally:
        eval_export.mealie_commit.cache_clear()
