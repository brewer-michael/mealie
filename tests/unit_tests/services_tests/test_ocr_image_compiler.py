from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.services import ocr
from mealie.services.recipe.import_workflow.compilers import (
    COMPILE_SOURCE_PROMPT,
    DEFAULT_SOURCE_COMPILERS,
    ImageCompiler,
    OCRImageCompiler,
)
from mealie.services.recipe.import_workflow.context import WorkflowContext, WorkflowInput, WorkflowOptions
from mealie.services.recipe.import_workflow.steps.compile_source import CompileSourceStep

OCR_TEXT = "Banana Mug Cake\n1 banana\n1 T. coconut oil"


def compiled_source() -> OpenAICompiledSource:
    return OpenAICompiledSource(contains_recipe=True, content="# Banana Mug Cake", language=None, image_url=None)


def make_ctx(*, image_provider: bool = False, default_provider: bool = True, images: int = 1) -> WorkflowContext:
    ai = MagicMock()
    ai.image_provider = MagicMock() if image_provider else None
    ai.default_provider = MagicMock() if default_provider else None
    ai.get_prompt.side_effect = lambda name: f"prompt:{name}"
    ai.get_response = AsyncMock(return_value=compiled_source())

    translator = MagicMock()
    translator.t.side_effect = lambda key: key

    return WorkflowContext(
        input=WorkflowInput(images=[Path(f"card-{i}.jpg") for i in range(images)]),
        options=WorkflowOptions(),
        repos=MagicMock(),
        translator=translator,
        ai=ai,
        on_progress=AsyncMock(),
    )


@pytest.fixture()
def ocr_texts(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stands in for Tesseract: maps an image's filename to the text read from it."""

    texts: dict[str, str] = {}

    def mock_extract_text(path: Path) -> ocr.OCRResult:
        text = texts.get(path.name, "")
        return ocr.OCRResult(text=text, confidence=80 if text else 0, rotation=0)

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", mock_extract_text)
    return texts


def test_ocr_is_tried_right_after_the_image_provider():
    index = DEFAULT_SOURCE_COMPILERS.index(ImageCompiler)
    assert DEFAULT_SOURCE_COMPILERS[index + 1] is OCRImageCompiler


@pytest.mark.asyncio
async def test_images_are_read_with_ocr_when_there_is_no_image_provider(ocr_texts: dict[str, str]):
    ocr_texts["card-0.jpg"] = OCR_TEXT
    ctx = make_ctx(image_provider=False)

    assert not ImageCompiler(ctx).can_compile()

    compiled = await CompileSourceStep()._compile_images(ctx)

    assert compiled == compiled_source()
    ctx.ai.get_response.assert_awaited_once()
    prompt, message = ctx.ai.get_response.await_args.args
    assert prompt == f"prompt:{COMPILE_SOURCE_PROMPT}"
    assert OCR_TEXT in message
    assert ctx.ai.get_response.await_args.kwargs == {"response_schema": OpenAICompiledSource}

    assert ctx.on_progress is not None
    ctx.on_progress.assert_awaited_once_with("recipe.create-progress.reading-images-with-ocr")


@pytest.mark.asyncio
async def test_images_are_read_with_ocr_when_the_image_provider_fails(ocr_texts: dict[str, str]):
    ocr_texts["card-0.jpg"] = OCR_TEXT
    ctx = make_ctx(image_provider=True)
    ctx.ai.get_response.side_effect = [RuntimeError("the image provider is down"), compiled_source()]

    compiled = await CompileSourceStep()._compile_images(ctx)

    assert compiled == compiled_source()
    image_call, ocr_call = ctx.ai.get_response.await_args_list
    assert image_call.kwargs["attachments"]
    assert "attachments" not in ocr_call.kwargs
    assert OCR_TEXT in ocr_call.args[1]


@pytest.mark.asyncio
async def test_the_image_provider_is_preferred_over_ocr(ocr_texts: dict[str, str]):
    ocr_texts["card-0.jpg"] = OCR_TEXT
    ctx = make_ctx(image_provider=True)

    await CompileSourceStep()._compile_images(ctx)

    ctx.ai.get_response.assert_awaited_once()
    assert ctx.ai.get_response.await_args.kwargs["attachments"]


@pytest.mark.asyncio
async def test_nothing_is_sent_when_ocr_reads_no_text(ocr_texts: dict[str, str]):
    ctx = make_ctx(image_provider=False)

    assert await OCRImageCompiler(ctx).compile() is None
    assert await CompileSourceStep()._compile_images(ctx) is None
    ctx.ai.get_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_readable_image_is_sent_in_one_message(ocr_texts: dict[str, str]):
    ocr_texts["card-0.jpg"] = "Banana Mug Cake"
    ocr_texts["card-2.jpg"] = "Microwave until firm"
    ctx = make_ctx(images=3)

    await OCRImageCompiler(ctx).compile()

    message = ctx.ai.get_response.await_args.args[1]
    assert "Text from photo 1 of 3:" in message
    assert "Banana Mug Cake" in message
    assert "photo 2 of 3" not in message
    assert "Text from photo 3 of 3:" in message
    assert "Microwave until firm" in message


@pytest.mark.asyncio
async def test_the_message_says_the_text_is_ocr_output(ocr_texts: dict[str, str]):
    ocr_texts["card-0.jpg"] = OCR_TEXT
    ctx = make_ctx()

    await OCRImageCompiler(ctx).compile()

    message = ctx.ai.get_response.await_args.args[1]
    assert "OCR" in message
    assert "handwritten" in message
    assert "Never invent quantities, times, or steps" in message


def test_ocr_needs_a_default_provider(ocr_texts: dict[str, str]):
    assert OCRImageCompiler(make_ctx(default_provider=True)).can_compile()
    assert not OCRImageCompiler(make_ctx(default_provider=False)).can_compile()


def test_ocr_needs_images(ocr_texts: dict[str, str]):
    assert not OCRImageCompiler(make_ctx(images=0)).can_compile()


def test_ocr_is_skipped_when_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    assert not OCRImageCompiler(make_ctx()).can_compile()
