"""
Turning an uploaded photo into a card page, and the page operations after that (docs/ai/PHASE2.md §2, §4.4, §4.7).

`normalize_page` runs inside the upload request (or the inbox scan), so the uploaded bytes, GPS included, never
outlive it: what's stored is an upright, metadata-free JPEG plus two smaller copies. One Pillow path covers every
accepted format and enforces the pixel caps before anything is decoded: `limits.MAX_PIXELS` for every format, after a
JPEG's reduced-scale decoding (`draft`), so a 200-megapixel phone JPEG (up to `MAX_JPEG_SOURCE_PIXELS`) is read at
half size. A progressive JPEG, or one whose components are in scans of their own, also has its decoding's memory
capped (`MAX_JPEG_COEFFICIENT_BYTES`: libjpeg keeps all its coefficients at full resolution, whatever the scale) and
its scans (`MAX_JPEG_SCANS`: each goes over them all).
Images are opened with their format's own Pillow opener, not `Image.open`, whose decompression-bomb check would refuse
those photos (and warn from about 89 megapixels): these caps apply instead, and Pillow's global `MAX_IMAGE_PIXELS` is
never changed.

**Turning a page is staged**, so a crash can't leave its files turned and its stored metadata not (or the reverse):
1. `stage_rotation` writes the turned page beside the current one (`page.next.jpg` first, then `view.next.jpg` and
   `thumb.next.webp`) and returns its metadata, whose `page_sha256` names `page.next.jpg`;
2. the caller stores that metadata (a conditional update);
3. `apply_staged` moves the staged files over the current ones (thumbnail, view, then `page.jpg` last), or
   `discard_staged` removes them (`page.next.jpg` last) when the update was refused.

`recover_staged(page_dir, stored_meta)` finishes or undoes whatever a crash left: staged files whose page matches the
stored metadata were committed and are swapped in; any others are discarded. Callers hold `storage.ingest_write()`.
"""

import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Literal, NamedTuple, Protocol, cast

from PIL import ExifTags, Image, UnidentifiedImageError

import mealie.pkgs.img  # noqa: F401  (registers the HEIF opener)
from mealie.core.root_logger import get_logger
from mealie.schema.recipe_ingest import IngestRejectReason, PageMeta, PageRotationSource

from . import limits
from .settings import get_ingest_settings
from .storage import atomic_save_image, atomic_write_bytes

logger = get_logger(__name__)

PAGE_FILE = "page.jpg"
VIEW_FILE = "view.jpg"
THUMB_FILE = "thumb.webp"
STAGED_FILES = {PAGE_FILE: "page.next.jpg", VIEW_FILE: "view.next.jpg", THUMB_FILE: "thumb.next.webp"}
"""A page file's staged name: where a turned page waits until its metadata is stored"""
SWAP_ORDER = (THUMB_FILE, VIEW_FILE, PAGE_FILE)
"""The order staged files replace the current ones: `page.jpg` last, as its hash says the page changed"""

SNIFF_BYTES = 16
"""How much of a file `sniff` needs"""
SPOOL_MAX_BYTES = 1024 * 1024
"""A rendered PDF page is kept in memory up to this size, then in an unnamed file in the system temp directory"""

MAX_JPEG_SOURCE_PIXELS = 260_000_000
"""
The most pixels a JPEG may have. A JPEG is decoded at 1/2, 1/4 or 1/8 scale when the page is that much smaller
(`draft`), and the decoded size must still fit `limits.MAX_PIXELS`: a 200-megapixel phone photo is read at half size.
That bounds a baseline JPEG's decoding, which goes a row of blocks at a time; a multi-scan one's is bounded by
`MAX_JPEG_COEFFICIENT_BYTES` as well.
"""
MAX_JPEG_COEFFICIENT_BYTES = 600_000_000
"""
The most memory a multi-scan JPEG's coefficients may take while it's decoded (`_jpeg_coefficient_bytes`). A progressive
JPEG, or one whose components are in scans of their own, is decoded by keeping every DCT coefficient of the image (2
bytes a sample at full resolution, whatever scale `draft` chose), so a 3 MB progressive file of 16100 x 16100 pixels
would take 1.5 GB. This is what a 100-megapixel 4:4:4 one takes, the cap every image had before JPEGs got their own,
and what a 200-megapixel 4:2:0 phone photo saved progressive takes.
"""
MAX_JPEG_SCANS = 100
"""
The most scans a multi-scan JPEG may have. Its decoding goes over the coefficients of every scan's components once,
however little data the scan has, and libjpeg carries on through a progression that makes no sense: an 8 MB file of
300,000 empty scans takes six minutes to decode at 12 megapixels. libjpeg's own tools write at most 100; its default
progression has 10.
"""

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}

PIL_FORMATS: dict[str, list[str]] = {
    "jpeg": ["JPEG", "MPO"],
    "png": ["PNG"],
    "webp": ["WEBP"],
    "heif": ["HEIF", "AVIF"],
    "avif": ["AVIF", "HEIF"],
    "tiff": ["TIFF"],
}
"""Pillow's openers for each sniffed format. `Image.open` is never left to guess: it would also try EPS and PSD."""

_EXIF_TRANSPOSE = {
    # what `ImageOps.exif_transpose` does for each EXIF orientation
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}

_HIGH_BIT_MODES = ("I;16", "I", "F")
"""Grayscale modes with more than 8 bits, which `_eight_bit` scales rather than clips"""

_ROTATE_CLOCKWISE = {
    # Pillow's ROTATE_* turn counter-clockwise
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}

_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")
_SURROGATES = re.compile("[\ud800-\udfff]")
"""Lone surrogates (JSON escapes, a file name's undecodable bytes) aren't text a database or a log can take"""
MAX_FILENAME_LENGTH = 120


class PageRejected(Exception):
    """An image that can't become a card page, and why"""

    def __init__(self, reason: IngestRejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class RenderTimedOut(PageRejected):
    """A PDF that ran out of render time (`pdf_render_timeout()`, or its CPU time): `pdf_not_supported`"""

    def __init__(self) -> None:
        super().__init__(IngestRejectReason.pdf_not_supported)


class Region(NamedTuple):
    """Part of an upright page, in fractions of its width and height"""

    x: float
    y: float
    width: float
    height: float


class RegionLike(Protocol):
    @property
    def x(self) -> float: ...
    @property
    def y(self) -> float: ...
    @property
    def width(self) -> float: ...
    @property
    def height(self) -> float: ...


def sniff(head: bytes) -> str | None:
    """
    The format of a file, from its first `SNIFF_BYTES` bytes, never its name: `jpeg` (MPO included), `png`, `webp`,
    `heif`, `avif` or `tiff`; `pdf` for a PDF, whose pages `expand_document` renders; None for anything else.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _AVIF_BRANDS:
            return "avif"
        if brand in _HEIF_BRANDS:
            return "heif"
        return None
    if head.startswith(b"%PDF-"):
        return "pdf"
    return None


def sanitize_filename(name: str | None, *, max_length: int = MAX_FILENAME_LENGTH) -> str | None:
    """
    The base name of an uploaded file, with control characters and path separators removed, at most `max_length`
    characters (the extension kept); display only
    """
    if not name:
        return None
    base = re.split(r"[/\\]", name)[-1]
    base = _SURROGATES.sub("\ufffd", base)
    base = unicodedata.normalize("NFC", _UNSAFE_FILENAME_CHARS.sub("", base)).strip()
    if base in ("", ".", ".."):
        return None
    if len(base) > max_length:
        stem, dot, suffix = base.rpartition(".")
        if dot and len(suffix) <= 10:
            base = stem[: max_length - len(suffix) - 1] + "." + suffix
        else:
            base = base[:max_length]
    return base


class _Borrowed:
    """
    The caller's file, as Pillow reads it: closing an image (`_replace` does, to free its memory) closes its file
    whoever opened it, and the caller's file may have more pages to give (a multi-page TIFF) or be the caller's to
    close. Without `fileno` or `getvalue`, libtiff is handed the bytes, never the descriptor.
    """

    def __init__(self, file: BinaryIO) -> None:
        self._file = file

    def read(self, size: int = -1) -> bytes:
        return self._file.read(size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def close(self) -> None:
        pass


def _open_image(raw: BinaryIO, kind: str) -> Image.Image:
    """
    `raw` opened (its header only) with the sniffed format's own Pillow openers, in `PIL_FORMATS` order, as
    `Image.open(raw, formats=...)` does, but without its decompression-bomb check: that check warns above Pillow's
    `MAX_IMAGE_PIXELS` (about 89 megapixels) and refuses above twice that, before a JPEG's reduced-scale decoding is
    possible. The callers' own pixel caps apply instead, before anything is decoded, and no warning is raised (an error
    wherever warnings are errors). Changing Pillow's global limit, or its warning filters, would affect every thread.
    """
    raw.seek(0)
    prefix = raw.read(SNIFF_BYTES)
    Image.init()  # every opener registered (once)
    for name in PIL_FORMATS[kind]:
        opener = Image.OPEN.get(name)
        if opener is None:
            continue
        factory, accept = opener
        accepted = accept(prefix) if accept is not None else True
        if isinstance(accepted, str) or not accepted:
            continue  # a string is Pillow's reason for not taking it
        raw.seek(0)
        try:
            return factory(cast(BinaryIO, _Borrowed(raw)), "")
        except SyntaxError, IndexError, TypeError, struct.error:
            continue  # not this format after all: the next opener, as `Image.open` goes on
    raise UnidentifiedImageError(f"Not a {kind} image")


_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
"""Start-of-frame markers (DHT, JPG and DAC share their range)"""
_JPEG_PROGRESSIVE_SOF = frozenset({0xC2, 0xC6, 0xCA, 0xCE})
_JPEG_SOS = 0xDA
_JPEG_EOI = 0xD9
_JPEG_NO_SEGMENT = frozenset({0x01, *range(0xD0, 0xD9)})
"""Markers with no segment after them: TEM, RST0-7 and SOI"""
_JPEG_MAX_SEGMENTS = 4096
"""Segments read before the first scan: a header with more isn't read"""
_JPEG_MAX_SKIPPED = 64 * 1024
"""Stray bytes skipped between segments (libjpeg skips them too): a header with more isn't read"""


class _JpegFrame(NamedTuple):
    """A JPEG's first image as its markers describe it, up to its first scan"""

    width: int
    height: int
    sampling: tuple[tuple[int, int], ...]
    """Each component's horizontal and vertical sampling factors"""
    progressive: bool
    first_scan_components: int
    first_scan_data: int
    """Where the first scan's entropy-coded data starts"""

    @property
    def multi_scan(self) -> bool:
        """
        Decoded with all its coefficients kept (libjpeg's `has_multiple_scans`): progressive, or its first scan doesn't
        hold every component
        """
        return self.progressive or self.first_scan_components < len(self.sampling)


def _jpeg_marker(raw: BinaryIO) -> int | None:
    """The next marker's code, past stray bytes and fill bytes; None at the end of the file or after too many strays"""
    skipped = 0
    while True:
        byte = raw.read(1)
        while byte and byte != b"\xff":
            skipped += 1
            if skipped > _JPEG_MAX_SKIPPED:
                return None
            byte = raw.read(1)
        while byte == b"\xff":
            byte = raw.read(1)  # fill bytes
        if not byte:
            return None
        if byte != b"\x00":  # FF 00 is a stuffed data byte, not a marker
            return byte[0]
        skipped += 2


def _jpeg_frame(raw: BinaryIO) -> _JpegFrame | None:
    """
    The first image's frame header and its first scan's, from the markers before that scan (every other segment
    skipped by its length); None when they can't be read
    """
    raw.seek(0)
    if raw.read(2) != b"\xff\xd8":
        return None
    frame: tuple[int, int, tuple[tuple[int, int], ...], bool] | None = None
    for _ in range(_JPEG_MAX_SEGMENTS):
        marker = _jpeg_marker(raw)
        if marker is None or marker == _JPEG_EOI:
            return None
        if marker in _JPEG_NO_SEGMENT:
            continue
        length_bytes = raw.read(2)
        length = int.from_bytes(length_bytes, "big")
        if len(length_bytes) < 2 or length < 2:
            return None
        if marker == _JPEG_SOS:
            count = raw.read(1)
            if frame is None or not count:
                return None
            width, height, sampling, progressive = frame
            return _JpegFrame(width, height, sampling, progressive, count[0], raw.tell() + length - 3)
        if marker in _JPEG_SOF and frame is None:
            segment = raw.read(length - 2)
            if len(segment) < 6 or len(segment) < 6 + 3 * segment[5] or segment[5] < 1:
                return None
            height, width = int.from_bytes(segment[1:3], "big"), int.from_bytes(segment[3:5], "big")
            factors = segment[7 : 6 + 3 * segment[5] : 3]
            sampling = tuple((factor >> 4, factor & 0x0F) for factor in factors)
            if any(h < 1 or v < 1 for h, v in sampling):
                return None
            frame = (width, height, sampling, marker in _JPEG_PROGRESSIVE_SOF)
        else:
            raw.seek(length - 2, os.SEEK_CUR)
    return None


def _jpeg_coefficient_bytes(frame: _JpegFrame) -> int:
    """
    The memory libjpeg keeps for the image's DCT coefficients while decoding it (`MAX_JPEG_COEFFICIENT_BYTES`): none
    to speak of for a single-scan JPEG (decoded a row of blocks at a time), all of them for a multi-scan one: each
    component's blocks, rounded up to its sampling factors, of 64 coefficients of 2 bytes
    """
    if not frame.multi_scan:
        return 0
    h_max = max(h for h, _ in frame.sampling)
    v_max = max(v for _, v in frame.sampling)
    blocks = 0
    for h, v in frame.sampling:
        across = math.ceil(frame.width * h / (h_max * 8))
        down = math.ceil(frame.height * v / (v_max * 8))
        blocks += math.ceil(across / h) * h * math.ceil(down / v) * v
    return blocks * 64 * 2


def _jpeg_scans(raw: BinaryIO, frame: _JpegFrame, limit: int) -> int:
    """
    How many scans the image has, counted from its first to its end (`EOI`) and at most to `limit` + 1: each scan's
    entropy-coded data is passed over (stuffed bytes and restart markers), other segments by their lengths
    """
    raw.seek(frame.first_scan_data)
    data = raw.read()
    scans = 1
    position = 0
    while scans <= limit:
        position = data.find(b"\xff", position)
        if position < 0 or position + 1 >= len(data):
            break
        marker = data[position + 1]
        if marker == 0x00 or marker == 0xFF or marker in _JPEG_NO_SEGMENT:
            position += 1  # a stuffed byte, a fill byte or a restart marker
            continue
        if marker == _JPEG_EOI:
            break
        if marker == _JPEG_SOS:
            scans += 1
        position += 2 + int.from_bytes(data[position + 2 : position + 4], "big")  # the segment, then its data
    return scans


def _check_jpeg_decoding(raw: BinaryIO) -> None:
    """
    `PageRejected` for a JPEG whose decoding would cost too much, before anything is decoded: a multi-scan JPEG's
    coefficients over `MAX_JPEG_COEFFICIENT_BYTES` (`too_many_pixels`), or more than `MAX_JPEG_SCANS` scans
    (`unreadable_image`); a header that can't be read is `unreadable_image` too. Leaves `raw` where it was.
    """
    position = raw.tell()
    try:
        frame = _jpeg_frame(raw)
        if frame is None:
            raise PageRejected(IngestRejectReason.unreadable_image)
        if not frame.multi_scan:
            return
        if _jpeg_coefficient_bytes(frame) > MAX_JPEG_COEFFICIENT_BYTES:
            raise PageRejected(IngestRejectReason.too_many_pixels)
        if _jpeg_scans(raw, frame, MAX_JPEG_SCANS) > MAX_JPEG_SCANS:
            raise PageRejected(IngestRejectReason.unreadable_image)
    finally:
        raw.seek(position)


def _hash_stream(raw: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := raw.read(1024 * 1024):
        size += len(chunk)
        if size > limits.MAX_FILE_BYTES:
            raise PageRejected(IngestRejectReason.too_large)
        digest.update(chunk)
    return digest.hexdigest(), size


def _rgb_icc_profile(image: Image.Image) -> bytes | None:
    """The image's ICC profile if it describes RGB data, which the RGB pages can carry; None otherwise"""
    icc = image.info.get("icc_profile")
    if isinstance(icc, bytes) and len(icc) >= 20 and icc[16:20] == b"RGB ":
        return icc
    return None


def _draft_size(size: tuple[int, int], max_side: int) -> tuple[int, int]:
    """The size an image fits into with its long side at `max_side`, for a JPEG's reduced-scale decoding"""
    scale = max_side / max(size)
    return max(1, int(size[0] * scale)), max(1, int(size[1] * scale))


def _eight_bit(image: Image.Image) -> Image.Image:
    """
    High-bit-depth grayscale (`I;16`, `I` or `F`: 16-bit, 32-bit or float scans) scaled to 8 bits. Pillow's `convert`
    would clip it instead, which makes a 16-bit scan white and a float one black.
    """
    _, high = image.getextrema()
    if image.mode == "F" and high <= 1.0:
        scale = 255.0
    elif high <= 255:
        scale = 1.0
    elif high <= 65535:
        scale = 255 / 65535
    else:
        scale = 255 / high
    return image.point(lambda value: value * scale).convert("L")


def _page_rgb(image: Image.Image) -> Image.Image:
    """
    The decoded frame as RGB, its long side at most `PAGE_MAX_SIDE`, transparency flattened onto white. It's scaled
    down before anything else and flattened through a mask rather than RGBA copies, so a 100-megapixel upload needs at
    most one full-size copy besides its decoded pixels. `image` is consumed: changed in place, or closed once replaced.
    """
    if image.mode.startswith("I;16") and image.mode != "I;16":
        image = _replace(image, image.convert("I"))  # the byte-swapped variants can't be resized or scaled
    elif image.mode == "PA" or ("transparency" in image.info and image.mode not in _HIGH_BIT_MODES):
        image = _replace(image, image.convert("RGBA"))  # palette or colour-key transparency, as alpha
    elif image.mode in ("1", "P"):
        image = _replace(image, image.convert("L" if image.mode == "1" else "RGB"))  # else resized with NEAREST

    if image.mode in ("RGBA", "LA"):
        flat = Image.new("RGB", image.size, "white")
        flat.paste(image, mask=image)  # its alpha band is the mask
        image = _replace(image, flat)

    image.thumbnail((limits.PAGE_MAX_SIDE, limits.PAGE_MAX_SIDE), Image.Resampling.LANCZOS)
    if image.mode in _HIGH_BIT_MODES:
        image = _replace(image, _eight_bit(image))
    if image.mode != "RGB":
        image = _replace(image, image.convert("RGB"))
    return image


def _replace(old: Image.Image, new: Image.Image) -> Image.Image:
    """`new`, with `old`'s memory released at once rather than when the caller lets go of it"""
    if new is not old:
        old.close()
    return new


def _fit(image: Image.Image, max_side: int) -> Image.Image:
    """A copy whose long side is at most `max_side`, never upscaled"""
    copy = image.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return copy


def _jpeg_bytes(image: Image.Image, icc: bytes | None) -> bytes:
    buffer = io.BytesIO()
    params: dict = {"quality": limits.JPEG_QUALITY, "optimize": True}
    if icc:
        params["icc_profile"] = icc
    image.save(buffer, format="JPEG", **params)
    return buffer.getvalue()


class _PageFiles(NamedTuple):
    page_sha256: str
    page_size: tuple[int, int]
    view_size: tuple[int, int]


def _write_page_files(page_dir: Path, page: Image.Image, icc: bytes | None, *, staged: bool = False) -> _PageFiles:
    """
    Writes `page.jpg`, `view.jpg` and `thumb.webp` from an upright RGB image (`page.jpg` last: its hash is what says
    the page changed). `staged`: their staged names instead, `page.next.jpg` first, so a staging cut short always
    leaves a staged page that the stored metadata doesn't name (`recover_staged` then discards it).
    """
    page_image = _fit(page, limits.PAGE_MAX_SIDE)
    page_bytes = _jpeg_bytes(page_image, icc)
    view = _fit(page_image, limits.VIEW_MAX_SIDE)
    thumb = _fit(view, limits.THUMB_MAX_SIDE)

    def name(file: str) -> Path:
        return page_dir / (STAGED_FILES[file] if staged else file)

    if staged:
        atomic_write_bytes(name(PAGE_FILE), page_bytes)
    atomic_write_bytes(name(VIEW_FILE), _jpeg_bytes(view, icc))
    atomic_save_image(thumb, name(THUMB_FILE), "WEBP", quality=limits.THUMB_WEBP_QUALITY)
    if not staged:
        atomic_write_bytes(name(PAGE_FILE), page_bytes)
    return _PageFiles(hashlib.sha256(page_bytes).hexdigest(), page_image.size, view.size)


@dataclass(frozen=True)
class DocumentPage:
    """One card page an uploaded file holds (`expand_document`), ready for `normalize_document_page`"""

    file: BinaryIO
    """What's decoded: the upload itself, or a PDF page rendered as a PNG (`rendered`)"""
    kind: str
    """`file`'s sniffed format"""
    raw_sha256: str
    raw_bytes: int
    """
    The page's identity, stored as its `PageMeta.raw_sha256` and `raw_bytes`: the upload's SHA-256 and size; for one
    of several pages, the SHA-256 of the upload's and the page number (the size stays the upload's). So the same file
    sent again is found as a duplicate, whatever version of PDFium rendered it.
    """
    number: int | None = None
    """The page's number in the uploaded file, from 1, when the file holds several pages"""
    frame: int = 0
    """Which frame of `file` is the page (a multi-page TIFF's)"""
    format: str | None = None
    """The uploaded file's format when `file` isn't the upload (`pdf`)"""
    rendered: bool = False
    """`file` was made here: `close_pages` closes it"""


def close_pages(pages: Iterable[DocumentPage]) -> None:
    """Closes the files `expand_document` made (a PDF's rendered pages); the uploads stay with their callers"""
    for page in pages:
        if page.rendered:
            page.file.close()


def page_filename(name: str | None, number: int | None) -> str | None:
    """A page's display name: the uploaded file's, sanitized, with `(page 2)` for a page of a multi-page file"""
    if number is None:
        return sanitize_filename(name)
    suffix = f" (page {number})"
    base = sanitize_filename(name, max_length=MAX_FILENAME_LENGTH - len(suffix))
    return f"{base}{suffix}" if base else suffix.strip(" ()").capitalize()


def expand_document(raw: BinaryIO) -> list[DocumentPage]:
    """
    The card pages one uploaded file holds, in order: an image is one page; a multi-page TIFF gives one per frame
    (reduced-resolution copies and masks left out); a PDF one per page, rendered at a long side of `PAGE_MAX_SIDE`
    (within `MAX_PIXELS`) in a process of its own (`pdf_render`), so a hostile PDF can't crash or hang the server. The
    file is sniffed and hashed here (its identity is each page's `raw_sha256`); nothing is decoded but a PDF.

    `raw` is an open, seekable binary file, never reopened by path. Raises `PageRejected` with `unsupported_format`,
    `too_large`, `too_many_pages` (more than `MAX_PAGES_PER_CARD`), `pdf_not_supported` (encrypted, empty, damaged,
    not rendered within its time, a `RenderTimedOut` then, or on a system that can't confine the renderer without
    `AI_INGEST_PDF_UNCONFINED`) or `unreadable_image` (a TIFF whose frames can't be read). Blocking; `close_pages`
    closes the rendered files. Needs no write lock: nothing is written to `DATA_DIR`.
    """
    raw.seek(0)
    kind = sniff(raw.read(SNIFF_BYTES))
    if kind is None:
        raise PageRejected(IngestRejectReason.unsupported_format)
    raw.seek(0)
    raw_sha256, raw_bytes = _hash_stream(raw)
    raw.seek(0)

    if kind == "pdf":
        return _pdf_pages(raw, raw_sha256, raw_bytes)
    frames = _tiff_page_frames(raw) if kind == "tiff" else [0]
    if len(frames) == 1:
        return [DocumentPage(raw, kind, raw_sha256, raw_bytes, frame=frames[0])]
    return [
        DocumentPage(raw, kind, _page_identity(raw_sha256, number), raw_bytes, number=number, frame=frame)
        for number, frame in enumerate(frames, start=1)
    ]


def _page_identity(raw_sha256: str, number: int) -> str:
    return hashlib.sha256(f"{raw_sha256}:page:{number}".encode("ascii")).hexdigest()


_TIFF_SUBFILE_TYPE = 254
_TIFF_NOT_A_PAGE = 0b101
"""`NewSubfileType` bits of a frame that isn't a page of its own: a reduced-resolution copy (1), a mask (4)"""
MAX_TIFF_FRAMES = 32
"""Frames looked at in a TIFF: more, and it has too many pages for a card whatever they are"""


def _tiff_page_frames(raw: BinaryIO) -> list[int]:
    """The frames of a TIFF that are pages (its headers only are read)"""
    frames: list[int] = []
    try:
        with _open_image(raw, "tiff") as image:
            for frame in range(MAX_TIFF_FRAMES + 1):
                try:
                    image.seek(frame)
                except EOFError:
                    break
                if frame == MAX_TIFF_FRAMES:
                    raise PageRejected(IngestRejectReason.too_many_pages)
                subfile_type = getattr(image, "tag_v2", {}).get(_TIFF_SUBFILE_TYPE, 0)
                if frame == 0 or not (isinstance(subfile_type, int) and subfile_type & _TIFF_NOT_A_PAGE):
                    frames.append(frame)
                if len(frames) > limits.MAX_PAGES_PER_CARD:
                    raise PageRejected(IngestRejectReason.too_many_pages)
    except PageRejected:
        raise
    except Exception as e:
        raise PageRejected(IngestRejectReason.unreadable_image) from e
    finally:
        raw.seek(0)
    return frames


PDF_RENDER_WALL_FACTOR = 1.5
"""How long a PDF's pages may take to render in all, waiting for a CPU included, as a multiple of their CPU time"""


def pdf_render_cpu_seconds() -> int:
    """
    The CPU time a PDF's pages may take to render (the renderer's `RLIMIT_CPU`): `AI_INGEST_PDF_CPU_SECONDS`, 20 by
    default (a scanned card of 4 pages takes about 4 s on a current x86-64 core)
    """
    return get_ingest_settings().PDF_CPU_SECONDS


def pdf_render_timeout() -> float:
    """How long a PDF's pages may take to render in all, waiting for a CPU included"""
    return pdf_render_cpu_seconds() * PDF_RENDER_WALL_FACTOR


_PDF_RENDERER = Path(__file__).with_name("pdf_render.py")
_CHILD_ENVIRONMENT = ("SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
"""What the renderer's process gets of the server's environment: nothing secret"""
_PAGE_FRAME, _RESULT_FRAME, _FRAME_HEADER = b"P", b"R", 9
"""The renderer's stdout: frames of a kind byte and an 8-byte big-endian length (`pdf_render`)"""
MAX_RENDERED_PAGE_BYTES = 256 * 1024 * 1024
"""A larger page frame is a broken renderer (its own limit is the same)"""
MAX_RESULT_BYTES = 64 * 1024
_UNCONFINED = "unconfined"
"""The renderer's error where it couldn't be confined and rendered nothing (`AI_INGEST_PDF_UNCONFINED` is off)"""
_TIME_LIMIT_SIGNALS = (-signal.SIGKILL, -signal.SIGXCPU)
"""The exit statuses of a renderer stopped by its CPU time limit (`RLIMIT_CPU`, soft and hard alike)"""
RENDER_OUTPUT_GRACE = 5.0
"""Seconds the renderer's output may take to end once it's killed: what still holds it then is left behind"""
_sandbox_logged = False


class _RenderedFrames:
    """What the renderer wrote to stdout, read on a thread of its own: its pages, spooled, and its result"""

    def __init__(self) -> None:
        self.pages: list[SpooledTemporaryFile[bytes]] = []
        self.result: dict | None = None
        self.broken = False
        self.closed = False

    def read(self, stream: BinaryIO) -> None:
        """
        The frames up to the result, then `stream` closed: by this thread, as closing it from another while a read
        waits for the renderer would wait too
        """
        try:
            self._read(stream)
        finally:
            stream.close()

    def _read(self, stream: BinaryIO) -> None:
        try:
            while self.result is None and not self.closed:
                header = stream.read(_FRAME_HEADER)
                if len(header) < _FRAME_HEADER:
                    return  # it ended without a result: crashed, or killed
                kind, length = header[:1], int.from_bytes(header[1:], "big")
                if (
                    kind == _PAGE_FRAME
                    and length <= MAX_RENDERED_PAGE_BYTES
                    and len(self.pages) < limits.MAX_PAGES_PER_CARD
                ):
                    self._read_page(stream, length)
                elif kind == _RESULT_FRAME and length <= MAX_RESULT_BYTES:
                    self.result = self._read_result(stream, length)
                else:
                    self.broken = True  # an unknown frame, too large, or a page too many: nothing after it is read
                    return
        except OSError, ValueError, _BrokenFrame:
            self.broken = True

    def _read_page(self, stream: BinaryIO, length: int) -> None:
        page: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
        self.pages.append(page)
        left = length
        while left:
            chunk = stream.read(min(left, 1024 * 1024))
            if not chunk:
                raise _BrokenFrame()
            page.write(chunk)
            left -= len(chunk)
        page.seek(0)

    @staticmethod
    def _read_result(stream: BinaryIO, length: int) -> dict:
        data = stream.read(length)
        if len(data) < length:
            raise _BrokenFrame()
        result = json.loads(data)
        if not isinstance(result, dict):
            raise _BrokenFrame()
        return result

    def close(self) -> None:
        self.closed = True  # a reader left behind (`_run_renderer`) stops at its next frame
        for page in self.pages:
            page.close()


class _BrokenFrame(Exception):
    pass


def _log_sandbox(result: dict) -> None:
    """Once per process: how the renderer was confined (`pdf_render.sandbox`), or why it rendered nothing"""
    global _sandbox_logged
    if _sandbox_logged:
        return
    _sandbox_logged = True
    protections = result.get("sandbox")
    applied = [str(item) for item in protections] if isinstance(protections, list) else []
    listed = ", ".join(applied) or "time and memory limits only"
    if result.get("error") == _UNCONFINED:
        logger.error(
            "PDFs are refused (pdf_not_supported): this system can't confine the PDF renderer (its seccomp filter, "
            f"for Linux on x86-64 or arm64, didn't apply; it has {listed}). AI_INGEST_PDF_UNCONFINED=true renders "
            "them anyway, with those protections only."
        )
    elif "seccomp" not in applied:
        logger.warning(
            "PDF pages are rendered in a process this system can't confine (no seccomp filter: it could open network "
            f"sockets and reach the server's process), as AI_INGEST_PDF_UNCONFINED allows: {listed}"
        )
    elif "seccomp-files" in applied:
        logger.info(
            "PDF pages are rendered in a confined process, without Landlock: it can open no file, so a PDF's fonts "
            f"that aren't in it are drawn with PDFium's own: {listed}"
        )
    else:
        logger.info(f"PDF pages are rendered in a confined process: {listed}")


def _pdf_pages(raw: BinaryIO, raw_sha256: str, raw_bytes: int) -> list[DocumentPage]:
    """
    A PDF's pages rendered as PNG images by `pdf_render` in a confined child process, which writes them to its stdout;
    read from there into unnamed temporary files
    """
    if not sys.executable:
        logger.error("Couldn't start the PDF renderer: the Python interpreter's path is unknown")
        raise PageRejected(IngestRejectReason.pdf_not_supported)
    with tempfile.TemporaryDirectory(prefix="mealie-pdf-") as work:
        document = Path(work) / "document.pdf"
        with document.open("wb") as copy:
            shutil.copyfileobj(raw, copy)
        raw.seek(0)
        frames = _run_renderer(document)

    try:
        result = frames.result or {}
        _log_sandbox(result)
        if result.get("error") == IngestRejectReason.too_many_pages.value:
            raise PageRejected(IngestRejectReason.too_many_pages)
        count = result.get("pages")
        if not isinstance(count, int) or not 1 <= count <= limits.MAX_PAGES_PER_CARD or count != len(frames.pages):
            raise PageRejected(IngestRejectReason.pdf_not_supported)
        return [
            DocumentPage(
                rendered,  # type: ignore[arg-type]
                "png",
                raw_sha256 if count == 1 else _page_identity(raw_sha256, number),
                raw_bytes,
                number=None if count == 1 else number,
                format="pdf",
                rendered=True,
            )
            for number, rendered in enumerate(frames.pages, start=1)
        ]
    except BaseException:
        frames.close()
        raise


def _run_renderer(document: Path) -> _RenderedFrames:
    """
    The renderer's run on `document` within `pdf_render_timeout()` and `pdf_render_cpu_seconds()`; `RenderTimedOut`
    when it ran out of either, `PageRejected` when it failed
    """
    cpu_seconds, timeout = pdf_render_cpu_seconds(), pdf_render_timeout()
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(  # the renderer's path and our own numbers: no shell, no user input
            [
                sys.executable,
                "-I",
                str(_PDF_RENDERER),
                str(document),
                str(limits.PAGE_MAX_SIDE),
                str(limits.MAX_PIXELS),
                str(limits.MAX_PAGES_PER_CARD),
                str(cpu_seconds),
                "1" if get_ingest_settings().PDF_UNCONFINED else "0",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={name: os.environ[name] for name in _CHILD_ENVIRONMENT if name in os.environ},
            close_fds=True,
            start_new_session=True,  # a process group of its own, killed with what it started (`_stop_renderer`)
        )
    except OSError as e:
        logger.error(f"Couldn't start the PDF renderer: {e}")
        raise PageRejected(IngestRejectReason.pdf_not_supported) from e

    frames = _RenderedFrames()
    assert process.stdout is not None
    # the reader closes the renderer's stdout once it's done with it
    reader = threading.Thread(target=frames.read, args=(process.stdout,), name="pdf-render-output", daemon=True)
    reader.start()
    timed_out = False
    try:
        reader.join(max(0.0, deadline - time.monotonic()))
        if reader.is_alive():  # still rendering at the deadline
            timed_out = True
        elif not frames.broken:
            try:
                process.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out or frames.broken:
            _stop_renderer(process)
            reader.join(RENDER_OUTPUT_GRACE)  # its output ends with it
            if reader.is_alive():
                # a process it started that left its group still holds the output: the reader is left to end with
                # it, and the render slot isn't held for it
                logger.warning("A process the PDF renderer started outlived it; it was left behind")
        returncode = process.wait()
    except BaseException:
        _stop_renderer(process)  # this thread was interrupted: the renderer doesn't outlive it
        raise

    if timed_out or (not frames.broken and returncode in _TIME_LIMIT_SIGNALS):
        frames.close()
        logger.info(
            f"A PDF wasn't rendered within {timeout:g} seconds or {cpu_seconds} seconds of CPU time "
            "(AI_INGEST_PDF_CPU_SECONDS)"
        )
        raise RenderTimedOut()
    if frames.result is None or frames.broken or returncode != 0:
        # killed by a resource limit, or PDFium crashed: logged without the document's content
        logger.info(f"The PDF renderer failed (exit status {returncode})")
        frames.close()
        raise PageRejected(IngestRejectReason.pdf_not_supported)
    return frames


def _stop_renderer(process: subprocess.Popen[bytes]) -> None:
    """
    Kills the renderer and every process it started that stayed in its process group (it leads one of its own): one
    left running would keep its stdout open. Its seccomp filter refuses it new processes; this is for where it has none.
    """
    if process.returncode is None and hasattr(os, "killpg"):  # not reaped yet: its pid is still its group's
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    process.kill()


def normalize_page(raw: BinaryIO, page_dir: Path, index: int, *, original_filename: str | None) -> PageMeta:
    """
    Turns one uploaded image into page `index` in `page_dir` (which must exist): an upright RGB `page.jpg` (long side
    at most 4096, quality 90, the RGB ICC profile kept, no EXIF, XMP or GPS), `view.jpg` (2048) and `thumb.webp`
    (480, aspect kept), each written atomically.

    `raw` is an open, seekable binary file: a multipart part's spooled file, a `BytesIO` or the inbox's file object.
    It's read from the start and never reopened by path. A multi-page TIFF gives its first page; a PDF is refused
    (`expand_document` turns either into pages for `normalize_document_page`).

    Raises `PageRejected` with `too_large`, `unsupported_format`, `pdf_not_supported`, `too_many_pixels` or
    `unreadable_image`. Callers hold `storage.ingest_write()`.
    """
    raw.seek(0)
    kind = sniff(raw.read(SNIFF_BYTES))
    if kind == "pdf":
        raise PageRejected(IngestRejectReason.pdf_not_supported)
    if kind is None:
        raise PageRejected(IngestRejectReason.unsupported_format)

    raw.seek(0)
    raw_sha256, raw_bytes = _hash_stream(raw)
    raw.seek(0)
    page = DocumentPage(raw, kind, raw_sha256, raw_bytes)
    return normalize_document_page(page, page_dir, index, original_filename=original_filename)


def normalize_document_page(
    page: DocumentPage, page_dir: Path, index: int, *, original_filename: str | None
) -> PageMeta:
    """
    `normalize_page` for one page `expand_document` found: its frame of the file, its own identity, and the uploaded
    file's format. `original_filename` is the page's display name (`page_filename`). Raises `PageRejected` with
    `too_many_pixels` or `unreadable_image`. Callers hold `storage.ingest_write()`.
    """
    raw = page.file
    kind = page.kind
    raw.seek(0)
    try:
        with _open_image(raw, kind) as image:
            if page.frame or getattr(image, "n_frames", 1) > 1:
                image.seek(page.frame)  # MPO: the first frame; TIFF: the page's
            if kind == "jpeg":
                if image.width * image.height > MAX_JPEG_SOURCE_PIXELS:
                    raise PageRejected(IngestRejectReason.too_many_pixels)
                _check_jpeg_decoding(raw)  # a multi-scan JPEG: all its coefficients held, and gone over each scan
                # decoded at 1/2, 1/4 or 1/8 scale when the page is that much smaller: never below the page's size
                image.draft(None, _draft_size(image.size, limits.PAGE_MAX_SIDE))
            if image.width * image.height > limits.MAX_PIXELS:  # a JPEG's as it will be decoded
                raise PageRejected(IngestRejectReason.too_many_pixels)
            image.load()  # what finds a truncated file
            format_name = page.format or (image.format or kind).lower()
            icc = _rgb_icc_profile(image)
            transpose = _EXIF_TRANSPOSE.get(image.getexif().get(ExifTags.Base.Orientation, 1))
            rgb = _page_rgb(image)
        if transpose is not None:
            rgb = _replace(rgb, rgb.transpose(transpose))  # upright by its EXIF orientation
        rgb.info = {}  # nothing of the original's metadata may reach the files
    except PageRejected:
        raise
    except Image.DecompressionBombError as e:
        raise PageRejected(IngestRejectReason.too_many_pixels) from e
    except Exception as e:
        # anything a damaged or hostile file makes the decoders raise (OSError for a truncated file, SyntaxError,
        # struct.error or ValueError for broken headers and EXIF): never a server error
        raise PageRejected(IngestRejectReason.unreadable_image) from e
    finally:
        raw.seek(0)

    files = _write_page_files(page_dir, rgb, icc)
    return PageMeta(
        index=index,
        width=files.page_size[0],
        height=files.page_size[1],
        view_width=files.view_size[0],
        view_height=files.view_size[1],
        rotation=0,
        rotation_source=PageRotationSource.none,
        oriented=False,
        raw_sha256=page.raw_sha256,
        page_sha256=files.page_sha256,
        original_filename=sanitize_filename(original_filename),
        format=format_name,
        raw_bytes=page.raw_bytes,
        ocr=None,
    )


def stage_rotation(page_dir: Path, meta: PageMeta, degrees_clockwise: int, source: PageRotationSource) -> PageMeta:
    """
    Writes the page turned clockwise by 90, 180 or 270 degrees under the staged names (`page.next.jpg`, then
    `view.next.jpg` and `thumb.next.webp`, each atomically), leaving the current files as they are. Returns the turned
    page's metadata: its size, its total rotation, `oriented` set (its orientation has now been decided), its OCR text
    cleared (read at the old orientation) and `page_sha256` of `page.next.jpg`. Store it, then `apply_staged`; or
    `discard_staged`. `meta` is the page's stored metadata: a turn an earlier crash left staged is settled against it
    first (`recover_staged`), so it's never overwritten unapplied. Callers hold `storage.ingest_write()`.
    """
    if degrees_clockwise not in _ROTATE_CLOCKWISE:
        raise ValueError("A page can only be turned by 90, 180 or 270 degrees")

    recover_staged(page_dir, meta)
    with Image.open(page_dir / PAGE_FILE, formats=["JPEG"]) as image:
        image.load()
        icc = _rgb_icc_profile(image)
        turned = image.convert("RGB").transpose(_ROTATE_CLOCKWISE[degrees_clockwise])
    turned.info = {}

    try:
        files = _write_page_files(page_dir, turned, icc, staged=True)
    except BaseException:
        discard_staged(page_dir)
        raise
    return meta.model_copy(
        update={
            "width": files.page_size[0],
            "height": files.page_size[1],
            "view_width": files.view_size[0],
            "view_height": files.view_size[1],
            "rotation": (meta.rotation + degrees_clockwise) % 360,
            "rotation_source": source,
            "oriented": True,
            "page_sha256": files.page_sha256,
            "ocr": None,
        }
    )


def has_staged(page_dir: Path) -> bool:
    """Whether any staged page file is waiting in the page's directory"""
    return any((page_dir / staged).exists() for staged in STAGED_FILES.values())


def apply_staged(page_dir: Path) -> None:
    """
    Moves the staged files over the current ones with `os.replace`: the thumbnail, the view, then `page.jpg` last. A
    staged file already moved (a swap a crash cut short) is skipped. Callers hold `storage.ingest_write()`.
    """
    for name in SWAP_ORDER:
        try:
            os.replace(page_dir / STAGED_FILES[name], page_dir / name)
        except FileNotFoundError:
            pass


def discard_staged(page_dir: Path) -> None:
    """Removes the staged files, `page.next.jpg` last. Callers hold `storage.ingest_write()`."""
    for name in SWAP_ORDER:
        (page_dir / STAGED_FILES[name]).unlink(missing_ok=True)


def _file_sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def recover_staged(page_dir: Path, stored_meta: PageMeta) -> Literal["none", "applied", "discarded"]:
    """
    What a crash left between staging a turn and swapping it in, settled against the page's stored metadata: when any
    staged file exists, the swap is finished if the staged page (or `page.jpg`, once the staged page has been moved)
    is the one `stored_meta.page_sha256` names, since that metadata was stored; otherwise the staged files are
    discarded, since it wasn't. Callers hold `storage.ingest_write()`.
    """
    if not has_staged(page_dir):
        return "none"
    staged_page = page_dir / STAGED_FILES[PAGE_FILE]
    page = staged_page if staged_page.exists() else page_dir / PAGE_FILE
    if _file_sha256(page) == stored_meta.page_sha256:
        apply_staged(page_dir)
        return "applied"
    discard_staged(page_dir)
    return "discarded"


def rotate_page_files(page_dir: Path, meta: PageMeta, degrees_clockwise: int, source: PageRotationSource) -> PageMeta:
    """
    Turns a page clockwise by 90, 180 or 270 degrees at once, for a caller without stored metadata to commit in
    between (`stage_rotation`, then `apply_staged`): returns the page's new metadata. Callers hold
    `storage.ingest_write()`.
    """
    turned = stage_rotation(page_dir, meta, degrees_clockwise, source)
    apply_staged(page_dir)
    return turned


def crop_region(page_path: Path, region: RegionLike, *, margin: float = limits.REREAD_MARGIN) -> bytes:
    """
    A JPEG of one region of an upright page, built in memory: the region plus `margin` of the page on each side
    (clamped to the page), upscaled with LANCZOS to a long side of `REREAD_MIN_SIDE` when smaller (at most
    `REREAD_MAX_UPSCALE` times). Nothing is written to disk.
    """
    with Image.open(page_path, formats=["JPEG"]) as image:
        image.load()
        width, height = image.size
        left = max(0.0, region.x - margin) * width
        top = max(0.0, region.y - margin) * height
        right = min(1.0, region.x + region.width + margin) * width
        bottom = min(1.0, region.y + region.height + margin) * height
        box = (int(left), int(top), max(int(left) + 1, round(right)), max(int(top) + 1, round(bottom)))
        crop = image.convert("RGB").crop(box)

    long_side = max(crop.size)
    if long_side < limits.REREAD_MIN_SIDE:
        scale = min(limits.REREAD_MIN_SIDE / long_side, float(limits.REREAD_MAX_UPSCALE))
        size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
        crop = crop.resize(size, Image.Resampling.LANCZOS)

    crop.info = {}
    return _jpeg_bytes(crop, None)


def page_file(page_dir: Path, kind: str) -> Path:
    """`page`, `view` or `thumb` to its file in a page's directory"""
    names = {"page": PAGE_FILE, "view": VIEW_FILE, "thumb": THUMB_FILE}
    if kind not in names:
        raise ValueError(f"Unknown page image: {kind}")
    return page_dir / names[kind]
