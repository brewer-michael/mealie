"""Turning uploads into card pages, rotating them and cropping regions (docs/ai/PHASE2.md §2, §4.4, §4.7)"""

import io
import struct
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import pytest
from PIL import Image, ImageCms

from mealie.schema.recipe_ingest import IngestRejectReason, PageMeta, PageRotationSource
from mealie.services.ai.ingest import images, limits
from mealie.services.ai.ingest.images import PageRejected, Region, crop_region, normalize_page, rotate_page_files, sniff

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


def _srgb_profile() -> bytes:
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
