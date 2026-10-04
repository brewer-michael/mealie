"""
Removes the metadata from a recipe card photo losslessly, before it goes into the repository as an eval fixture
(docs/ai/PHASE2.md §11.6, docs/ai/EVAL.md):

    python -m mealie.scripts.strip_card_photo IMG_2503.jpg tests/data/cards/my-card.jpg [--drop-orientation]

Phone photos carry GPS coordinates, the camera and time in EXIF and XMP, and iPhones save an MPO: the photo plus a
second, smaller frame after it, indexed by an MPF segment. This walks the JPEG's segments in pure Python, so neither
`jpegtran` nor `exiftool` is needed, and copies the compressed image data byte for byte, so the pixels are exactly the
first frame's. It:

- keeps the first frame only (everything after its end-of-image marker, the MPO's other frames, is dropped);
- keeps APP2 segments that start with `ICC_PROFILE\\0`, so a Display P3 profile stays, and drops every other APP2 (the
  MPF index);
- drops APP1 (EXIF, XMP), APP13 (Photoshop, IPTC) and comments, and keeps every other segment (JFIF, Adobe, tables);
- writes a minimal EXIF holding only the orientation, so the eval still exercises EXIF transpose (a phone held flat
  records the wrong one), unless `--drop-orientation` is given.
"""

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SOI, EOI, SOS = 0xD8, 0xD9, 0xDA
APP0, APP1, APP2, APP13, COM = 0xE0, 0xE1, 0xE2, 0xED, 0xFE
_STANDALONE = {0x01, *range(0xD0, 0xD8)}
"""Markers without a length: TEM and the restart markers RST0-RST7"""

ICC_PROFILE_ID = b"ICC_PROFILE\x00"
EXIF_ID = b"Exif\x00\x00"
ORIENTATION_TAG = 0x0112


class NotAJpegError(ValueError):
    """The input isn't a JPEG (or MPO) file this can walk"""


@dataclass
class StripResult:
    data: bytes
    orientation: int | None
    """The orientation written to the output's EXIF; None when there is none"""
    dropped: list[str] = field(default_factory=list)
    """What was removed, for the summary"""
    frames_dropped: bool = False


def read_orientation(exif: bytes) -> int | None:
    """The Orientation tag (1-8) of an APP1 EXIF payload (starting `Exif\\0\\0`), or None"""
    if not exif.startswith(EXIF_ID):
        return None
    tiff = exif[len(EXIF_ID) :]
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    order: Literal["little", "big"] = "little" if tiff[:2] == b"II" else "big"

    def number(offset: int, size: int) -> int:
        if offset < 0 or offset + size > len(tiff):
            raise ValueError("EXIF entry outside the segment")
        return int.from_bytes(tiff[offset : offset + size], order)

    try:
        ifd = number(4, 4)
        for i in range(number(ifd, 2)):
            entry = ifd + 2 + 12 * i
            if number(entry, 2) == ORIENTATION_TAG and number(entry + 2, 2) == 3:  # SHORT
                value = number(entry + 8, 2)
                return value if 1 <= value <= 8 else None
    except ValueError:
        return None
    return None


def minimal_exif(orientation: int) -> bytes:
    """An APP1 payload holding one tag, the orientation: `Exif\\0\\0`, a big-endian TIFF header and one IFD entry"""
    tiff = (
        b"MM\x00\x2a\x00\x00\x00\x08"  # big-endian, IFD0 at offset 8
        + (1).to_bytes(2, "big")  # one entry
        + ORIENTATION_TAG.to_bytes(2, "big")
        + (3).to_bytes(2, "big")  # SHORT
        + (1).to_bytes(4, "big")  # one value
        + orientation.to_bytes(2, "big")
        + b"\x00\x00"  # the value's padding
        + (0).to_bytes(4, "big")  # no next IFD
    )
    return EXIF_ID + tiff


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload


def _end_of_scan(data: bytes, pos: int) -> int:
    """Where the entropy-coded data starting at `pos` ends: the next marker that isn't stuffing or a restart"""
    while True:
        pos = data.find(b"\xff", pos)
        if pos < 0 or pos + 1 >= len(data):
            raise NotAJpegError("The image data ends before its end-of-image marker")
        following = data[pos + 1]
        if following == 0x00 or following in _STANDALONE or following == 0xFF:
            pos += 1
            continue
        return pos


def strip_jpeg(data: bytes, *, keep_orientation: bool = True) -> StripResult:
    """
    The JPEG's first frame with its metadata removed (see the module docstring), and what was removed. Raises
    `NotAJpegError` for anything that isn't a well-formed JPEG or MPO.
    """
    if not data.startswith(b"\xff\xd8\xff"):
        raise NotAJpegError("Not a JPEG file")

    kept: list[bytes] = []
    dropped: list[str] = []
    orientation: int | None = None
    exif_at: int | None = None
    """Where in `kept` the minimal EXIF goes: after SOI and a leading JFIF segment"""
    pos = 2
    ended = False
    while pos < len(data):
        if data[pos] != 0xFF:
            raise NotAJpegError(f"Expected a marker at byte {pos}")
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1  # fill bytes
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1

        if marker == EOI:
            ended = True
            break
        if marker in _STANDALONE:
            kept.append(bytes((0xFF, marker)))
            continue
        if marker == SOI:
            raise NotAJpegError("A second start-of-image marker inside the first frame")
        if pos + 2 > len(data):
            raise NotAJpegError("A segment is cut off")
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > len(data):
            raise NotAJpegError("A segment's length runs past the end of the file")
        payload = data[pos + 2 : pos + length]
        pos += length

        if marker == SOS:
            end = _end_of_scan(data, pos)
            kept.append(_segment(marker, payload) + data[pos:end])
            pos = end
            continue

        if marker == APP1:
            if payload.startswith(EXIF_ID):
                orientation = orientation or read_orientation(payload)
                dropped.append("EXIF")
            else:
                dropped.append("XMP" if payload.startswith(b"http://ns.adobe.com/xap/") else "APP1")
            if exif_at is None:
                exif_at = len(kept)
            continue
        if marker == APP2 and not payload.startswith(ICC_PROFILE_ID):
            dropped.append("MPF index" if payload.startswith(b"MPF\x00") else "APP2")
            continue
        if marker == APP13:
            dropped.append("APP13 (Photoshop/IPTC)")
            continue
        if marker == COM:
            dropped.append("comment")
            continue

        kept.append(_segment(marker, payload))

    if not ended:
        raise NotAJpegError("No end-of-image marker")

    frames_dropped = bool(data[pos:].strip(b"\x00"))
    written = orientation if keep_orientation else None
    if written is not None:
        if exif_at is None:
            # after a JFIF segment if the file starts with one, else right after the start-of-image marker
            exif_at = 1 if kept and kept[0][1] == APP0 else 0
        kept.insert(exif_at, _segment(APP1, minimal_exif(written)))

    return StripResult(
        data=b"\xff\xd8" + b"".join(kept) + b"\xff\xd9",
        orientation=written,
        dropped=list(dict.fromkeys(dropped)),
        frames_dropped=frames_dropped,
    )


def _write_atomically(path: Path, data: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mealie.scripts.strip_card_photo",
        description=(
            "Remove GPS, EXIF, XMP and the second MPO frame from a recipe card photo without re-encoding it, keeping "
            "its colour profile and (unless told otherwise) its EXIF orientation."
        ),
    )
    parser.add_argument("input", type=Path, metavar="IN", help="the photo as it came off the phone (JPEG or MPO)")
    parser.add_argument("output", type=Path, metavar="OUT", help="where to write the cleaned JPEG")
    parser.add_argument(
        "--drop-orientation", action="store_true", help="leave out the EXIF orientation too (no EXIF at all)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data = args.input.read_bytes()
        result = strip_jpeg(data, keep_orientation=not args.drop_orientation)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(args.output, result.data)
    except NotAJpegError as e:
        sys.stderr.write(f"{args.input}: {e}. Only JPEG (and iPhone MPO) photos can be cleaned losslessly.\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"{e}\n")
        return 1

    removed = [*result.dropped, *(["the other MPO frames"] if result.frames_dropped else [])]
    orientation = f"orientation {result.orientation} kept" if result.orientation else "no orientation written"
    sys.stdout.write(
        f"Wrote {args.output} ({len(result.data):,} bytes; removed {', '.join(removed) or 'nothing'}; {orientation})\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
