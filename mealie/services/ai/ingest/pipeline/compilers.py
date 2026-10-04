"""
The card compilers (docs/ai/PHASE2.md §4.1 step 2), and `capture_errors`, which records a failed compiler instead of
letting the compile step log its traceback (which could hold a provider's response body, F3).

Both compilers read the card with upstream's compile prompt plus `card-compile-rules.txt` into
`OpenAIRecipeCardTranscription`, and return a plain `OpenAICompiledSource`: the compile step's `_merge` reads
`image_url` and `language` off every document. The card's attribution (without a leading "From", which the review
page's field and the commit's note title already say), the reader's `unsure` list, which reader read it and how far
the image reader says each page must turn go on the `CardWorkflowContext`.

A two-sided card is one request with both pages. Some local vision models take one image per request and fail it; then
each page is read on its own and the readings are joined. `ONE_IMAGE_PROVIDERS` remembers such a provider, so its
later cards are read page by page at once, but only once it failed that way on `ONE_IMAGE_STRIKES` cards in a row, and
never for an answer that was the model's own trouble (a cut-off or malformed answer, a refusal): one passing error
mustn't make every later card take twice the requests.
"""

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pydantic

from mealie.core import exceptions
from mealie.core.config import get_app_settings
from mealie.core.root_logger import get_logger
from mealie.schema.group.ai_providers import AIProviderOut
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.schema.recipe_ingest import ExtractionUnsure, IngestReadPath
from mealie.services import ocr
from mealie.services.ai.errors import (
    AIProviderLimitReachedError,
    AIProviderLocalOnlyError,
    AIProviderOutputTruncatedError,
    AIProviderRefusedError,
    IngestBusyError,
    IngestPaused,
    describe_provider_error,
    is_rate_limit_error,
)
from mealie.services.openai import OpenAINotEnabledException, OpenAIService
from mealie.services.openai.content import truncate_source_content
from mealie.services.recipe.import_workflow.compilers import COMPILE_SOURCE_PROMPT, ImageCompiler, OCRImageCompiler
from mealie.services.recipe.import_workflow.compilers.base import SourceCompiler
from mealie.services.recipe.import_workflow.context import WorkflowContext

from .attachments import CardImage
from .cardtext import canonical_markers, strip_from_prefix
from .context import CardWorkflowContext
from .llm_schemas import VALID_ROTATIONS, OpenAIRecipeCardTranscription
from .models import CardPage
from .service import end_transaction

CARD_COMPILE_RULES_PROMPT = "recipes.card-compile-rules"
NOTE_FROM_KEY = "recipe-ingest.note-from"

logger = get_logger(__name__)

ONE_IMAGE_PROVIDERS: set[tuple[UUID, str]] = set()
"""
Providers (by id and model) that failed a request with several images and then read each one on its own, on
`ONE_IMAGE_STRIKES` cards in a row: this process reads their cards page by page from then on
"""
ONE_IMAGE_STRIKES = 2
"""How many cards in a row a provider must fail with several images, and read page by page, to be remembered"""
MULTI_IMAGE_FAILURES: dict[tuple[UUID, str], int] = {}
"""Providers' failed requests with several images since the last one they answered, for `ONE_IMAGE_STRIKES`"""

NOT_ABOUT_IMAGES: tuple[type[BaseException], ...] = (
    exceptions.RateLimitError,
    AIProviderLimitReachedError,
    AIProviderLocalOnlyError,
    OpenAINotEnabledException,
    IngestPaused,
    IngestBusyError,
)
"""Failures that reading the pages one by one can't help: the card waits, or the error is reported as it is"""


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


def attribution_text(ctx: CardWorkflowContext, attribution: str | None) -> str | None:
    """
    A reader's attribution as the draft keeps it: markers written as the review page and commit look for them, and
    without its own leading "From" (in English or the job's language), which the field's label already says
    """
    text = canonical_markers((attribution or "").strip())
    return strip_from_prefix(text, ctx.translator.t(NOTE_FROM_KEY)) or None


def _compiled(
    ctx: CardWorkflowContext, response: OpenAIRecipeCardTranscription | None, read_path: IngestReadPath
) -> OpenAICompiledSource | None:
    if response is None:
        return None

    ctx.read_path = read_path
    ctx.attribution = attribution_text(ctx, response.attribution)
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


def may_take_fewer_images(error: BaseException) -> bool:
    """Whether a failed request with several images may succeed with one: a provider error, not a limit or a policy"""
    if not isinstance(error, Exception) or isinstance(error, NOT_ABOUT_IMAGES):
        return False
    return not is_rate_limit_error(error.__cause__ or error)


def is_model_output_error(error: BaseException | None) -> bool:
    """
    Whether a provider's answer failed for the model's own trouble, not the images': malformed or cut off, filtered or
    refused. The card may still be read page by page, but the provider isn't remembered for it.
    """
    import openai

    return isinstance(
        error,
        pydantic.ValidationError
        | openai.LengthFinishReasonError
        | openai.ContentFilterFinishReasonError
        | AIProviderRefusedError
        | AIProviderOutputTruncatedError,
    )


@contextmanager
def answered_attempts(ai: OpenAIService, feature: str) -> Iterator[list[tuple[AIProviderOut, bool]]]:
    """
    Every provider attempt for `feature` (a response schema's name) on `ai`'s runtime meanwhile, as `(provider,
    answered)`: watched where the runtime logs each attempt, so routing, fallbacks and the usage log run as they are.
    An attempt that failed for the model's own trouble (`is_model_output_error`) isn't listed.
    """
    runtime = ai.runtime
    attempts: list[tuple[AIProviderOut, bool]] = []
    record = runtime.record_attempt

    def watch(provider: AIProviderOut, **kwargs: Any) -> None:
        record(provider, **kwargs)
        if kwargs.get("feature") == feature:
            error = kwargs.get("error")
            if is_model_output_error(error):
                return  # neither answered nor a failure about the images
            attempts.append((provider, error is None and kwargs.get("error_type") is None))

    runtime.record_attempt = watch  # type: ignore[method-assign]
    try:
        yield attempts
    finally:
        del runtime.record_attempt  # the class's method again


def _remember_one_image_providers(
    multi: Sequence[tuple[AIProviderOut, bool]], single: Sequence[tuple[AIProviderOut, bool]]
) -> None:
    """
    After a card's request with several images (`multi`'s attempts) and, when it failed, its pages read one by one
    (`single`'s): a provider that answered with several images starts over, and one that failed them but answered
    each page on its own counts a strike; at `ONE_IMAGE_STRIKES` it's remembered (`ONE_IMAGE_PROVIDERS`)
    """
    for provider, answered in multi:
        if answered:
            MULTI_IMAGE_FAILURES.pop((provider.id, provider.model), None)
    failed = {(provider.id, provider.model) for provider, answered in multi if not answered}
    struck = {key for provider, answered in single if answered and (key := (provider.id, provider.model)) in failed}
    for key in struck:
        MULTI_IMAGE_FAILURES[key] = MULTI_IMAGE_FAILURES.get(key, 0) + 1
        if MULTI_IMAGE_FAILURES[key] >= ONE_IMAGE_STRIKES:
            ONE_IMAGE_PROVIDERS.add(key)
            del MULTI_IMAGE_FAILURES[key]


def reads_one_image_at_a_time(ai: OpenAIService) -> bool:
    """Whether the image slot's own provider is one this process saw take only one image per request"""
    provider = ai.image_provider
    return provider is not None and (provider.id, provider.model) in ONE_IMAGE_PROVIDERS


def _page_word(index: int, count: int) -> str:
    """How a page read on its own is named in the joined reading: a word, never a number the flags would count"""
    if index == 0:
        return "Front"
    if index == 1 and count == 2:
        return "Back"
    return "Next page"


def _rotations(pages: Sequence[CardPage], rotations: Sequence[int]) -> dict[int, int]:
    """The turns a reader asked for, by page index, for pages not yet oriented; odd values are ignored"""
    return {
        page.meta.index: rotation
        for page, rotation in zip(pages, rotations, strict=False)
        if not page.meta.oriented and rotation in VALID_ROTATIONS
    }


class CardImageCompiler(ImageCompiler):
    """Reads the card's `view.jpg`s on the image slot: all pages in one request, or page by page (see above)"""

    async def compile(self) -> OpenAICompiledSource | None:
        ctx = _card_context(self.ctx)
        count = len(ctx.pages)
        if count > 1 and reads_one_image_at_a_time(ctx.ai):
            return await self._compile_page_by_page(ctx)

        labels = ", ".join(page_label(index, count) for index in range(count))
        if count == 1:
            message = f"Attached is {labels}: one recipe card."
        else:
            message = f"Attached are {count} images of one recipe card, in order: {labels}."

        feature = OpenAIRecipeCardTranscription.__name__
        try:
            with answered_attempts(ctx.ai, feature) as multi:
                response = await ctx.ai.get_response(
                    _card_prompt(ctx),
                    message,
                    response_schema=OpenAIRecipeCardTranscription,
                    attachments=[CardImage(path=page.view_path) for page in ctx.pages],
                )
        except Exception as e:
            if count == 1 or not may_take_fewer_images(e):
                raise
            reason = describe_provider_error(e)
            logger.info(f"Reading a {count}-page card in one request failed ({reason}); reading each page on its own")
            with answered_attempts(ctx.ai, feature) as single:
                compiled = await self._compile_page_by_page(ctx)
            _remember_one_image_providers(multi, single)
            return compiled

        _remember_one_image_providers(multi, [])
        if response is not None:
            ctx.rotations = _rotations(ctx.pages, response.rotation_clockwise)
        return _compiled(ctx, response, IngestReadPath.image)

    async def _compile_page_by_page(self, ctx: CardWorkflowContext) -> OpenAICompiledSource | None:
        """
        Each page read in a request of its own, the readings joined under the page's name (a word: a number in the
        transcription would count as on the card). The first page's language and attribution, every page's `unsure`.
        """
        count = len(ctx.pages)
        responses: list[OpenAIRecipeCardTranscription] = []
        for index, page in enumerate(ctx.pages):
            message = (
                f"Attached is {page_label(index, count)} of one recipe card that has {count} images; the other images "
                "are read on their own. Transcribe this image only."
            )
            response = await ctx.ai.get_response(
                _card_prompt(ctx),
                message,
                response_schema=OpenAIRecipeCardTranscription,
                attachments=[CardImage(path=page.view_path)],
            )
            if response is None:
                return None
            responses.append(response)

        rotations = [response.rotation_clockwise[0] if response.rotation_clockwise else 0 for response in responses]
        ctx.rotations = _rotations(ctx.pages, rotations)
        joined = OpenAIRecipeCardTranscription(
            contains_recipe=any(response.contains_recipe for response in responses),
            content="\n\n".join(
                f"{_page_word(index, count)}:\n{response.content.strip()}"
                for index, response in enumerate(responses)
                if response.content.strip()
            ),
            language=next((response.language for response in responses if response.language), None),
            attribution=next((response.attribution for response in responses if response.attribution), None),
            unsure=[entry for response in responses for entry in response.unsure],
        )
        return _compiled(ctx, joined, IngestReadPath.image)


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
    have other providers. `OCR_ENABLED=false` turns it off, even when orientation (`AI_INGEST_ORIENT`, which needs
    only Tesseract) stored the pages' text.
    """

    def can_compile(self) -> bool:
        ctx = self.ctx
        if not ctx.input.images or ctx.ai.default_provider is None or not get_app_settings().OCR_ENABLED:
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
