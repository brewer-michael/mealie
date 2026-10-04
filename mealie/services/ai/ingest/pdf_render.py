"""
Fork: renders an uploaded PDF's pages as PNG files, in a process of its own (docs/ai/PHASE2.md §2).

`images.expand_document` runs this file with the server's Python in isolated mode, under a time limit, so a hostile
PDF that crashes or hangs PDFium can't take the server with it; here its memory, CPU time and output are capped too.
It imports nothing of Mealie's and takes the limits it applies on the command line:

    python -I pdf_render.py <document> <output folder> <page max side> <max pixels> <max pages>

Each page is rendered upright on white, its long side `page max side` pixels (fewer when `max pixels` needs it), and
written as `page-<n>.png` (from 1). The last line written to stdout is JSON: `{"pages": n}`, or
`{"error": "pdf_not_supported"}` (encrypted, empty, damaged or unrenderable) or `{"error": "too_many_pages"}`. A crash
or a timeout means the same as `pdf_not_supported`.
"""

import json
import math
import sys
from pathlib import Path

MEMORY_LIMIT = 1024 * 1024 * 1024
"""Address space: a page bitmap at the default limits is at most 64 MB"""
OUTPUT_LIMIT = 256 * 1024 * 1024
"""Per written file"""
CPU_SECONDS = 60


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:  # not on Windows; the parent's timeout still applies
        return
    for limit, value in (
        (resource.RLIMIT_AS, MEMORY_LIMIT),
        (resource.RLIMIT_CPU, CPU_SECONDS),
        (resource.RLIMIT_FSIZE, OUTPUT_LIMIT),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            _, hard = resource.getrlimit(limit)
            resource.setrlimit(limit, (value if hard == resource.RLIM_INFINITY else min(value, hard), hard))
        except ValueError, OSError:
            pass


def render_scale(width: float, height: float, max_side: int, max_pixels: int) -> float:
    """
    The scale (pixels per PDF unit) that gives a page of `width` x `height` units a long side of `max_side` pixels,
    smaller when its rendering (PDFium rounds each side up) would have more than `max_pixels`
    """
    scale = min(max_side / max(width, height), math.sqrt(max_pixels / (width * height)))
    while scale > 0 and math.ceil(width * scale) * math.ceil(height * scale) > max_pixels:
        scale *= 0.99
    return scale


def render(document: Path, output: Path, max_side: int, max_pixels: int, max_pages: int) -> dict:
    """Renders `document`'s pages into `output`; the result to print"""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(document)
    except pdfium.PdfiumError:
        return {"error": "pdf_not_supported"}  # damaged, or it needs a password

    try:
        count = len(pdf)
        if count < 1:
            return {"error": "pdf_not_supported"}
        if count > max_pages:
            return {"error": "too_many_pages"}

        for number in range(1, count + 1):
            page = pdf[number - 1]
            try:
                width, height = page.get_size()  # in PDF units, with the page's rotation applied
                if not (width > 0 and height > 0 and math.isfinite(width * height)):
                    return {"error": "pdf_not_supported"}
                bitmap = page.render(
                    scale=render_scale(width, height, max_side, max_pixels), fill_color=(255, 255, 255, 255)
                )
                image = bitmap.to_pil().convert("RGB")
                image.save(output / f"page-{number}.png", format="PNG", compress_level=1)
                image.close()
                bitmap.close()
            finally:
                page.close()
    except pdfium.PdfiumError:
        return {"error": "pdf_not_supported"}
    finally:
        pdf.close()
    return {"pages": count}


def main(argv: list[str]) -> int:
    _limit_resources()
    document, output, max_side, max_pixels, max_pages = argv
    result = render(Path(document), Path(output), int(max_side), int(max_pixels), int(max_pages))
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
