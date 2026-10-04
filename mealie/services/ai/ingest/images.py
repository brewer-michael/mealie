"""
Turning an uploaded photo into a card page, and the page operations after that (docs/ai/PHASE2.md §2, §4.4, §4.7).

`normalize_page` runs inside the upload request (or the inbox scan), so the uploaded bytes, GPS included, never
outlive it: what's stored is an upright, metadata-free JPEG plus two smaller copies. One Pillow path covers every
accepted format and enforces the pixel cap before anything is decoded.
"""

import hashlib
import io
import re
import unicodedata
from pathlib import Path
from typing import BinaryIO, NamedTuple, Protocol

from PIL import ExifTags, Image

import mealie.pkgs.img  # noqa: F401  (registers the HEIF opener)
from mealie.schema.recipe_ingest import IngestRejectReason, PageMeta, PageRotationSource

from . import limits
from .storage import atomic_save_image, atomic_write_bytes

PAGE_FILE = "page.jpg"
VIEW_FILE = "view.jpg"
THUMB_FILE = "thumb.webp"

SNIFF_BYTES = 16
"""How much of a file `sniff` needs"""

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
    The image format of a file, from its first `SNIFF_BYTES` bytes, never its name: `jpeg` (MPO included), `png`,
    `webp`, `heif`, `avif` or `tiff`; `pdf` for a PDF, which is refused with its own reason; None for anything else.
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


def sanitize_filename(name: str | None) -> str | None:
    """The base name of an uploaded file, with control characters and path separators removed; display only"""
    if not name:
        return None
    base = re.split(r"[/\\]", name)[-1]
    base = _SURROGATES.sub("\ufffd", base)
    base = unicodedata.normalize("NFC", _UNSAFE_FILENAME_CHARS.sub("", base)).strip()
    if base in ("", ".", ".."):
        return None
    if len(base) > MAX_FILENAME_LENGTH:
        stem, dot, suffix = base.rpartition(".")
        if dot and len(suffix) <= 10:
            base = stem[: MAX_FILENAME_LENGTH - len(suffix) - 1] + "." + suffix
        else:
            base = base[:MAX_FILENAME_LENGTH]
    return base


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


def _write_page_files(page_dir: Path, page: Image.Image, icc: bytes | None) -> _PageFiles:
    """Writes `page.jpg`, `view.jpg` and `thumb.webp` from an upright RGB image"""
    page_image = _fit(page, limits.PAGE_MAX_SIDE)
    page_bytes = _jpeg_bytes(page_image, icc)

    view = _fit(page_image, limits.VIEW_MAX_SIDE)
    atomic_write_bytes(page_dir / VIEW_FILE, _jpeg_bytes(view, icc))

    thumb = _fit(view, limits.THUMB_MAX_SIDE)
    atomic_save_image(thumb, page_dir / THUMB_FILE, "WEBP", quality=limits.THUMB_WEBP_QUALITY)

    # page.jpg last: its hash is what says the page changed
    atomic_write_bytes(page_dir / PAGE_FILE, page_bytes)
    return _PageFiles(hashlib.sha256(page_bytes).hexdigest(), page_image.size, view.size)


def normalize_page(raw: BinaryIO, page_dir: Path, index: int, *, original_filename: str | None) -> PageMeta:
    """
    Turns one uploaded image into page `index` in `page_dir` (which must exist): an upright RGB `page.jpg` (long side
    at most 4096, quality 90, the RGB ICC profile kept, no EXIF, XMP or GPS), `view.jpg` (2048) and `thumb.webp`
    (480, aspect kept), each written atomically.

    `raw` is an open, seekable binary file: a multipart part's spooled file, a `BytesIO` or the inbox's file object.
    It's read from the start and never reopened by path.

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

    try:
        with Image.open(raw, formats=PIL_FORMATS[kind]) as image:
            if image.width * image.height > limits.MAX_PIXELS:
                raise PageRejected(IngestRejectReason.too_many_pixels)
            if getattr(image, "n_frames", 1) > 1:
                image.seek(0)  # MPO and TIFF: the first frame only
            if kind == "jpeg":
                # decoded at 1/2, 1/4 or 1/8 scale when the page is that much smaller: never below the page's size
                image.draft(None, _draft_size(image.size, limits.PAGE_MAX_SIDE))
            image.load()  # what finds a truncated file
            format_name = (image.format or kind).lower()
            icc = _rgb_icc_profile(image)
            transpose = _EXIF_TRANSPOSE.get(image.getexif().get(ExifTags.Base.Orientation, 1))
            page = _page_rgb(image)
        if transpose is not None:
            page = _replace(page, page.transpose(transpose))  # upright by its EXIF orientation
        page.info = {}  # nothing of the original's metadata may reach the files
    except PageRejected:
        raise
    except Image.DecompressionBombError as e:
        raise PageRejected(IngestRejectReason.too_many_pixels) from e
    except Exception as e:
        # anything a damaged or hostile file makes the decoders raise (OSError for a truncated file, SyntaxError,
        # struct.error or ValueError for broken headers and EXIF): never a server error
        raise PageRejected(IngestRejectReason.unreadable_image) from e

    files = _write_page_files(page_dir, page, icc)
    return PageMeta(
        index=index,
        width=files.page_size[0],
        height=files.page_size[1],
        view_width=files.view_size[0],
        view_height=files.view_size[1],
        rotation=0,
        rotation_source=PageRotationSource.none,
        oriented=False,
        raw_sha256=raw_sha256,
        page_sha256=files.page_sha256,
        original_filename=sanitize_filename(original_filename),
        format=format_name,
        raw_bytes=raw_bytes,
        ocr=None,
    )


def rotate_page_files(page_dir: Path, meta: PageMeta, degrees_clockwise: int, source: PageRotationSource) -> PageMeta:
    """
    Turns a page clockwise by 90, 180 or 270 degrees: rewrites `page.jpg` and regenerates `view.jpg` and the thumbnail
    from it. Returns the page's new metadata: its size, its total rotation, `oriented` set (its orientation has now
    been decided) and its OCR text cleared (read at the old orientation). Callers hold `storage.ingest_write()`.
    """
    if degrees_clockwise not in _ROTATE_CLOCKWISE:
        raise ValueError("A page can only be turned by 90, 180 or 270 degrees")

    with Image.open(page_dir / PAGE_FILE, formats=["JPEG"]) as image:
        image.load()
        icc = _rgb_icc_profile(image)
        turned = image.convert("RGB").transpose(_ROTATE_CLOCKWISE[degrees_clockwise])
    turned.info = {}

    files = _write_page_files(page_dir, turned, icc)
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
