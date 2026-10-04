"""Turning uploads into card pages, rotating them and cropping regions (docs/ai/PHASE2.md §2, §4.4, §4.7)"""

import hashlib
import io
import json
import math
import os
import struct
import tempfile
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import BinaryIO

import pytest
from PIL import Image, ImageCms, ImageFile

from mealie.schema.recipe_ingest import IngestRejectReason, PageMeta, PageRotationSource
from mealie.services.ai.ingest import images, limits
from mealie.services.ai.ingest.images import (
    PageRejected,
    Region,
    apply_staged,
    crop_region,
    discard_staged,
    normalize_page,
    recover_staged,
    rotate_page_files,
    sniff,
    stage_rotation,
)

RED = (255, 0, 0)
WHITE = (255, 255, 255)
ORIENTATION = 0x0112
GPS_IFD = 0x8825


@pytest.fixture()
def page_dir(tmp_path: Path) -> Path:
    path = tmp_path / "pages" / "0"
    path.mkdir(parents=True)
    return path


def _left_third_red(width: int, height: int) -> Image.Image:
    """White with its left third red, so the way up can be told after a turn"""
    image = Image.new("RGB", (width, height), WHITE)
    image.paste(RED, (0, 0, width // 3, height))
    return image


def _exif(orientation: int | None = None, gps: bool = True) -> Image.Exif:
    exif = Image.Exif()
    if orientation:
        exif[ORIENTATION] = orientation
    if gps:
        exif[GPS_IFD] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    return exif


def _encoded(image: Image.Image, format: str, **params) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=format, **params)
    return buffer.getvalue()


@cache
def _srgb_profile() -> bytes:
    # built once: the profile's header records when it was made, to the second, so two builds can differ
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _close(actual: tuple, expected: tuple, tolerance: int = 40) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


def _assert_no_metadata(page_dir: Path) -> None:
    for name in (images.PAGE_FILE, images.VIEW_FILE, images.THUMB_FILE):
        data = (page_dir / name).read_bytes()
        assert b"Exif" not in data, name
        assert b"GPS" not in data, name
        assert b"http://ns.adobe.com/xap" not in data, name  # XMP
        with Image.open(page_dir / name) as image:
            assert not image.getexif(), name
            assert "exif" not in image.info, name


@contextmanager
def _spooled(data: bytes) -> Iterator[BinaryIO]:
    # what Starlette's multipart parser hands over: no path, and positioned at the start
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spooled:
        spooled.write(data)
        spooled.seek(0)
        yield spooled  # type: ignore[misc]


# ==========================================
# sniffing


@pytest.mark.parametrize(
    "head, kind",
    [
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00", "jpeg"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00", "tiff"),
        (b"MM\x00*\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00", "tiff"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "heif"),
        (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00", "heif"),
        (b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00", "avif"),
        (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", None),  # a video
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "pdf"),
        (b"GIF89a\x01\x00\x01\x00\x00\x00\x00", None),
        (b"", None),
    ],
)
def test_formats_are_recognised_by_their_magic_bytes(head: bytes, kind: str | None):
    assert sniff(head) == kind


@pytest.mark.parametrize(
    "name, sanitized",
    [
        ("IMG_0007.HEIC", "IMG_0007.HEIC"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\card.jpg", "card.jpg"),
        ("bad\x00name\x1f.jpg", "badname.jpg"),
        ("..", None),
        ("", None),
        (None, None),
        ("x" * 300 + ".jpeg", "x" * 115 + ".jpeg"),
        ("\ud800card.jpg", "\ufffdcard.jpg"),  # a lone surrogate from JSON can't be stored
        ("r\udce9cipe.jpg", "r\ufffdcipe.jpg"),  # nor a file name's undecodable byte
    ],
)
def test_file_names_are_sanitized_for_display(name: str | None, sanitized: str | None):
    assert images.sanitize_filename(name) == sanitized


# ==========================================
# normalize_page


def test_a_heic_with_orientation_6_and_gps_comes_out_upright_with_no_metadata(page_dir: Path):
    pytest.importorskip("pillow_heif")
    raw = _encoded(_left_third_red(600, 300), "HEIF", exif=_exif(orientation=6).tobytes(), quality=90)
    assert raw[4:12] == b"ftypheic"

    with _spooled(raw) as upload:
        meta = normalize_page(upload, page_dir, 0, original_filename="IMG_0007.HEIC")

    # turned a quarter clockwise: the red left third is now the top third
    assert (meta.width, meta.height) == (300, 600)
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert page.format == "JPEG"
        assert page.size == (300, 600)
        assert _close(page.getpixel((150, 50)), RED)
        assert _close(page.getpixel((150, 550)), WHITE)
    _assert_no_metadata(page_dir)

    assert meta.format == "heif"
    assert meta.original_filename == "IMG_0007.HEIC"
    assert meta.raw_bytes == len(raw)
    assert meta.rotation == 0 and meta.rotation_source == PageRotationSource.none and not meta.oriented
    assert meta.ocr is None


def test_a_jpeg_from_a_bytes_buffer_is_turned_by_its_exif_and_keeps_its_rgb_profile(page_dir: Path):
    raw = _encoded(_left_third_red(600, 300), "JPEG", exif=_exif(orientation=6), icc_profile=_srgb_profile())

    meta = normalize_page(io.BytesIO(raw), page_dir, 2, original_filename="card.jpg")

    assert meta.index == 2
    assert (meta.width, meta.height) == (300, 600)
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert _close(page.getpixel((150, 50)), RED)
        assert page.info.get("icc_profile") == _srgb_profile()
    with Image.open(page_dir / images.VIEW_FILE) as view:
        assert view.info.get("icc_profile") == _srgb_profile()
    _assert_no_metadata(page_dir)


def test_hashes_describe_the_upload_and_the_stored_page(page_dir: Path):
    import hashlib

    raw = _encoded(_left_third_red(90, 60), "PNG")
    meta = normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)

    assert meta.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert meta.page_sha256 == hashlib.sha256((page_dir / images.PAGE_FILE).read_bytes()).hexdigest()
    assert meta.original_filename is None
    assert meta.format == "png"


def test_an_mpo_is_read_from_its_first_frame(page_dir: Path):
    raw = _encoded(
        Image.new("RGB", (80, 40), RED), "MPO", save_all=True, append_images=[Image.new("RGB", (80, 40), "blue")]
    )
    with Image.open(io.BytesIO(raw)) as check:
        assert check.format == "MPO" and check.n_frames == 2

    meta = normalize_page(io.BytesIO(raw), page_dir, 0, original_filename="IMG_2503.jpg")

    assert meta.format == "mpo"
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert _close(page.getpixel((40, 20)), RED)
        assert getattr(page, "n_frames", 1) == 1
    _assert_no_metadata(page_dir)


def test_normalize_page_alone_reads_a_multi_page_tiffs_first_page(page_dir: Path):
    # expand_document gives each page (below)
    raw = _encoded(
        Image.new("RGB", (50, 50), RED), "TIFF", save_all=True, append_images=[Image.new("RGB", (50, 50), "blue")]
    )
    normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)

    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert _close(page.getpixel((25, 25)), RED)


def test_transparency_is_flattened_onto_white_and_the_thumbnail_keeps_the_aspect(page_dir: Path):
    image = Image.new("RGBA", (1000, 500), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, 100, 500))
    meta = normalize_page(io.BytesIO(_encoded(image, "WEBP", lossless=True)), page_dir, 0, original_filename=None)

    assert (meta.width, meta.height) == (1000, 500)
    assert (meta.view_width, meta.view_height) == (1000, 500)  # never upscaled
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert page.mode == "RGB"
        assert _close(page.getpixel((50, 250)), RED)
        assert _close(page.getpixel((600, 250)), WHITE)
    with Image.open(page_dir / images.THUMB_FILE) as thumb:
        assert thumb.format == "WEBP"
        assert thumb.size == (480, 240)


def _gradient_16(width: int = 256, height: int = 64) -> list[int]:
    """A left-to-right ramp over the whole 16-bit range, as a flatbed scanner's 16-bit grayscale writes it"""
    return [round(x * 65535 / (width - 1)) for _ in range(height) for x in range(width)]


def _big_endian_tiff(width: int, height: int, values: list[int]) -> bytes:
    """A 16-bit grayscale TIFF in Motorola byte order (Pillow reads it as `I;16B`; it can't write one)"""
    data = struct.pack(f">{len(values)}H", *values)
    tags = [(256, width), (257, height), (258, 16), (259, 1), (262, 1), (273, 8), (277, 1), (278, height)]
    tags.append((279, len(data)))
    ifd = struct.pack(">H", len(tags))
    for tag, value in tags:
        ifd += struct.pack(">HHII", tag, 4, 1, value)  # every value as a LONG
    return b"MM\x00*" + struct.pack(">I", 8 + len(data)) + data + ifd + struct.pack(">I", 0)


def _high_bit_scan(kind: str) -> bytes:
    width, height = 256, 64
    values = _gradient_16(width, height)
    if kind == "I;16 PNG":
        return _encoded(Image.frombytes("I;16", (width, height), struct.pack(f"<{len(values)}H", *values)), "PNG")
    if kind == "I;16 TIFF":
        return _encoded(Image.frombytes("I;16", (width, height), struct.pack(f"<{len(values)}H", *values)), "TIFF")
    if kind == "I;16B TIFF":
        return _big_endian_tiff(width, height, values)
    if kind == "I TIFF":
        return _encoded(Image.frombytes("I", (width, height), struct.pack(f"<{len(values)}i", *values)), "TIFF")
    assert kind == "F TIFF"
    floats = struct.pack(f"<{len(values)}f", *(value / 65535 for value in values))
    return _encoded(Image.frombytes("F", (width, height), floats), "TIFF")


@pytest.mark.parametrize("kind", ["I;16 PNG", "I;16 TIFF", "I;16B TIFF", "I TIFF", "F TIFF"])
def test_high_bit_depth_grayscale_scans_keep_their_tones(page_dir: Path, kind: str):
    # converted straight to RGB, 16-bit values are clipped to white and floats to black
    normalize_page(io.BytesIO(_high_bit_scan(kind)), page_dir, 0, original_filename="scan")

    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert page.mode == "RGB"
        left, middle, right = (page.getpixel((x, 32)) for x in (2, 128, 253))
    assert all(value < 20 for value in left), left
    assert all(100 < value < 155 for value in middle), middle
    assert all(value > 235 for value in right), right


def test_palette_and_bilevel_images_become_rgb_pages(page_dir: Path):
    palette = Image.new("P", (300, 100))
    palette.putpalette([255, 255, 255, 255, 0, 0])
    palette.paste(1, (0, 0, 100, 100))
    normalize_page(io.BytesIO(_encoded(palette, "PNG")), page_dir, 0, original_filename=None)
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert _close(page.getpixel((50, 50)), RED) and _close(page.getpixel((250, 50)), WHITE)

    bilevel = Image.new("1", (300, 100), 1)
    bilevel.paste(0, (0, 0, 100, 100))
    normalize_page(io.BytesIO(_encoded(bilevel, "TIFF")), page_dir, 0, original_filename=None)
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert _close(page.getpixel((50, 50)), (0, 0, 0)) and _close(page.getpixel((250, 50)), WHITE)


def test_large_photos_are_scaled_to_the_page_and_view_sizes(page_dir: Path):
    meta = normalize_page(
        io.BytesIO(_encoded(_left_third_red(5000, 2500), "JPEG")), page_dir, 0, original_filename=None
    )

    assert (meta.width, meta.height) == (4096, 2048)
    assert (meta.view_width, meta.view_height) == (2048, 1024)
    with Image.open(page_dir / images.VIEW_FILE) as view:
        assert view.size == (2048, 1024)
    with Image.open(page_dir / images.THUMB_FILE) as thumb:
        assert thumb.size == (480, 240)


@pytest.fixture()
def full_size_copies(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[list[str], tuple[int, int]]]:
    """
    Records the mode of every image Pillow makes at the upload's own size (new, copied, converted or turned) while it's
    normalized: with a 100-megapixel upload each is about 400 MB. The page size is lowered so small images stand in
    for large ones.
    """
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 1000)
    full_size = (2400, 1600)
    made: list[str] = []
    real_new = Image.Image._new

    def new(self: Image.Image, im) -> Image.Image:
        result = real_new(self, im)
        if result.size in (full_size, full_size[::-1]):
            made.append(result.mode)
        return result

    monkeypatch.setattr(Image.Image, "_new", new)
    yield made, full_size


def test_a_large_upload_is_scaled_down_before_it_is_copied(page_dir: Path, full_size_copies):
    made, size = full_size_copies
    rgba = Image.new("RGBA", size, (0, 0, 0, 0))
    rgba.paste((255, 0, 0, 255), (0, 0, 800, 1600))
    rgb = _left_third_red(*size)
    uploads = [_encoded(rgba, "PNG"), _encoded(rgb, "PNG", exif=_exif(orientation=6)), _encoded(rgb, "TIFF")]
    made.clear()

    for raw in uploads:
        meta = normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)
        assert max(meta.width, meta.height) == 1000
    # transparency is flattened into one RGB image; nothing else is copied before it's scaled down, an EXIF turn
    # included (before: six full-size copies for the RGBA upload)
    assert made == ["RGB"]
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert page.size == (1000, 667) or page.size == (667, 1000)


def test_a_large_jpeg_is_decoded_at_a_reduced_scale(page_dir: Path, full_size_copies, monkeypatch: pytest.MonkeyPatch):
    made, size = full_size_copies
    raw = _encoded(_left_third_red(*size), "JPEG", exif=_exif(orientation=6))
    decoded: list[tuple[int, int]] = []
    real_page_rgb = images._page_rgb

    def page_rgb(image: Image.Image) -> Image.Image:
        decoded.append(image.size)
        return real_page_rgb(image)

    monkeypatch.setattr(images, "_page_rgb", page_rgb)
    made.clear()
    meta = normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)

    assert decoded == [(1200, 800)]  # half scale: still larger than the page
    assert (meta.width, meta.height) == (667, 1000)  # then turned upright
    assert made == []


def test_a_truncated_jpeg_is_unreadable(page_dir: Path):
    raw = _encoded(_left_third_red(400, 400), "JPEG")
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(raw[: len(raw) // 2]), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.unreadable_image
    assert not any(page_dir.iterdir())


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"%PDF-1.7\n" + b"0" * 64, IngestRejectReason.pdf_not_supported),
        (b"GIF89a" + b"\x00" * 64, IngestRejectReason.unsupported_format),
        (b"hello, world", IngestRejectReason.unsupported_format),
    ],
)
def test_anything_but_the_accepted_formats_is_rejected(page_dir: Path, data: bytes, reason: IngestRejectReason):
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(data), page_dir, 0, original_filename="card.jpg")
    assert e.value.reason == reason


def test_a_broken_header_is_unreadable_not_an_error(page_dir: Path):
    raw = bytearray(_encoded(_left_third_red(30, 30), "PNG"))
    raw[29] ^= 0xFF  # the IHDR checksum
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(bytes(raw)), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.unreadable_image


def test_the_format_comes_from_the_bytes_not_the_name(page_dir: Path):
    meta = normalize_page(
        io.BytesIO(_encoded(_left_third_red(30, 30), "PNG")), page_dir, 0, original_filename="IMG.HEIC"
    )
    assert meta.format == "png"


def _png_header_only(width: int, height: int) -> bytes:
    """A PNG claiming a size, with a few bytes of image data: opening it is cheap, decoding it fails"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b"")
    )


def test_the_pixel_cap_is_checked_before_the_image_is_decoded(page_dir: Path, monkeypatch: pytest.MonkeyPatch):
    # 81 megapixels: under Pillow's own warning, over a cap lowered for the test. Decoding it would fail as
    # unreadable, so too_many_pixels shows the check came first.
    monkeypatch.setattr(limits, "MAX_PIXELS", 50_000_000)
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(_png_header_only(9000, 9000)), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_many_pixels


def test_the_pixel_cap_uses_width_times_height(page_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "MAX_PIXELS", 100)
    raw = _encoded(_left_third_red(10, 10), "PNG")
    normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)  # exactly at the cap

    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(_encoded(_left_third_red(11, 10), "PNG")), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_many_pixels


@pytest.fixture()
def small_pixel_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pixel caps scaled down, so small images stand in for 200-megapixel phone photos"""
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 1024)
    monkeypatch.setattr(limits, "MAX_PIXELS", 2_000_000)
    monkeypatch.setattr(images, "MAX_JPEG_SOURCE_PIXELS", 10_000_000)


def test_a_jpeg_over_the_pixel_cap_is_read_at_a_reduced_scale(
    page_dir: Path, small_pixel_caps, monkeypatch: pytest.MonkeyPatch
):
    # 6 megapixels, three times the cap: decoded at half size it's 1.5
    decoded: list[tuple[int, int]] = []
    real_page_rgb = images._page_rgb

    def page_rgb(image: Image.Image) -> Image.Image:
        decoded.append(image.size)
        return real_page_rgb(image)

    monkeypatch.setattr(images, "_page_rgb", page_rgb)
    meta = normalize_page(
        io.BytesIO(_encoded(_left_third_red(3000, 2000), "JPEG")), page_dir, 0, original_filename=None
    )

    assert decoded == [(1500, 1000)]
    assert (meta.width, meta.height) == (1024, 683)
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert _close(page.getpixel((100, 300)), RED) and _close(page.getpixel((900, 300)), WHITE)


def test_other_formats_keep_the_pixel_cap(page_dir: Path, small_pixel_caps):
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(_encoded(_left_third_red(3000, 2000), "PNG")), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_many_pixels


def test_a_jpeg_over_its_own_source_cap_is_refused(page_dir: Path, small_pixel_caps):
    # 12 megapixels: over the JPEG cap, though its 1/4-scale decoding would fit
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(_encoded(_left_third_red(4000, 3000), "JPEG")), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_many_pixels
    assert not any(page_dir.iterdir())


def test_a_cmyk_jpeg_over_the_pixel_cap_becomes_an_rgb_page(page_dir: Path, small_pixel_caps):
    cmyk = Image.new("CMYK", (3000, 2000), (0, 255, 255, 0))  # red
    meta = normalize_page(io.BytesIO(_encoded(cmyk, "JPEG")), page_dir, 0, original_filename=None)

    assert (meta.width, meta.height) == (1024, 683)
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert page.mode == "RGB"
        assert _close(page.getpixel((500, 300)), RED, tolerance=60)


def test_an_mpo_over_the_pixel_cap_is_read_from_its_first_frame_at_a_reduced_scale(page_dir: Path, small_pixel_caps):
    raw = _encoded(
        Image.new("RGB", (3000, 2000), RED),
        "MPO",
        save_all=True,
        append_images=[Image.new("RGB", (3000, 2000), "blue")],
    )
    meta = normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)

    assert meta.format == "mpo"
    assert (meta.width, meta.height) == (1024, 683)
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert _close(page.getpixel((500, 300)), RED)


@pytest.mark.filterwarnings("error::PIL.Image.DecompressionBombWarning")
@pytest.mark.parametrize(
    "size, format",
    [
        ((40, 30), "PNG"),  # between Pillow's warning and its refusal
        ((60, 60), "PNG"),  # over Pillow's refusal, under ours
        ((40, 30), "JPEG"),
        ((100, 60), "JPEG"),
    ],
)
def test_pillows_decompression_bomb_check_doesnt_apply(
    page_dir: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int], format: str
):
    # Pillow warns above MAX_IMAGE_PIXELS and refuses above twice that; our own caps apply instead, and a warning
    # (an error here, as anywhere warnings are errors) is never raised. The global is lowered for the test only.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    meta = normalize_page(io.BytesIO(_encoded(_left_third_red(*size), format)), page_dir, 0, original_filename=None)

    assert (meta.width, meta.height) == size
    assert Image.MAX_IMAGE_PIXELS == 1000


def _jpeg_header_claiming(width: int, height: int) -> bytes:
    """A small JPEG whose frame header claims another size: opening it is cheap, and libjpeg pads the missing data"""
    raw = bytearray(_encoded(Image.new("RGB", (16, 16), RED), "JPEG"))
    sof = raw.index(b"\xff\xc0")
    raw[sof + 5 : sof + 9] = struct.pack(">HH", height, width)
    return bytes(raw)


def test_a_200_megapixel_phone_jpeg_is_accepted(page_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 1024)  # decoded at 1/8 scale: a small allocation for the test
    meta = normalize_page(io.BytesIO(_jpeg_header_claiming(16320, 12240)), page_dir, 0, original_filename=None)
    assert (meta.width, meta.height) == (1024, 768)


def test_a_jpeg_over_260_megapixels_is_refused_before_decoding(page_dir: Path, monkeypatch: pytest.MonkeyPatch):
    def no_decoding(*args, **kwargs):
        raise AssertionError("decoded")

    monkeypatch.setattr(images, "_page_rgb", no_decoding)
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(_jpeg_header_claiming(18000, 15000)), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_many_pixels


def _jpeg_header_only(
    width: int, height: int, sampling: list[tuple[int, int]], *, progressive: bool, scan_components: int
) -> bytes:
    """A JPEG's markers up to its first scan, which holds `scan_components` of the frame's components; no image data"""

    def segment(marker: int, data: bytes) -> bytes:
        return bytes([0xFF, marker]) + struct.pack(">H", len(data) + 2) + data

    components = b"".join(bytes([number, h << 4 | v, 0]) for number, (h, v) in enumerate(sampling, start=1))
    frame = struct.pack(">BHHB", 8, height, width, len(sampling)) + components
    scan = bytes([scan_components]) + b"".join(bytes([number, 0]) for number in range(1, scan_components + 1))
    return (
        b"\xff\xd8"
        + segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
        + b"\xff\xff"  # a fill byte
        + segment(0xC2 if progressive else 0xC0, frame)
        + segment(0xDA, scan + bytes([0, 63, 0]))
        + b"\x00" * 64
        + b"\xff\xd9"
    )


FULL = [(1, 1), (1, 1), (1, 1)]
"""4:4:4"""
HALF = [(2, 2), (1, 1), (1, 1)]
"""4:2:0, a phone's"""


@pytest.fixture()
def small_coefficient_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 512 x 512 JPEG's coefficients take 1.5 MB at 4:4:4 and 0.8 MB at 4:2:0"""
    monkeypatch.setattr(images, "MAX_JPEG_COEFFICIENT_BYTES", 1_000_000)


@pytest.mark.parametrize("subsampling", [0, 2])
def test_a_baseline_jpeg_isnt_held_to_the_coefficient_budget(page_dir: Path, small_coefficient_budget, subsampling):
    raw = _encoded(_left_third_red(512, 512), "JPEG", subsampling=subsampling)
    assert normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None).width == 512


def test_a_progressive_jpeg_over_the_coefficient_budget_is_refused_before_decoding(
    page_dir: Path, small_coefficient_budget, monkeypatch: pytest.MonkeyPatch
):
    def no_decoding(*args, **kwargs):
        raise AssertionError("decoded")

    raw = _encoded(_left_third_red(512, 512), "JPEG", progressive=True, subsampling=0)
    with monkeypatch.context() as patched:
        patched.setattr(ImageFile.ImageFile, "load", no_decoding)
        with pytest.raises(PageRejected) as e:
            normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_many_pixels
    assert not any(page_dir.iterdir())

    # the same pixels at 4:2:0 fit
    raw = _encoded(_left_third_red(512, 512), "JPEG", progressive=True, subsampling=2)
    assert normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None).width == 512


def test_a_jpeg_whose_components_are_in_scans_of_their_own_is_held_to_the_budget(
    page_dir: Path, small_coefficient_budget
):
    # sequential, but its first scan holds one component of three: libjpeg keeps every coefficient, as for a
    # progressive JPEG, which Pillow doesn't say it is. Its decoding would fail (no data): refused before that.
    raw = _jpeg_header_only(512, 512, FULL, progressive=False, scan_components=1)
    with Image.open(io.BytesIO(raw)) as image:
        assert not image.info.get("progressive")
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_many_pixels


def test_a_small_progressive_file_claiming_a_huge_frame_is_refused_before_decoding(page_dir: Path):
    # the finding's 2.9 MB progressive 4:4:4 JPEG of 16100 x 16100 pixels: 1.5 GB of coefficients to decode it
    raw = _jpeg_header_only(16100, 16100, FULL, progressive=True, scan_components=3)
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(raw), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_many_pixels


@pytest.mark.parametrize(
    "width, height, sampling, progressive, scan_components, expected",
    [
        (16320, 12240, HALF, False, 3, 0),  # a 200-megapixel phone photo: decoded a row of blocks at a time
        (16320, 12240, HALF, True, 3, 599_270_400),  # the same, saved progressive: just within the budget
        (10000, 10000, FULL, True, 3, 600_000_000),  # 100 megapixels at 4:4:4: the budget
        (10000, 10000, FULL, False, 1, 600_000_000),  # its components in scans of their own
        (16100, 16100, FULL, True, 3, 3 * 2013 * 2013 * 128),
        (17, 9, HALF, True, 3, (4 * 2 + 2 * 2) * 128),  # blocks rounded up to the sampling factors
        (8000, 8000, [(1, 1)] * 4, True, 4, 4 * 1000 * 1000 * 128),  # CMYK
    ],
)
def test_the_coefficient_memory_of_a_jpeg(
    width: int,
    height: int,
    sampling: list[tuple[int, int]],
    progressive: bool,
    scan_components: int,
    expected: int,
):
    raw = io.BytesIO(
        _jpeg_header_only(width, height, sampling, progressive=progressive, scan_components=scan_components)
    )
    frame = images._jpeg_frame(raw)
    assert frame is not None
    assert images._jpeg_coefficient_bytes(frame) == expected


def test_the_coefficient_budget_is_a_100_megapixel_444_jpegs():
    assert images.MAX_JPEG_COEFFICIENT_BYTES == 10000 * 10000 * 3 * 2


def test_the_checks_leave_the_file_where_pillow_left_it():
    raw = io.BytesIO(_encoded(_left_third_red(64, 64), "JPEG", progressive=True))
    raw.seek(5)
    images._check_jpeg_decoding(raw)
    assert raw.tell() == 5


@pytest.mark.parametrize(
    "raw",
    [
        _jpeg_header_only(800, 600, HALF, progressive=True, scan_components=3).replace(b"\xff\xda", b"\xff\xd9"),
        b"\xff\xd8"
        + b"\xff\xfe\x00\x02" * 5000
        + _jpeg_header_only(80, 60, HALF, progressive=True, scan_components=3)[2:],
        b"\xff\xd8\xff\xe0\x00\x10" + os.urandom(64 * 1024 + 100).replace(b"\xff", b"\x00"),
    ],
    ids=["no scan", "too many segments", "too many stray bytes"],
)
def test_a_jpeg_header_that_cant_be_read_is_refused_before_decoding(page_dir: Path, raw: bytes):
    with pytest.raises(PageRejected) as e:
        images._check_jpeg_decoding(io.BytesIO(raw))
    assert e.value.reason == IngestRejectReason.unreadable_image


def _scans(raw: bytes) -> list[int]:
    """Where each of a JPEG's scans starts (its SOS marker): a stuffed FF DA can't occur in scan data"""
    starts, position = [], 0
    while (position := raw.find(b"\xff\xda", position)) >= 0:
        starts.append(position)
        position += 2
    return starts


def _with_extra_scans(raw: bytes, count: int) -> bytes:
    """`raw` with its last scan sent again `count` times: libjpeg decodes the repeats too (a bogus progression)"""
    end = raw.rindex(b"\xff\xd9")
    last = raw[_scans(raw)[-1] : end]
    return raw[:end] + last * count + raw[end:]


def test_scans_are_counted_past_stuffed_bytes_restart_markers_and_tables():
    raw = _encoded(
        Image.frombytes("RGB", (256, 256), os.urandom(256 * 256 * 3)), "JPEG", progressive=True, restart_marker_blocks=4
    )
    assert b"\xff\x00" in raw and b"\xff\xd0" in raw  # its scans' data has stuffed bytes and restart markers

    def counted(raw: bytes, limit: int = 1000) -> int:
        raw = raw.replace(b"\xff\xd9", b"\xff\xfe\x00\x06\xff\xda\xff\xda\xff\xd9")  # a comment that looks like scans
        frame = images._jpeg_frame(io.BytesIO(raw))
        assert frame is not None and frame.multi_scan
        return images._jpeg_scans(io.BytesIO(raw), frame, limit)

    assert counted(raw) == 10  # libjpeg's default progression
    assert counted(_with_extra_scans(raw, 5)) == 15
    assert counted(_with_extra_scans(raw, 500), limit=100) == 101  # it stops counting there


def test_a_progressive_jpeg_with_too_many_scans_is_refused_before_decoding(
    page_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # each scan is gone over once, however little data it holds: thousands of empty ones take minutes
    raw = _encoded(_left_third_red(400, 300), "JPEG", progressive=True)
    with_more = _with_extra_scans(raw, images.MAX_JPEG_SCANS - 10)  # 10 scans, plus 90
    assert normalize_page(io.BytesIO(with_more), page_dir, 0, original_filename=None).width == 400

    def no_decoding(*args, **kwargs):
        raise AssertionError("decoded")

    monkeypatch.setattr(ImageFile.ImageFile, "load", no_decoding)
    too_many = _with_extra_scans(raw, images.MAX_JPEG_SCANS - 9)
    with pytest.raises(PageRejected) as e:
        images._check_jpeg_decoding(io.BytesIO(too_many))
    assert e.value.reason == IngestRejectReason.unreadable_image
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(too_many), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.unreadable_image


def test_a_baseline_jpegs_scans_arent_counted(page_dir: Path, monkeypatch: pytest.MonkeyPatch):
    counted: list[int] = []
    monkeypatch.setattr(images, "_jpeg_scans", lambda *args: counted.append(1) or 1)
    normalize_page(io.BytesIO(_encoded(_left_third_red(64, 64), "JPEG")), page_dir, 0, original_filename=None)
    assert counted == []


def test_pillows_own_pixel_limit_is_left_alone(page_dir: Path, small_pixel_caps):
    before = Image.MAX_IMAGE_PIXELS
    normalize_page(io.BytesIO(_encoded(_left_third_red(3000, 2000), "JPEG")), page_dir, 0, original_filename=None)
    assert Image.MAX_IMAGE_PIXELS == before


def test_files_over_the_size_limit_are_rejected(page_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 100)
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(_encoded(_left_third_red(200, 200), "PNG")), page_dir, 0, original_filename=None)
    assert e.value.reason == IngestRejectReason.too_large


def test_pages_are_only_written_into_an_existing_directory(tmp_path: Path):
    missing = tmp_path / "gone" / "0"
    with pytest.raises(FileNotFoundError):
        normalize_page(io.BytesIO(_encoded(_left_third_red(30, 30), "PNG")), missing, 0, original_filename=None)
    assert not (tmp_path / "gone").exists()


def test_no_temporary_files_are_left_behind(page_dir: Path):
    normalize_page(io.BytesIO(_encoded(_left_third_red(30, 30), "PNG")), page_dir, 0, original_filename=None)
    assert sorted(path.name for path in page_dir.iterdir()) == sorted(
        [images.PAGE_FILE, images.VIEW_FILE, images.THUMB_FILE]
    )


# ==========================================
# expand_document: multi-page TIFFs and PDFs

BLUE = (0, 0, 255)


def _tiff(*colors: tuple[int, int, int], size: tuple[int, int] = (60, 40)) -> bytes:
    frames = [Image.new("RGB", size, color) for color in colors]
    return _encoded(frames[0], "TIFF", save_all=True, append_images=frames[1:])


def _pdf_of(*colors: tuple[int, int, int], size: tuple[int, int] = (300, 200)) -> bytes:
    """A PDF with one page per colour, as Pillow writes it (each page an embedded image)"""
    pages = [Image.new("RGB", size, color) for color in colors]
    return _encoded(pages[0], "PDF", save_all=True, append_images=pages[1:], resolution=100)


def _normalized(pages: list[images.DocumentPage], root: Path, name: str) -> list[tuple[PageMeta, Path]]:
    out = []
    for index, page in enumerate(pages):
        page_dir = root / str(index)
        page_dir.mkdir(parents=True)
        meta = images.normalize_document_page(
            page, page_dir, index, original_filename=images.page_filename(name, page.number)
        )
        out.append((meta, page_dir))
    return out


def _center(page_dir: Path) -> tuple:
    with Image.open(page_dir / images.PAGE_FILE) as page:
        return page.getpixel((page.width // 2, page.height // 2))


def test_a_single_image_is_one_page_identified_by_its_bytes():
    raw = _encoded(_left_third_red(30, 20), "PNG")
    pages = images.expand_document(io.BytesIO(raw))

    assert len(pages) == 1
    page = pages[0]
    assert (page.kind, page.number, page.frame, page.rendered) == ("png", None, 0, False)
    assert page.raw_sha256 == hashlib.sha256(raw).hexdigest() and page.raw_bytes == len(raw)


def test_a_two_page_tiff_gives_two_pages(tmp_path: Path):
    raw = _tiff(RED, BLUE)
    pages = images.expand_document(io.BytesIO(raw))

    assert [(page.number, page.frame) for page in pages] == [(1, 0), (2, 1)]
    # each page has an identity of its own, derived from the file's: the same file again is the same pages
    assert len({page.raw_sha256 for page in pages}) == 2
    assert hashlib.sha256(raw).hexdigest() not in {page.raw_sha256 for page in pages}
    assert [page.raw_sha256 for page in images.expand_document(io.BytesIO(raw))] == [p.raw_sha256 for p in pages]

    normalized = _normalized(pages, tmp_path, "scan.tiff")
    assert [meta.original_filename for meta, _ in normalized] == ["scan.tiff (page 1)", "scan.tiff (page 2)"]
    assert all(meta.format == "tiff" and meta.raw_bytes == len(raw) for meta, _ in normalized)
    assert _close(_center(normalized[0][1]), RED) and _close(_center(normalized[1][1]), BLUE)
    for _, page_dir in normalized:
        _assert_no_metadata(page_dir)


def test_a_16_bit_tiff_page_keeps_its_tones(tmp_path: Path):
    width, height = 256, 64
    values = _gradient_16(width, height)
    gray = Image.frombytes("I;16", (width, height), struct.pack(f"<{len(values)}H", *values))
    raw = _encoded(Image.new("RGB", (width, height), RED), "TIFF", save_all=True, append_images=[gray])

    pages = images.expand_document(io.BytesIO(raw))
    (_, _), (_, second) = _normalized(pages, tmp_path, "scan.tif")
    with Image.open(second / images.PAGE_FILE) as page:
        left, middle, right = (page.getpixel((x, 32)) for x in (2, 128, 253))
    assert all(value < 20 for value in left), left
    assert all(100 < value < 155 for value in middle), middle
    assert all(value > 235 for value in right), right


def test_a_tiffs_reduced_resolution_copy_isnt_a_page():
    from PIL import TiffImagePlugin

    buffer = io.BytesIO()
    with TiffImagePlugin.AppendingTiffWriter(buffer, new=True) as tiff:
        Image.new("RGB", (200, 100), RED).save(tiff, format="TIFF")
        tiff.newFrame()
        Image.new("RGB", (20, 10), RED).save(tiff, format="TIFF", tiffinfo={254: 1})  # a thumbnail
        tiff.newFrame()

    pages = images.expand_document(io.BytesIO(buffer.getvalue()))
    assert [(page.number, page.frame) for page in pages] == [(None, 0)]


def test_a_tiff_with_more_pages_than_a_card_is_refused():
    with pytest.raises(PageRejected) as e:
        images.expand_document(io.BytesIO(_tiff(RED, BLUE, RED, BLUE, RED)))
    assert e.value.reason == IngestRejectReason.too_many_pages


def test_a_two_page_pdf_gives_two_rendered_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 800)
    raw = _pdf_of(RED, BLUE)
    pages = images.expand_document(io.BytesIO(raw))
    try:
        assert [(page.number, page.kind, page.format, page.rendered) for page in pages] == [
            (1, "png", "pdf", True),
            (2, "png", "pdf", True),
        ]
        assert len({page.raw_sha256 for page in pages}) == 2
        normalized = _normalized(pages, tmp_path, "scan.pdf")
    finally:
        images.close_pages(pages)

    assert all(page.file.closed for page in pages)
    assert [meta.original_filename for meta, _ in normalized] == ["scan.pdf (page 1)", "scan.pdf (page 2)"]
    for meta, page_dir in normalized:
        assert meta.format == "pdf" and meta.raw_bytes == len(raw)
        assert (meta.width, meta.height) == (800, 534)  # rendered at the page's long side (PDFium rounds up)
        _assert_no_metadata(page_dir)
    assert _close(_center(normalized[0][1]), RED) and _close(_center(normalized[1][1]), BLUE)


def test_a_one_page_pdf_is_identified_by_its_bytes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 400)
    raw = _pdf_of(RED)
    pages = images.expand_document(io.BytesIO(raw))
    images.close_pages(pages)

    assert [(page.number, page.raw_sha256) for page in pages] == [(None, hashlib.sha256(raw).hexdigest())]
    assert images.page_filename("scan.pdf", None) == "scan.pdf"


def test_a_pdf_page_is_rendered_within_the_pixel_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 1000)
    monkeypatch.setattr(limits, "MAX_PIXELS", 100_000)
    pages = images.expand_document(io.BytesIO(_pdf_of(RED, size=(300, 300))))
    try:
        ((meta, _),) = _normalized(pages, tmp_path, "square.pdf")
    finally:
        images.close_pages(pages)
    assert meta.width * meta.height <= 100_000
    assert meta.width >= 310  # as large as the cap allows


@pytest.mark.parametrize("pixels", [100_000, 99_999, 2_000_000])
def test_render_scale_never_exceeds_the_pixel_cap(pixels: int):
    from mealie.services.ai.ingest.pdf_render import render_scale

    for width, height in [(612, 792), (300.3, 200.7), (1, 5000), (14400, 14400)]:
        scale = render_scale(width, height, 4096, pixels)
        assert math.ceil(width * scale) * math.ceil(height * scale) <= pixels
        assert max(width, height) * scale <= 4096 + 1


def test_a_pdf_with_more_pages_than_a_card_is_refused():
    with pytest.raises(PageRejected) as e:
        images.expand_document(io.BytesIO(_pdf_of(RED, BLUE, RED, BLUE, RED, size=(30, 20))))
    assert e.value.reason == IngestRejectReason.too_many_pages


def _pdf_document(objects: list[bytes], trailer: bytes = b"") -> bytes:
    """A PDF from numbered objects (1, 2, ...), with a correct cross-reference table"""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R %s >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, trailer, xref)
    return bytes(out)


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


_PDF_PADDING = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _password_protected_pdf(password: bytes) -> bytes:
    """A one-page PDF that needs `password` to open (the standard security handler, revision 2, RC4 40-bit)"""
    padded = (password + _PDF_PADDING)[:32]
    owner = _rc4(hashlib.md5(padded).digest()[:5], padded)  # the owner password is the user's
    permissions = -4
    file_id = b"0123456789abcdef"
    key = hashlib.md5(padded + owner + struct.pack("<i", permissions) + file_id).digest()[:5]
    user = _rc4(key, _PDF_PADDING)
    encrypt = b"<< /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P %d >>" % (
        owner.hex().encode(),
        user.hex().encode(),
        permissions,
    )
    return _pdf_document(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >>",
            encrypt,
        ],
        trailer=b"/Encrypt 4 0 R /ID [<%s> <%s>]" % (file_id.hex().encode(), file_id.hex().encode()),
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"%PDF-1.7\n" + os.urandom(2000), id="damaged"),
        pytest.param(
            _pdf_document([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"]),
            id="no pages",
        ),
        pytest.param(_password_protected_pdf(b"secret"), id="password"),
    ],
)
def test_a_pdf_that_cant_be_opened_is_refused(raw: bytes):
    with pytest.raises(PageRejected) as e:
        images.expand_document(io.BytesIO(raw))
    assert e.value.reason == IngestRejectReason.pdf_not_supported


def test_the_password_protected_test_pdf_is_otherwise_valid():
    # the refusal above is the password's: with it, PDFium opens the same file
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(_password_protected_pdf(b"secret"), password="secret")
    assert len(pdf) == 1
    pdf.close()


@pytest.fixture()
def fake_renderer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Points the PDF renderer at a script of the test's"""

    def install(code: str) -> Path:
        script = tmp_path / "renderer.py"
        script.write_text(code)
        monkeypatch.setattr(images, "_PDF_RENDERER", script)
        return script

    return install


def test_a_renderer_that_hangs_is_stopped(fake_renderer, monkeypatch: pytest.MonkeyPatch):
    fake_renderer("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(images, "PDF_RENDER_TIMEOUT", 0.5)
    started = time.monotonic()
    with pytest.raises(PageRejected) as e:
        images.expand_document(io.BytesIO(_pdf_of(RED, size=(30, 20))))
    assert e.value.reason == IngestRejectReason.pdf_not_supported
    assert time.monotonic() - started < 10


def test_a_renderer_that_crashes_is_a_refusal_not_an_error(fake_renderer):
    fake_renderer("import os\nos.abort()\n")
    with pytest.raises(PageRejected) as e:
        images.expand_document(io.BytesIO(_pdf_of(RED, size=(30, 20))))
    assert e.value.reason == IngestRejectReason.pdf_not_supported


_FRAME_WRITER = (
    "import json, os, sys\n"
    "def frame(kind, data):\n"
    "    sys.stdout.buffer.write(kind + len(data).to_bytes(8, 'big') + data)\n"
    "    sys.stdout.buffer.flush()\n"
)
"""A fake renderer's start: `frame(kind, data)` writes one of the renderer's stdout frames"""


def test_the_renderer_gets_none_of_the_servers_environment(fake_renderer, monkeypatch: pytest.MonkeyPatch):
    # the page it sends is the environment it got
    fake_renderer(
        _FRAME_WRITER + "frame(b'P', json.dumps(dict(os.environ)).encode())\nframe(b'R', b'{\"pages\": 1}')\n"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    pages = images.expand_document(io.BytesIO(_pdf_of(RED, size=(30, 20))))
    try:
        seen = json.loads(pages[0].file.read())
    finally:
        images.close_pages(pages)

    assert "OPENAI_API_KEY" not in seen
    assert set(seen) <= set(images._CHILD_ENVIRONMENT) | {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("frame(b'P', b'png')\n", id="no result"),
        pytest.param("frame(b'P', b'png')\nframe(b'R', b'{\"pages\": 2}')\n", id="fewer pages than it says"),
        pytest.param("sys.stdout.buffer.write(b'P' + (1 << 40).to_bytes(8, 'big'))\n", id="a page too large"),
        pytest.param("frame(b'X', b'')\nframe(b'R', b'{\"pages\": 0}')\n", id="an unknown frame"),
        pytest.param("frame(b'R', b'[1]')\n", id="a result that isn't an object"),
        pytest.param("frame(b'P', b'png')\nframe(b'R', b'{\"pages\": 1}')\nsys.exit(3)\n", id="a failed exit"),
    ],
)
def test_a_renderer_answer_that_doesnt_add_up_is_a_refusal(fake_renderer, script: str):
    fake_renderer(_FRAME_WRITER + script)
    with pytest.raises(PageRejected) as e:
        images.expand_document(io.BytesIO(_pdf_of(RED, size=(30, 20))))
    assert e.value.reason == IngestRejectReason.pdf_not_supported


def test_a_renderer_that_keeps_writing_is_stopped(fake_renderer, monkeypatch: pytest.MonkeyPatch):
    fake_renderer(_FRAME_WRITER + "frame(b'P', b'png')\nwhile True:\n    frame(b'P', b'png')\n")
    with pytest.raises(PageRejected) as e:
        images.expand_document(io.BytesIO(_pdf_of(RED, size=(30, 20))))
    assert e.value.reason == IngestRejectReason.pdf_not_supported


def test_the_renderer_writes_its_pages_to_stdout_not_files(monkeypatch: pytest.MonkeyPatch):
    """It gets no output folder: nothing but the document is in its work folder, before and after"""
    seen: list[list[str]] = []
    real = images._run_renderer

    def run_renderer(document: Path):
        seen.append(sorted(path.name for path in document.parent.iterdir()))
        frames = real(document)
        seen.append(sorted(path.name for path in document.parent.iterdir()))
        return frames

    monkeypatch.setattr(images, "_run_renderer", run_renderer)
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 400)
    pages = images.expand_document(io.BytesIO(_pdf_of(RED, BLUE)))
    try:
        assert [Image.open(page.file).format for page in pages] == ["PNG", "PNG"]
    finally:
        images.close_pages(pages)
    assert seen == [["document.pdf"], ["document.pdf"]]


def test_normalize_page_alone_still_refuses_a_pdf(page_dir: Path):
    with pytest.raises(PageRejected) as e:
        normalize_page(io.BytesIO(_pdf_of(RED, size=(30, 20))), page_dir, 0, original_filename="scan.pdf")
    assert e.value.reason == IngestRejectReason.pdf_not_supported


@pytest.mark.parametrize(
    "name, number, expected",
    [
        ("scan.pdf", None, "scan.pdf"),
        ("scan.pdf", 2, "scan.pdf (page 2)"),
        (None, 3, "Page 3"),
        ("x" * 300 + ".tiff", 4, "x" * 106 + ".tiff (page 4)"),
    ],
)
def test_page_names_say_which_page_of_the_file(name: str | None, number: int | None, expected: str):
    assert images.page_filename(name, number) == expected
    assert len(expected) <= images.MAX_FILENAME_LENGTH


# ==========================================
# rotate_page_files and crop_region


def _page(page_dir: Path, width: int = 600, height: int = 300) -> PageMeta:
    return normalize_page(
        io.BytesIO(_encoded(_left_third_red(width, height), "JPEG", icc_profile=_srgb_profile())),
        page_dir,
        0,
        original_filename=None,
    )


@pytest.mark.parametrize(
    "degrees, size, red_at, white_at",
    [
        (90, (300, 600), (150, 50), (150, 550)),
        (180, (600, 300), (550, 150), (50, 150)),
        (270, (300, 600), (150, 550), (150, 50)),
    ],
)
def test_rotating_a_page_rewrites_every_file(
    page_dir: Path, degrees: int, size: tuple[int, int], red_at: tuple[int, int], white_at: tuple[int, int]
):
    before = _page(page_dir).model_copy(update={"ocr": {"text": "sideways", "confidence": 20}})

    after = rotate_page_files(page_dir, before, degrees, PageRotationSource.user)

    assert (after.width, after.height) == size
    assert after.rotation == degrees
    assert after.rotation_source == PageRotationSource.user
    assert after.oriented
    assert after.ocr is None  # read at the old orientation
    assert after.page_sha256 != before.page_sha256
    assert after.raw_sha256 == before.raw_sha256
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert page.size == size
        assert _close(page.getpixel(red_at), RED)
        assert _close(page.getpixel(white_at), WHITE)
        assert page.info.get("icc_profile") == _srgb_profile()
    with Image.open(page_dir / images.VIEW_FILE) as view:
        assert view.size == size
    with Image.open(page_dir / images.THUMB_FILE) as thumb:
        assert thumb.size == ((240, 480) if size[1] > size[0] else (480, 240))
    _assert_no_metadata(page_dir)


def test_rotations_add_up(page_dir: Path):
    meta = _page(page_dir)
    meta = rotate_page_files(page_dir, meta, 270, PageRotationSource.ocr)
    meta = rotate_page_files(page_dir, meta, 180, PageRotationSource.user)
    assert meta.rotation == 90


def test_only_quarter_turns_are_allowed(page_dir: Path):
    with pytest.raises(ValueError):
        rotate_page_files(page_dir, _page(page_dir), 45, PageRotationSource.user)


# ==========================================
# Staged turns: a crash never leaves files and stored metadata disagreeing


def _files(page_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(page_dir.iterdir())}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _staged_names() -> set[str]:
    return set(images.STAGED_FILES.values())


def test_staging_a_turn_leaves_the_page_as_it_is(page_dir: Path):
    before = _page(page_dir)
    current = _files(page_dir)

    staged = stage_rotation(page_dir, before, 90, PageRotationSource.ocr)

    assert {name: data for name, data in _files(page_dir).items() if name in current} == current
    assert set(_files(page_dir)) == set(current) | _staged_names()
    assert staged.page_sha256 == _sha(page_dir / "page.next.jpg") != before.page_sha256
    assert (staged.width, staged.height, staged.rotation, staged.oriented) == (300, 600, 90, True)
    with Image.open(page_dir / "view.next.jpg") as view:
        assert view.size == (staged.view_width, staged.view_height)


def test_applying_a_staged_turn_swaps_every_file(page_dir: Path):
    staged = stage_rotation(page_dir, _page(page_dir), 90, PageRotationSource.user)
    next_files = {name: (page_dir / staged_name).read_bytes() for name, staged_name in images.STAGED_FILES.items()}

    apply_staged(page_dir)

    assert _files(page_dir) == next_files
    assert _sha(page_dir / images.PAGE_FILE) == staged.page_sha256


def test_discarding_a_staged_turn_keeps_the_page(page_dir: Path):
    before = _page(page_dir)
    current = _files(page_dir)
    stage_rotation(page_dir, before, 180, PageRotationSource.user)
    discard_staged(page_dir)
    assert _files(page_dir) == current


def test_a_staging_that_fails_leaves_nothing_staged(page_dir: Path, monkeypatch: pytest.MonkeyPatch):
    before = _page(page_dir)
    current = _files(page_dir)

    def full_disk(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(images, "atomic_save_image", full_disk)
    with pytest.raises(OSError):
        stage_rotation(page_dir, before, 90, PageRotationSource.user)
    assert _files(page_dir) == current


def test_recovery_with_nothing_staged_does_nothing(page_dir: Path):
    before = _page(page_dir)
    current = _files(page_dir)
    assert recover_staged(page_dir, before) == "none"
    assert _files(page_dir) == current


def test_recovery_finishes_a_turn_whose_metadata_was_stored(page_dir: Path):
    # the process died after storing the turned metadata, before the swap
    staged = stage_rotation(page_dir, _page(page_dir), 270, PageRotationSource.ocr)
    turned = {name: (page_dir / staged_name).read_bytes() for name, staged_name in images.STAGED_FILES.items()}

    assert recover_staged(page_dir, staged) == "applied"
    assert _files(page_dir) == turned
    assert _sha(page_dir / images.PAGE_FILE) == staged.page_sha256


def test_recovery_discards_a_turn_whose_metadata_wasnt_stored(page_dir: Path):
    # the process died after staging, before the metadata was stored (or the update was refused)
    before = _page(page_dir)
    current = _files(page_dir)
    stage_rotation(page_dir, before, 90, PageRotationSource.ocr)

    assert recover_staged(page_dir, before) == "discarded"
    assert _files(page_dir) == current
    assert _sha(page_dir / images.PAGE_FILE) == before.page_sha256


def test_recovery_finishes_a_swap_cut_short(page_dir: Path):
    staged = stage_rotation(page_dir, _page(page_dir), 90, PageRotationSource.user)
    turned = {name: (page_dir / staged_name).read_bytes() for name, staged_name in images.STAGED_FILES.items()}
    os.replace(page_dir / "thumb.next.webp", page_dir / images.THUMB_FILE)  # the first of the three moves

    assert recover_staged(page_dir, staged) == "applied"
    assert _files(page_dir) == turned


def test_recovery_finishes_once_the_page_itself_was_moved(page_dir: Path):
    # page.jpg already holds the stored page: what's left staged belongs to it
    staged = stage_rotation(page_dir, _page(page_dir), 90, PageRotationSource.user)
    turned = {name: (page_dir / staged_name).read_bytes() for name, staged_name in images.STAGED_FILES.items()}
    os.replace(page_dir / "page.next.jpg", page_dir / images.PAGE_FILE)

    assert recover_staged(page_dir, staged) == "applied"
    assert _files(page_dir) == turned


@pytest.mark.parametrize("left", [["page.next.jpg"], ["page.next.jpg", "view.next.jpg"]], ids=["page", "page+view"])
def test_recovery_discards_a_staging_or_a_discard_cut_short(page_dir: Path, left: list[str]):
    # staging writes page.next.jpg first and a discard removes it last, so a cut-short one always has it
    before = _page(page_dir)
    current = _files(page_dir)
    stage_rotation(page_dir, before, 90, PageRotationSource.user)
    for name in _staged_names() - set(left):
        (page_dir / name).unlink()

    assert recover_staged(page_dir, before) == "discarded"
    assert _files(page_dir) == current


def test_a_new_turn_first_finishes_one_left_staged(page_dir: Path):
    # a turn whose metadata was stored but whose swap a crash cut short is never overwritten by the next one
    first = stage_rotation(page_dir, _page(page_dir), 90, PageRotationSource.ocr)
    second = stage_rotation(page_dir, first, 90, PageRotationSource.user)
    apply_staged(page_dir)
    assert second.rotation == 180
    assert _sha(page_dir / images.PAGE_FILE) == second.page_sha256
    assert not _staged_names() & set(_files(page_dir))
    with Image.open(page_dir / images.PAGE_FILE) as page:
        assert page.size == (600, 300)
        assert _close(page.getpixel((550, 150)), RED)  # red started on the left: two quarter turns put it right


def test_rotating_at_once_leaves_nothing_staged(page_dir: Path):
    after = rotate_page_files(page_dir, _page(page_dir), 90, PageRotationSource.user)
    assert not _staged_names() & set(_files(page_dir))
    assert _sha(page_dir / images.PAGE_FILE) == after.page_sha256


def test_a_small_region_is_cropped_with_a_margin_and_upscaled_at_most_three_times(page_dir: Path):
    _page(page_dir, 400, 400)

    data = crop_region(page_dir / images.PAGE_FILE, Region(0.25, 0.25, 0.5, 0.5))

    with Image.open(io.BytesIO(data)) as crop:
        assert crop.format == "JPEG"
        # 0.22 to 0.78 of 400 pixels is 224; tripled, which is still under 1000
        assert crop.size == (672, 672)
        assert not crop.getexif()
    # in memory only
    assert len(list(page_dir.iterdir())) == 3


def test_a_region_is_upscaled_to_1000_pixels_when_that_is_under_three_times(page_dir: Path):
    _page(page_dir, 1000, 1000)
    with Image.open(io.BytesIO(crop_region(page_dir / images.PAGE_FILE, Region(0.1, 0.1, 0.5, 0.2)))) as crop:
        assert max(crop.size) == 1000


def test_a_region_at_the_edge_is_clamped_to_the_page(page_dir: Path):
    _page(page_dir, 2000, 1000)
    with Image.open(io.BytesIO(crop_region(page_dir / images.PAGE_FILE, Region(0.0, 0.0, 1.0, 1.0)))) as crop:
        assert crop.size == (2000, 1000)
        assert _close(crop.getpixel((10, 500)), RED)
