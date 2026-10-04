"""
`POST /api/ai/ingest`'s request handling (docs/ai/PHASE2.md §1.2): the checks made before any body byte is read, the
byte-capped body stream, and the three body shapes (multipart, a raw image, JSON with base64 images).

**Order** (the route's only parameter is `request: Request`, so FastAPI reads nothing before the controller's auth):
1. the controller's auth; then the `Authorization` header must carry a Bearer token: a cookie alone is `401`, even
   beside a header of another scheme (F18);
2. `503 paused_for_restore` (with `Retry-After`) while a restore pauses ingestion; `503 ingest_disabled`;
3. `400 ai_not_enabled` / `local_only_unavailable`, and 4. `429 too_many_jobs` at 200 processing jobs in the group,
   then `429 user_quota` at `AI_INGEST_MAX_PROCESSING_PER_USER` of the uploader's own (when that's set; inbox cards
   have no uploader): one worker-thread call (`intake.reading_readiness`), so provider settings, address lookups and
   the counts never block the event loop. Their counts were read before the body, so intake counts again for every
   card, under the household's intake lock in the insert's transaction (`IntakeOptions.group_cap`, `user_cap`): a
   request whose first card finds a cap reached (another upload got in first, the same user's at once, say) gets the
   same `429`, with nothing in; a later card that would pass a cap is refused on its own, `quota` in `rejected`, so
   requests running at once can't each add more past it;
5. `413` by `Content-Length` (45 MiB for JSON), and `415` for any other content type;
6. the body, through a byte counter that also stops chunked bodies at the same caps.

Multipart is parsed by Starlette's `MultiPartParser` over the capped stream; its file parts spool to the system temp
directory (never `DATA_DIR`) and go to intake as open file objects, closed when the request ends. A raw image body is
spooled the same way, and so is a JSON body, which is then decoded in one of the process's intake slots (it takes about
three times its size in memory): leniently (line breaks, a `data:` prefix, URL-safe letters), each image into a spooled
file of its own, so cards waiting for intake hold no decoded images in memory. A JSON image may instead be a URL
(`{"url": ...}`, a bare string is always base64): fetched after every check above and the batch's, one after another in
its place, when `AI_INGEST_URL_FETCH` allows it (`fetch_url`), else refused `url_not_allowed`. Each card then goes
through `IntakeService.ingest_async`.

The answer is `202 IngestResponse`, whose `summary` is in the request's language for a Shortcut's notification (it
promises a notification only when a household notifier sends "recipe cards ready") and which has nothing named
`message` (the frontend toasts any). `400 nothing_accepted` carries the same body in `detail`.
Refusals are `UploadRefused`, which the route turns into `{"detail": {"code", "message"?}}`.
"""

import base64
import binascii
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from tempfile import SpooledTemporaryFile
from typing import Any, BinaryIO, Literal
from uuid import UUID

import anyio.to_thread
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

from mealie.core.exceptions import NoEntryFound
from mealie.lang.locale_config import LOCALE_CONFIG
from mealie.lang.providers import Translator, get_locale_provider
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import (
    IngestedJob,
    IngestRejected,
    IngestRejectReason,
    IngestResponse,
    IngestSource,
    IngestStatus,
)
from mealie.schema.user.user import PrivateUser
from mealie.services.ai.errors import IngestPaused

from . import events, limits, storage
from .fetch_url import fetch_image, url_filename
from .i18n import DEFAULT_LOCALE, FallbackTranslator
from .images import sanitize_filename
from .intake import (
    IntakeAccepted,
    IntakeCard,
    IntakeOptions,
    IntakePage,
    IntakeService,
    QuotaReached,
    ReadingReadiness,
    in_intake_slot,
    reading_readiness,
    source_name,
)
from .settings import get_ingest_settings

SPOOL_MAX_BYTES = 1024 * 1024
"""A raw image or JSON body is kept in memory up to this size, then in an unnamed file in the system temp directory"""

_TRUE = {"1", "true", "yes", "on"}
_LANGUAGE_ALIASES = {"nb": "no", "nn": "no"}
"""Languages Mealie keys under another code: Norwegian Bokmål and Nynorsk are its `no-NO`"""
_TRADITIONAL_CHINESE_REGIONS = {"tw", "hk", "mo"}
_DATA_URL = re.compile(r"^data:[^,]*,", re.IGNORECASE)
_NOT_BASE64 = re.compile(r"[^A-Za-z0-9+/=]")

# Error codes, matching `recipe-ingest.error.<code>` in the frontend
AUTHORIZATION_REQUIRED = "authorization_required"
PAUSED_FOR_RESTORE = "paused_for_restore"
INGEST_DISABLED = "ingest_disabled"
AI_NOT_ENABLED = "ai_not_enabled"
LOCAL_ONLY_UNAVAILABLE = "local_only_unavailable"
TOO_MANY_JOBS = "too_many_jobs"
USER_QUOTA = "user_quota"
TOO_LARGE = "too_large"
UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
INVALID_BODY = "invalid_body"
NOT_FOUND = "not_found"
NOTHING_ACCEPTED = "nothing_accepted"

BodyKind = Literal["multipart", "raw", "json"]


class UploadRefused(Exception):
    """
    An upload answered with an error: the route turns it into `{"detail": {"code": code, "message"?: ..., **params}}`,
    with `message` translated from `message_key` when there is one.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        *,
        message_key: str | None = None,
        message_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        **params: Any,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message_key = message_key
        self.message_params = dict(message_params or {})
        self.headers = dict(headers or {})
        self.params = params


class _BodyTooLarge(Exception):
    pass


# ==================================================================================================================
# Small parsers


def resolve_locale(accept_language: str | None) -> str:
    """
    The supported locale an `Accept-Language` header asks for first: an exact tag (`de-DE`), else the language's
    usual locale (`de` → `de-DE`, `en` → `en-US`), else en-US. Browsers and iOS send lists like `de-DE,de;q=0.9`.
    """
    if not accept_language:
        return DEFAULT_LOCALE

    weighted: list[tuple[float, int, str]] = []
    for order, part in enumerate(accept_language.split(",")):
        tag, _, params = part.strip().partition(";")
        quality = 1.0
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        tag = tag.strip()
        if tag and tag != "*" and quality > 0:
            weighted.append((-quality, order, tag))

    for _, _, tag in sorted(weighted):
        if locale := _match_locale(tag):
            return locale
    return DEFAULT_LOCALE


def _match_locale(tag: str) -> str | None:
    """
    A supported locale for one language tag: the exact tag; else its language and region, whatever script sits between
    them (`pt-Latn-BR`); Chinese by script, then by the regions that write Traditional Chinese (`zh-Hant`, `zh-HK` →
    `zh-TW`); else the language's usual locale
    """
    wanted = tag.replace("_", "-").lower()
    by_lower = {key.lower(): key for key in LOCALE_CONFIG}
    if wanted in by_lower:
        return by_lower[wanted]

    language, *subtags = wanted.split("-")
    language = _LANGUAGE_ALIASES.get(language, language)
    script = next((subtag for subtag in subtags if len(subtag) == 4 and subtag.isalpha()), None)
    region = next((subtag for subtag in subtags if len(subtag) == 2 or (len(subtag) == 3 and subtag.isdigit())), None)
    if language == "zh":
        traditional = script == "hant" or (script != "hans" and region in _TRADITIONAL_CHINESE_REGIONS)
        region = "tw" if traditional else "cn"
    if region and f"{language}-{region}" in by_lower:
        return by_lower[f"{language}-{region}"]

    same_language = [key for key in LOCALE_CONFIG if key.lower().split("-", 1)[0] == language]
    if not same_language:
        return None
    usual = "en-us" if language == "en" else f"{language}-{language}"
    return next((key for key in same_language if key.lower() == usual), same_language[0])


def parse_bool(value: Any) -> bool:
    """A lenient flag: `true`, `1`, `yes` and `on` (any case), or a JSON boolean"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return isinstance(value, str) and value.strip().lower() in _TRUE


def decode_base64(data: str) -> bytes | None:
    """
    Base64 as Shortcuts and scripts send it: with line breaks and spaces, a `data:<type>;base64,` prefix, missing
    padding or the URL-safe alphabet. None when it isn't base64 at all.
    """
    text = _DATA_URL.sub("", data.strip(), count=1)
    text = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/")
    if not text or _NOT_BASE64.search(text):
        return None
    text = text.rstrip("=")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error, ValueError:
        return None


@dataclass
class UploadOptions:
    batch_id: UUID | Literal["new"] | None = None
    position: int | None = None
    split: bool = False
    local_only: bool = False
    allow_duplicate: bool = False
    done: bool = False
    """This upload ends its batch: the route seals it once the card is in (the app's Done, for a Shortcut)"""

    @classmethod
    def parse(cls, values: Mapping[str, Any]) -> UploadOptions:
        """From query parameters, form fields or JSON keys (camelCase, as documented, or snake_case)"""

        def get(name: str, snake: str) -> Any:
            return values.get(name, values.get(snake))

        return cls(
            batch_id=_parse_batch_id(get("batchId", "batch_id")),
            position=_parse_position(get("position", "position")),
            split=parse_bool(get("split", "split")),
            local_only=parse_bool(get("localOnly", "local_only")),
            allow_duplicate=parse_bool(get("allowDuplicate", "allow_duplicate")),
            done=parse_bool(get("done", "done")),
        )


def _parse_batch_id(value: Any) -> UUID | Literal["new"] | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and value.strip().lower() == "new":
        return "new"
    try:
        return UUID(str(value).strip())
    except ValueError:
        raise _unknown_batch() from None  # an unknown batch, as far as the client is concerned


def _parse_position(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise _invalid_body()
    try:
        position = int(str(value).strip()) if not isinstance(value, int) else value
    except ValueError:
        raise _invalid_body() from None
    if position < 0:
        raise _invalid_body()
    return position


def body_kind(content_type: str | None) -> BodyKind | None:
    """Which body shape a `Content-Type` names; None for anything else (415)"""
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type == "multipart/form-data":
        return "multipart"
    if media_type.startswith("image/") or media_type == "application/octet-stream":
        return "raw"
    if media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json")):
        return "json"
    return None


def _content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", value, re.IGNORECASE)
    return match.group(1).strip() if match else None


# ==================================================================================================================
# The body


@dataclass
class UploadedImage:
    file: BinaryIO | None
    """None when the image was refused while reading (`rejected`), or is still to be fetched (`url`)"""
    filename: str | None
    index: int
    rejected: IngestRejectReason | None = None
    url: str | None = None
    """A JSON image given by its URL, fetched after the checks (`fetch_url`)"""


@dataclass
class UploadBody:
    images: list[UploadedImage] = field(default_factory=list)
    options: UploadOptions = field(default_factory=UploadOptions)
    _open: list[Any] = field(default_factory=list)

    def close(self) -> None:
        for file in self._open:
            try:
                file.close()
            except Exception:
                pass
        self._open.clear()


async def _capped(request: Request, limit: int) -> AsyncIterator[bytes]:
    """The request body, stopped with `_BodyTooLarge` as soon as more than `limit` bytes arrived (chunked included)"""
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _BodyTooLarge()
        if chunk:
            yield chunk


def _unknown_batch() -> UploadRefused:
    """404 `not_found`: a `batchId` that isn't one of the household's batches (the PWA starts another one)"""
    return UploadRefused(404, NOT_FOUND, message_key="recipe-ingest.errors.unknown-batch")


def _invalid_body() -> UploadRefused:
    """400 `invalid_body`: a body of a supported type that can't be read (a broken form, bad JSON, a bad option)"""
    return UploadRefused(400, INVALID_BODY, message_key="recipe-ingest.errors.invalid-body")


async def _read_multipart(request: Request, limit: int, query: Mapping[str, str]) -> UploadBody:
    parser = MultiPartParser(
        request.headers,
        _capped(request, limit),
        max_files=limits.MAX_IMAGES_PER_REQUEST,
        max_fields=limits.MAX_MULTIPART_FIELDS,
    )
    try:
        form = await parser.parse()
    except MultiPartException as e:
        raise _invalid_body() from e

    body = UploadBody()
    fields: dict[str, Any] = dict(query)
    for name, value in form.multi_items():
        if isinstance(value, UploadFile):
            body._open.append(value.file)
            if not value.filename and not value.size:
                continue  # an empty file input, as browsers send it
            body.images.append(UploadedImage(value.file, value.filename, len(body.images)))
        else:
            fields[name] = value

    try:
        body.options = UploadOptions.parse(fields)
    except BaseException:
        body.close()
        raise
    return body


async def _read_raw(request: Request, limit: int, query: Mapping[str, str]) -> UploadBody:
    options = UploadOptions.parse(query)
    filename = query.get("filename") or _content_disposition_filename(request.headers.get("content-disposition"))
    spooled: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    upload = UploadFile(spooled, size=0, filename=filename)  # type: ignore[arg-type]
    body = UploadBody(options=options, _open=[spooled])

    try:
        too_large = False
        async for chunk in _capped(request, limit):
            if too_large or (upload.size or 0) + len(chunk) > limits.MAX_FILE_BYTES:
                too_large = True  # keep reading to the request's cap, without keeping the bytes
                continue
            await upload.write(chunk)
        await upload.seek(0)
    except BaseException:
        body.close()
        raise

    if too_large:
        body.images.append(UploadedImage(None, filename, 0, rejected=IngestRejectReason.too_large))
    else:
        body.images.append(UploadedImage(spooled, filename, 0))  # type: ignore[arg-type]
    return body


def _decode_json_images(payload: Any, opened: list[Any]) -> list[UploadedImage]:
    """
    The body's images, each decoded into a spooled file (added to `opened`), so a card waiting for its intake holds
    its images on disk rather than in memory
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("images"), list):
        raise _invalid_body()
    items = payload["images"]
    if len(items) > limits.MAX_IMAGES_PER_REQUEST:
        raise _invalid_body()

    decoded: list[UploadedImage] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            data: Any = item  # a bare string is always base64, never a URL
            filename = None
        elif isinstance(item, dict):
            data = item.get("data")
            filename = item.get("filename") if isinstance(item.get("filename"), str) else None
            if "url" in item:
                url = item["url"]
                if not isinstance(url, str) or "data" in item:
                    raise _invalid_body()  # one image is either a URL or data
                decoded.append(UploadedImage(None, filename or url_filename(url), index, url=url))
                continue
        else:
            raise _invalid_body()

        raw = decode_base64(data) if isinstance(data, str) else None
        if raw is None:
            decoded.append(UploadedImage(None, filename, index, rejected=IngestRejectReason.unreadable_image))
        elif len(raw) > limits.MAX_FILE_BYTES:
            decoded.append(UploadedImage(None, filename, index, rejected=IngestRejectReason.too_large))
        else:
            spooled: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
            opened.append(spooled)
            spooled.write(raw)
            spooled.seek(0)
            decoded.append(UploadedImage(spooled, filename, index))  # type: ignore[arg-type]
        del raw
    return decoded


async def _read_json(request: Request, limit: int, query: Mapping[str, str]) -> UploadBody:
    spooled: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    body = UploadFile(spooled)  # type: ignore[arg-type]
    result = UploadBody()

    def decode() -> Any:
        spooled.seek(0)
        try:
            payload = json.loads(spooled.read())
        except ValueError as e:  # bad JSON or bad UTF-8
            raise _invalid_body() from e
        finally:
            spooled.close()
        result.images = _decode_json_images(payload, result._open)
        return payload

    try:
        async for chunk in _capped(request, limit):
            await body.write(chunk)
        # the body waits on disk, and only INTAKE_CONCURRENCY are decoded in memory at once; the decoded images wait
        # on disk too
        payload = await in_intake_slot(decode)
        result.options = UploadOptions.parse({**query, **{k: v for k, v in payload.items() if k != "images"}})
    except BaseException:
        result.close()
        raise
    finally:
        spooled.close()
    return result


async def _fetch_urls(body: UploadBody) -> None:
    """
    The body's image URLs, fetched one after another in their places (only now: after auth and every check), into
    spooled files the body closes; a URL that can't be fetched is that image's rejection. Together they may take at
    most the request's own cap (`AI_INGEST_MAX_UPLOAD_MB`), each at most `MAX_FILE_BYTES`.
    """
    remaining = get_ingest_settings().max_upload_bytes
    for image in body.images:
        if image.url is None or image.rejected is not None:
            continue
        fetched = await fetch_image(image.url, max_bytes=min(limits.MAX_FILE_BYTES, remaining))
        if isinstance(fetched, IngestRejectReason):
            image.rejected = fetched
            continue
        body._open.append(fetched.file)
        image.file = fetched.file
        remaining -= fetched.size


# ==================================================================================================================
# The handler


def _summary(translator: Translator, accepted: int, rejected: list[IngestRejected], *, notifies: bool = False) -> str:
    """
    For a Shortcut's notification: the cards queued, then those already scanned, then those refused for another
    reason (each counted once, however many photos it had). "You'll be notified when it's ready" only when the
    household `notifies` (a notifier sends "recipe cards ready"); otherwise the cards wait for review in Mealie.
    """
    if not accepted:
        queued = translator.t("recipe-ingest.upload-summary-none")
    elif notifies:
        queued = translator.t("recipe-ingest.upload-summary", count=accepted)
    else:
        queued = translator.t("recipe-ingest.upload-summary-in-app", count=accepted)
    parts = [queued]
    duplicates = sum(1 for item in rejected if item.reason == IngestRejectReason.duplicate)
    if duplicates:
        parts.append(translator.t("recipe-ingest.upload-summary-duplicate", count=duplicates))
    if others := len(rejected) - duplicates:
        parts.append(translator.t("recipe-ingest.upload-summary-rejected", count=others))
    return " ".join(parts)


@dataclass
class UploadHandler:
    """One `POST /api/ai/ingest`: `await UploadHandler(...).handle()`"""

    request: Request
    session: Session
    user: PrivateUser
    integration_id: str | None = None
    locale: str = field(init=False)
    translator: Translator = field(init=False)
    options: UploadOptions | None = field(init=False, default=None)
    """The upload's options, once its body has been read"""

    def __post_init__(self) -> None:
        self.locale = resolve_locale(self.request.headers.get("accept-language"))
        self.translator = FallbackTranslator(get_locale_provider(self.locale), get_locale_provider(DEFAULT_LOCALE))

    @property
    def group_id(self) -> UUID:
        return self.user.group_id

    @property
    def household_id(self) -> UUID:
        return self.user.household_id

    def _check_before_body(self) -> None:
        """Checks 1 and 2, made on the event loop: cheap, and no database"""
        # 1. the controller authenticated the request; a cookie alone isn't enough (F18). Any other scheme (a proxy's
        # Basic credentials, which browsers add to a cross-site post) leaves the cookie authenticating it.
        scheme, _, token = self.request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise UploadRefused(401, AUTHORIZATION_REQUIRED, message_key="recipe-ingest.errors.authorization-required")
        # 2. a restore pausing ingestion, or ingestion switched off
        if storage.is_paused():
            raise self._paused()
        if not get_ingest_settings().ENABLED:
            raise UploadRefused(503, INGEST_DISABLED, message_key="recipe-ingest.errors.ingest-disabled")

    @staticmethod
    def _paused() -> UploadRefused:
        return UploadRefused(
            503,
            PAUSED_FOR_RESTORE,
            message_key="recipe-ingest.errors.paused-for-restore",
            headers={"Retry-After": str(limits.PAUSED_RETRY_AFTER)},
        )

    @staticmethod
    def _check_readiness(readiness: ReadingReadiness, user_cap: int = 0) -> None:
        """Checks 3 and 4, from `reading_readiness`; `user_cap` is `AI_INGEST_MAX_PROCESSING_PER_USER` (0: off)"""
        if not readiness.can_read:
            raise UploadRefused(400, AI_NOT_ENABLED, message_key="recipe-ingest.errors.ai-not-enabled")
        if readiness.group_local_only and not readiness.local_ready:
            raise UploadRefused(400, LOCAL_ONLY_UNAVAILABLE, message_key="recipe-ingest.errors.local-only-unavailable")
        if readiness.processing >= limits.MAX_PROCESSING_JOBS_PER_GROUP:
            raise UploadHandler._too_many_jobs()
        if user_cap and readiness.user_processing is not None and readiness.user_processing >= user_cap:
            raise UploadHandler._user_quota(user_cap)

    @staticmethod
    def _too_many_jobs() -> UploadRefused:
        return UploadRefused(
            429,
            TOO_MANY_JOBS,
            message_key="recipe-ingest.errors.too-many-jobs",
            headers={"Retry-After": str(limits.QUOTA_RETRY_AFTER)},
        )

    @staticmethod
    def _user_quota(user_cap: int) -> UploadRefused:
        return UploadRefused(
            429,
            USER_QUOTA,
            message_key="recipe-ingest.errors.user-quota",
            message_params={"count": user_cap},
            headers={"Retry-After": str(limits.QUOTA_RETRY_AFTER)},
        )

    def _cap(self, kind: BodyKind | None) -> int:
        if kind == "json":
            return min(limits.MAX_JSON_BODY_BYTES, get_ingest_settings().max_upload_bytes)
        return get_ingest_settings().max_upload_bytes

    def _too_large(self, cap: int) -> UploadRefused:
        return UploadRefused(
            413,
            TOO_LARGE,
            message_key="recipe-ingest.errors.too-large",
            message_params={"max": cap // limits.MIB},
        )

    def _check_length(self, kind: BodyKind | None) -> int:
        """Check 5: the declared length against the cap, and the content type; returns the cap"""
        cap = self._cap(kind)
        declared = self.request.headers.get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > cap
            except ValueError:
                too_large = False  # the server rejects a malformed length itself; the counter still applies
            if too_large:
                raise self._too_large(cap)
        if kind is None:
            raise UploadRefused(415, UNSUPPORTED_MEDIA_TYPE, message_key="recipe-ingest.errors.unsupported-media-type")
        return cap

    async def _read_body(self, kind: BodyKind, cap: int) -> UploadBody:
        """Check 6: the body, through the byte counter"""
        query = dict(self.request.query_params)
        try:
            if kind == "multipart":
                return await _read_multipart(self.request, cap, query)
            if kind == "raw":
                return await _read_raw(self.request, cap, query)
            return await _read_json(self.request, cap, query)
        except _BodyTooLarge as e:
            raise self._too_large(cap) from e

    def _household_notifies(self) -> bool:
        """Whether a notifier tells the household when its cards are ready (the summary says so only then). Blocking."""
        try:
            return events.household_notifies(self.session, self.group_id, self.household_id)
        finally:
            if self.session.in_transaction():
                self.session.commit()

    def _check_batch(self, batch_id: UUID | Literal["new"] | None) -> None:
        """An explicit batch must be one of the household's (sealed is fine: it's replaced). Blocking."""
        if not isinstance(batch_id, UUID):
            return
        try:
            exists = IngestRepos(self.session, self.group_id, self.household_id).batches.get(batch_id) is not None
        finally:
            if self.session.in_transaction():
                self.session.commit()
        if not exists:
            raise _unknown_batch()

    async def handle(self) -> IngestResponse:
        """The whole request; raises `UploadRefused` for every refusal"""
        self._check_before_body()
        user_cap = get_ingest_settings().MAX_PROCESSING_PER_USER
        readiness = await anyio.to_thread.run_sync(
            reading_readiness, self.session, self.group_id, self.household_id, self.user.id if user_cap else None
        )
        self._check_readiness(readiness, user_cap)

        kind = body_kind(self.request.headers.get("content-type"))
        cap = self._check_length(kind)
        assert kind is not None

        body = await self._read_body(kind, cap)
        try:
            options = self.options = body.options
            # a card's own "keep it on this server", with the same refusal as the group's setting
            if options.local_only and not readiness.local_ready:
                raise UploadRefused(
                    400, LOCAL_ONLY_UNAVAILABLE, message_key="recipe-ingest.errors.local-only-unavailable"
                )
            await anyio.to_thread.run_sync(self._check_batch, options.batch_id)
            await _fetch_urls(body)
            return await self._ingest(
                body, local_only=readiness.group_local_only or options.local_only, user_cap=user_cap
            )
        finally:
            body.close()

    async def _ingest(self, body: UploadBody, *, local_only: bool, user_cap: int = 0) -> IngestResponse:
        options = body.options
        cards = [[image] for image in body.images] if options.split else ([body.images] if body.images else [])

        service = IntakeService(self.session, self.group_id, self.household_id)
        batch_id = options.batch_id
        position = options.position
        jobs: list[IngestedJob] = []
        rejected: list[IngestRejected] = []

        for card in cards:
            refused = next((image for image in card if image.rejected is not None), None)
            if refused is not None:
                assert refused.rejected is not None
                rejected.append(
                    IngestRejected(
                        index=refused.index, filename=sanitize_filename(refused.filename), reason=refused.rejected
                    )
                )
                continue

            front = card[0]
            intake_card = IntakeCard(
                pages=[IntakePage(image.file, image.filename, image.index) for image in card if image.file],
                source_name=source_name("upload", sanitize_filename(front.filename)),
            )
            intake_options = IntakeOptions(
                source=IngestSource.api,
                batch_id=batch_id,
                created_by=self.user.id,
                position=position,
                local_only=local_only,
                allow_duplicate=options.allow_duplicate,
                locale=self.locale,
                integration_id=self.integration_id,
                # check 4 again for every card, counted in the insert's transaction
                group_cap=limits.MAX_PROCESSING_JOBS_PER_GROUP,
                user_cap=user_cap or None,
            )
            try:
                outcome = await service.ingest_async(intake_card, intake_options)
            except IngestPaused as e:
                raise self._paused() from e
            except NoEntryFound as e:
                raise _unknown_batch() from e
            except QuotaReached as e:
                if not jobs:  # nothing of the request is in: it's refused whole, as by the counts before the body
                    raise (self._too_many_jobs() if e.which == "group" else self._user_quota(user_cap)) from e
                rejected.append(
                    IngestRejected(
                        index=front.index, filename=sanitize_filename(front.filename), reason=IngestRejectReason.quota
                    )
                )
                continue

            if isinstance(outcome, IntakeAccepted):
                # the request's further cards join the same batch, in order
                batch_id = outcome.batch_id
                if position is not None:
                    position += 1
                jobs.append(
                    IngestedJob(
                        id=outcome.job_id,
                        status=IngestStatus.processing,
                        page_count=outcome.page_count,
                        review_path=f"/g/{self.user.group_slug}/recipes/cards/{outcome.job_id}",
                    )
                )
            else:
                rejected.append(
                    IngestRejected(
                        index=outcome.index,
                        filename=outcome.filename,
                        reason=outcome.reason,
                        duplicate_of=outcome.duplicate_of,
                    )
                )

        notifies = bool(jobs) and await anyio.to_thread.run_sync(self._household_notifies)
        response = IngestResponse(
            batch_id=batch_id if jobs and isinstance(batch_id, UUID) else None,
            jobs=jobs,
            rejected=rejected,
            summary=_summary(self.translator, len(jobs), rejected, notifies=notifies),
        )
        if not jobs:
            raise UploadRefused(400, NOTHING_ACCEPTED, **response.model_dump(mode="json", by_alias=True))
        return response
