"""Cleaning a recipe card photo for the repository without jpegtran (docs/ai/PHASE2.md §11.6)"""

import io
from functools import cache
from pathlib import Path

import pytest
from PIL import Image, ImageCms, ImageOps

from mealie.scripts import strip_card_photo as strip

ORIENTATION = 0x0112
GPS_IFD = 0x8825
XMP = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><rdf:Description exif:GPSLatitude='51,30N'/></x:xmpmeta>"
PHOTOSHOP = b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x08\x1c\x02\x00\x00\x02\x00\x04"


@cache
def _icc() -> bytes:
    # built once: the profile's header records when it was made, to the second, so two builds can differ
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload


def _with_segments(data: bytes, *segments: bytes) -> bytes:
    """`data` with `segments` inserted after its JFIF segment"""
    assert data[2:4] == b"\xff\xe0"
    end = 4 + int.from_bytes(data[4:6], "big")
    return data[:end] + b"".join(segments) + data[end:]


def _frame0() -> Image.Image:
    """Upright pixels with a green left edge, so a turn would show"""
    image = Image.new("RGB", (96, 64), (200, 30, 30))
    image.paste((10, 200, 10), (0, 0, 24, 64))
    image.paste((240, 240, 240), (60, 10, 90, 30))
    return image


def _iphone_mpo(**save: object) -> bytes:
    """
    What an iPhone saves: an MPO whose first frame carries GPS EXIF with orientation 6, an ICC profile, the MPF index
    (APP2), plus XMP, Photoshop IPTC and a comment; and a second, smaller frame after it
    """
    exif = Image.Exif()
    exif[ORIENTATION] = 6
    exif[GPS_IFD] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    buffer = io.BytesIO()
    _frame0().save(
        buffer,
        "MPO",
        save_all=True,
        append_images=[Image.new("RGB", (48, 32), (0, 0, 255))],
        exif=exif,
        icc_profile=_icc(),
        comment=b"Taken at 12 Home Street",
        quality=92,
        **save,
    )
    return _with_segments(buffer.getvalue(), strip._segment(strip.APP1, XMP), strip._segment(strip.APP13, PHOTOSHOP))


def _pixels(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        image.seek(0)
        return image.convert("RGB").tobytes()


def _markers(data: bytes) -> list[tuple[int, bytes]]:
    """The segments before the first scan"""
    found: list[tuple[int, bytes]] = []
    pos = 2
    while data[pos + 1] != 0xDA:
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        found.append((data[pos + 1], data[pos + 4 : pos + 2 + length]))
        pos += 2 + length
    return found


def test_an_iphone_mpo_becomes_a_one_frame_jpeg_with_icc_and_no_gps():
    original = _iphone_mpo()
    with Image.open(io.BytesIO(original)) as image:
        assert image.format == "MPO" and getattr(image, "n_frames", 1) == 2
        assert image.getexif().get_ifd(GPS_IFD)

    result = strip.strip_jpeg(original)

    with Image.open(io.BytesIO(result.data)) as image:
        assert image.format == "JPEG"
        assert getattr(image, "n_frames", 1) == 1
        assert image.info.get("icc_profile") == _icc()
        assert "comment" not in image.info and "mp" not in image.info and "xmp" not in image.info
        exif = image.getexif()
        assert dict(exif) == {ORIENTATION: 6}
        assert not exif.get_ifd(GPS_IFD)
        # still turned by EXIF transpose, as the eval needs
        assert ImageOps.exif_transpose(image).size == (64, 96)

    # lossless: the first frame's pixels exactly
    assert _pixels(result.data) == _pixels(original)
    assert b"Home Street" not in result.data and b"GPSLatitude" not in result.data and b"MPF\x00" not in result.data
    assert result.orientation == 6
    assert result.frames_dropped
    assert set(result.dropped) == {"EXIF", "XMP", "MPF index", "APP13 (Photoshop/IPTC)", "comment"}

    # JFIF first, then the minimal EXIF where the original's was, then the colour profile
    assert [marker for marker, _ in _markers(result.data)][:3] == [strip.APP0, strip.APP1, strip.APP2]


def test_drop_orientation_leaves_no_exif():
    result = strip.strip_jpeg(_iphone_mpo(), keep_orientation=False)

    with Image.open(io.BytesIO(result.data)) as image:
        assert not image.getexif()
        assert image.size == (96, 64)
    assert result.orientation is None
    assert strip.APP1 not in [marker for marker, _ in _markers(result.data)]


@pytest.mark.parametrize("save", [{"progressive": True}, {"restart_marker_blocks": 2}])
def test_progressive_scans_and_restart_markers_are_copied_byte_for_byte(save: dict):
    exif = Image.Exif()
    exif[ORIENTATION] = 3
    buffer = io.BytesIO()
    _frame0().save(buffer, "JPEG", exif=exif, quality=90, **save)
    original = buffer.getvalue()

    result = strip.strip_jpeg(original)

    assert _pixels(result.data) == _pixels(original)
    assert result.orientation == 3
    assert not result.frames_dropped


def test_a_photo_without_orientation_gets_no_exif():
    buffer = io.BytesIO()
    _frame0().save(buffer, "JPEG", icc_profile=_icc())

    result = strip.strip_jpeg(buffer.getvalue())

    assert result.orientation is None
    with Image.open(io.BytesIO(result.data)) as image:
        assert not image.getexif()
        assert image.info.get("icc_profile") == _icc()


def test_read_orientation_and_minimal_exif_round_trip():
    payload = strip.minimal_exif(8)

    assert strip.read_orientation(payload) == 8
    assert strip.read_orientation(b"Exif\x00\x00MM\x00*") is None  # cut off
    assert strip.read_orientation(b"not exif") is None


@pytest.mark.parametrize(
    "data",
    [
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
        b"%PDF-1.7",
        b"\xff\xd8\xff\xe0\x00\x10JFIF",  # cut off before the image data
        b"",
    ],
)
def test_anything_but_a_whole_jpeg_is_refused(data: bytes):
    with pytest.raises(strip.NotAJpegError):
        strip.strip_jpeg(data)


def test_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    source = tmp_path / "IMG_2503.jpg"
    source.write_bytes(_iphone_mpo())
    target = tmp_path / "cards" / "banana-mug-cake.jpg"

    assert strip.main([str(source), str(target)]) == 0
    assert "orientation 6 kept" in capsys.readouterr().out
    assert _pixels(target.read_bytes()) == _pixels(source.read_bytes())

    assert strip.main([str(source), str(target), "--drop-orientation"]) == 0
    with Image.open(target) as image:
        assert not image.getexif()

    png = tmp_path / "card.png"
    _frame0().save(png)
    assert strip.main([str(png), str(tmp_path / "out.jpg")]) == 2
    assert "Only JPEG" in capsys.readouterr().err
    assert not (tmp_path / "out.jpg").exists()
