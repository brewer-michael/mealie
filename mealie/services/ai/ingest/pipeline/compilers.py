"""
The card compilers (docs/ai/PHASE2.md §4.1 step 2), and `capture_errors`, which records a failed compiler instead of
letting the compile step log its traceback (which could hold a provider's response body, F3).

Both compilers read the card with upstream's compile prompt plus `card-compile-rules.txt` into
`OpenAIRecipeCardTranscription`, and return a plain `OpenAICompiledSource`: the compile step's `_merge` reads
`image_url` and `language` off every document. The card's attribution, the reader's `unsure` list and which reader read
it go on the `CardWorkflowContext`.
"""

import asyncio
from dataclasses import dataclass

from mealie.core import exceptions
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.schema.recipe_ingest import ExtractionUnsure, IngestReadPath
from mealie.services import ocr
from mealie.services.ai.errors import describe_provider_error
from mealie.services.openai.content import truncate_source_content
from mealie.services.recipe.import_workflow.compilers import COMPILE_SOURCE_PROMPT, ImageCompiler, OCRImageCompiler
from mealie.services.recipe.import_workflow.compilers.base import SourceCompiler
from mealie.services.recipe.import_workflow.context import WorkflowContext

from .attachments import CardImage
from .cardtext import canonical_markers
from .context import CardWorkflowContext
from .llm_schemas import OpenAIRecipeCardTranscription
from .service import end_transaction

CARD_COMPILE_RULES_PROMPT = "recipes.card-compile-rules"


@dataclass
class CapturedError:
    """A compiler that failed"""

    compiler: str
    """The compiler's class name"""
    error: BaseException
    description: str
    """`describe_provider_error(error)`: safe to store and show"""


def capture_errors(compiler: type[SourceCompiler], errors: list[CapturedError]) -> type[SourceCompiler]:
    """
    `compiler`, wrapped so that an exception it raises is appended to `errors` and its `compile()` returns None
    instead: never re-raised, never logged with a traceback. The compile step then moves on to the next compiler.
    """

    class ErrorCapturingCompiler(SourceCompiler):
        wrapped = compiler
        source_type = compiler.source_type
        progress_key = compiler.progress_key

        def __init__(self, ctx: WorkflowContext, content: str | None = None) -> None:
            super().__init__(ctx, content)
            self.compiler = compiler(ctx, content)

        def can_compile(self) -> bool:
            return self.compiler.can_compile()

        async def compile(self) -> OpenAICompiledSource | None:
            if isinstance(self.ctx, CardWorkflowContext):
                self.ctx.compilers_tried.append(compiler.__name__)
            try:
                return await self.compiler.compile()
            except Exception as e:
                description = describe_provider_error(e)
                errors.append(CapturedError(compiler=compiler.__name__, error=e, description=description))
                # the description only: the error's message can hold the provider's response body
                self.logger.warning(f"{compiler.__name__} couldn't read the card ({description})")
                return None

    # the compile step and the eval report compilers by class name
    ErrorCapturingCompiler.__name__ = compiler.__name__
    ErrorCapturingCompiler.__qualname__ = compiler.__qualname__
    return ErrorCapturingCompiler


def _card_context(ctx: WorkflowContext) -> CardWorkflowContext:
    if not isinstance(ctx, CardWorkflowContext):
        raise TypeError("The card compilers read a CardWorkflowContext")
    return ctx


def _card_prompt(ctx: WorkflowContext) -> str:
    return f"{ctx.ai.get_prompt(COMPILE_SOURCE_PROMPT)}\n\n{ctx.ai.get_prompt(CARD_COMPILE_RULES_PROMPT)}"


def page_label(index: int, page_count: int) -> str:
    """How a page is named to the model: "Image 1 (front)", "Image 2 (back)", or "Image 3" for further pages"""
    if index == 0:
        return "Image 1 (front)"
    if index == 1 and page_count == 2:
        return "Image 2 (back)"
    return f"Image {index + 1}"


def _compiled(
    ctx: CardWorkflowContext, response: OpenAIRecipeCardTranscription | None, read_path: IngestReadPath
) -> OpenAICompiledSource | None:
    if response is None:
        return None

    ctx.read_path = read_path
    # markers written exactly as the review page and commit look for them, as in every other field of the draft
    ctx.attribution = canonical_markers((response.attribution or "").strip()) or None
    ctx.unsure = [
        ExtractionUnsure(
            text=entry.text.strip(),
            alternatives=[alternative.strip() for alternative in entry.alternatives if alternative.strip()],
            reason=entry.reason,
        )
        for entry in response.unsure
        if entry.text.strip()
    ]
    return OpenAICompiledSource(
        contains_recipe=response.contains_recipe, content=response.content, language=response.language
    )


class CardImageCompiler(ImageCompiler):
    """Reads the card's `view.jpg`s on the image slot"""

    async def compile(self) -> OpenAICompiledSource | None:
        ctx = _card_context(self.ctx)
        count = len(ctx.pages)
        labels = ", ".join(page_label(index, count) for index in range(count))
        if count == 1:
            message = f"Attached is {labels}: one recipe card."
        else:
            message = f"Attached are {count} images of one recipe card, in order: {labels}."

        response = await ctx.ai.get_response(
            _card_prompt(ctx),
            message,
            response_schema=OpenAIRecipeCardTranscription,
            attachments=[CardImage(path=page.view_path) for page in ctx.pages],
        )
        return _compiled(ctx, response, IngestReadPath.image)


OCR_MESSAGE = (
    "The text below was read with OCR (on-device text recognition) from photos of one recipe card, often "
    "handwritten. OCR makes mistakes: characters, words and numbers may be misread, and lines from side-by-side "
    "columns may be run together. Transcribe the card following the rules above. Where a word is plainly an OCR "
    'misreading (e.g. "f1our" for "flour"), write the word the context makes clear, but never change a quantity, '
    "time, temperature or abbreviation, and write [illegible] where the text is too garbled to read."
)


class CardOCRCompiler(OCRImageCompiler):
    """
    The OCR fallback: the text Tesseract read while orienting each page (`PageMeta.ocr`, read again only when it's
    missing), structured on the **default** slot with the same rules and schema as the image read.

    It doesn't run after the image compiler was rate limited: that card waits and is read properly (§3.6). A
    monthly limit, a missing image provider or a local-only refusal still fall back here, since the default slot may
    have other providers.
    """

    def can_compile(self) -> bool:
        ctx = self.ctx
        if not ctx.input.images or ctx.ai.default_provider is None:
            return False

        if isinstance(ctx, CardWorkflowContext):
            if any(
                captured.compiler == CardImageCompiler.__name__
                and isinstance(captured.error, exceptions.RateLimitError)
                for captured in ctx.compiler_errors
            ):
                return False
            if ctx.pages and all(page.meta.ocr is not None for page in ctx.pages):
                return True

        return ocr.is_available()

    async def compile(self) -> OpenAICompiledSource | None:
        ctx = _card_context(self.ctx)

        texts: list[tuple[int, str]] = []
        confidences: list[float] = []
        for index, page in enumerate(ctx.pages):
            if page.meta.ocr is not None:
                text, confidence = page.meta.ocr.text, page.meta.ocr.confidence
            else:
                end_transaction(ctx.repos.session)  # Tesseract takes seconds; no transaction stays open meanwhile
                result = await asyncio.to_thread(ocr.extract_text, page.page_path)
                text, confidence = result.text, result.confidence

            if text := text.strip():
                texts.append((index, text))
                confidences.append(confidence)

        if not texts:
            self.logger.info("OCR found no text on the card")
            return None

        message_parts = [OCR_MESSAGE]
        for index, text in texts:
            message_parts.append(f'Text from {page_label(index, len(ctx.pages))}:\n"""\n{text}\n"""')

        response = await ctx.ai.get_response(
            _card_prompt(ctx),
            truncate_source_content("\n\n".join(message_parts)),
            response_schema=OpenAIRecipeCardTranscription,
        )
        compiled = _compiled(ctx, response, IngestReadPath.ocr)
        if compiled is not None:
            ctx.ocr_confidence = sum(confidences) / len(confidences)
        return compiled
