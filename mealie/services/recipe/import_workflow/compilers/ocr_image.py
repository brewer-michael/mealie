import asyncio

from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.services import ocr
from mealie.services.openai.content import truncate_source_content

from .base import COMPILE_SOURCE_PROMPT, SourceCompiler, SourceType


class OCRImageCompiler(SourceCompiler):
    """
    Reads uploaded images with on-device OCR, then has the default provider make sense of the text.
    A fallback for groups without an image provider, or for when it fails: OCR loses the layout and
    stumbles over handwriting, so it's never preferred over a provider that can see the image.
    """

    source_type = SourceType.IMAGES
    progress_key = "recipe.create-progress.reading-images-with-ocr"

    def can_compile(self) -> bool:
        return bool(self.ctx.input.images) and ocr.is_available() and self.ctx.ai.default_provider is not None

    async def compile(self) -> OpenAICompiledSource | None:
        images = self.ctx.input.images

        texts: list[tuple[int, str]] = []
        for index, image in enumerate(images, start=1):
            result = await asyncio.to_thread(ocr.extract_text, image)
            if text := result.text.strip():
                texts.append((index, text))

        if not texts:
            self.logger.info("OCR found no text in the uploaded images")
            return None

        noun = "photos" if len(texts) > 1 else "a photo"
        message_parts = [
            f"The text below was read with OCR from {noun} of a single recipe, often a handwritten recipe card.",
            "OCR makes mistakes, so it may contain misread characters, words, and numbers, and lines from "
            "side-by-side columns may be run together. Correct an obvious OCR mistake only where the context "
            "makes the intended word clear. Never invent quantities, times, or steps that are not in the text.",
        ]
        for index, text in texts:
            label = f"Text from photo {index} of {len(images)}:" if len(images) > 1 else "Text:"
            message_parts.append(f'{label}\n"""\n{text}\n"""')

        return await self.ctx.ai.get_response(
            self.ctx.ai.get_prompt(COMPILE_SOURCE_PROMPT),
            truncate_source_content("\n\n".join(message_parts)),
            response_schema=OpenAICompiledSource,
        )
