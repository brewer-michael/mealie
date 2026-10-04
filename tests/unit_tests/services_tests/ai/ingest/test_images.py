"""Turning uploads into card pages, rotating them and cropping regions (docs/ai/PHASE2.md §2, §4.4, §4.7)"""

import hashlib
import io
import os
import struct
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import BinaryIO

import pytest
from PIL import Image, ImageCms

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


def test_a_multi_page_tiff_is_read_from_its_first_page(page_dir: Path):
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
