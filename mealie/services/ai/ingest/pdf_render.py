"""
Fork: renders an uploaded PDF's pages as PNG images, in a process of its own (docs/ai/PHASE2.md §2).

`images.expand_document` runs this file with the server's Python in isolated mode, under a time limit, so a hostile
PDF that crashes or hangs PDFium can't take the server with it. It imports nothing of Mealie's and takes the limits it
applies on the command line:

    python -I pdf_render.py <document> <page max side> <max pixels> <max pages>

It reads the document, loads PDFium and Pillow, and then confines itself (`sandbox`, best effort, before PDFium parses
a byte of the document): no new privileges; no file opened but fonts, read-only, and no TCP connection or listening
socket (Landlock, where the kernel has it); no socket at all (seccomp, on x86-64 and arm64); no file written, few file
descriptors, and its memory, CPU time and core dumps capped. Inherited file descriptors are closed first.

Each page is rendered upright on white, its long side `page max side` pixels (fewer when `max pixels` needs it). The
result goes to stdout, the only thing it writes, as frames of a kind byte and an 8-byte big-endian length: a `P` frame
holding each page's PNG, in order, then one `R` frame holding JSON: `{"pages": n}`, or `{"error": "pdf_not_supported"}`
(encrypted, empty, damaged or unrenderable) or `{"error": "too_many_pages"}`, each with `"sandbox"`: the protections
that applied. A crash, a timeout or a stream without its `R` frame means the same as `pdf_not_supported`.
"""

import ctypes
import io
import json
import math
import os
import struct
import sys
from pathlib import Path

MEMORY_LIMIT = 1024 * 1024 * 1024
"""Address space: a page bitmap at the default limits is at most 64 MB"""
OUTPUT_LIMIT = 256 * 1024 * 1024
"""Per page's PNG (the parent refuses a larger frame)"""
CPU_SECONDS = 60
OPEN_FILES = 32
"""File descriptors once confined: what Python and PDFium have open, and a font file or two"""
FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts", "/usr/share/X11/fonts", "/usr/X11R6/lib/X11/fonts")
"""What PDFium may read once confined: the fonts a PDF names without embedding them"""

PAGE_FRAME = b"P"
RESULT_FRAME = b"R"
FRAME_HEADER = 9
"""A frame's kind byte and its 8-byte big-endian length"""


# ==========================================
# Confinement (Linux; each part is skipped where the kernel or architecture lacks it)

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
"""The same numbers on every architecture"""
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3

_SECCOMP_ARCHITECTURES = {
    # machine: (AUDIT_ARCH_*, the `socket` system call's number, whether x32 numbers exist beside the native ones)
    "x86_64": (0xC000003E, 41, True),
    "aarch64": (0xC00000B7, 198, False),
}
_X32_SYSCALL_BIT = 0x40000000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_EPERM = 1
_BPF_LD_W_ABS = 0x00 | 0x00 | 0x20
_BPF_JMP_JEQ_K = 0x05 | 0x10 | 0x00
_BPF_JMP_JGE_K = 0x05 | 0x30 | 0x00
_BPF_RET_K = 0x06 | 0x00


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def _no_new_privs(libc: ctypes.CDLL) -> bool:
    """Nothing this process runs gains privileges (setuid, file capabilities); Landlock and seccomp need it"""
    one, zero = ctypes.c_ulong(1), ctypes.c_ulong(0)
    return libc.prctl(_PR_SET_NO_NEW_PRIVS, one, zero, zero, zero) == 0


def _landlock(libc: ctypes.CDLL) -> list[str]:
    """
    Every file access but reading fonts denied, and with ABI 4 TCP connections and listening, with ABI 6 abstract
    Unix sockets and signals to other processes: what the kernel's Landlock version handles. The protections applied.
    """
    abi = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET),
        ctypes.c_void_p(None),
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if abi < 1:
        return []
    handled_fs = (1 << 13) - 1  # ABI 1: execute, write, read, read dir, remove and make each kind of file
    handled_fs |= (1 << 13) if abi >= 2 else 0  # refer (link or rename across folders)
    handled_fs |= (1 << 14) if abi >= 3 else 0  # truncate
    handled_fs |= (1 << 15) if abi >= 5 else 0  # ioctl on devices
    attr = _RulesetAttr(handled_fs, 0b11 if abi >= 4 else 0, 0b11 if abi >= 6 else 0)
    size = 8 if abi < 4 else 16 if abi < 6 else 24  # what this ABI's struct holds: a larger one is refused
    ruleset = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0)
    )
    if ruleset < 0:
        return []
    try:
        for path in FONT_DIRS:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                # struct landlock_path_beneath_attr, packed: the access allowed beneath the folder, and the folder
                rule = ctypes.create_string_buffer(
                    struct.pack("=Qi", _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_READ_DIR, fd), 12
                )
                libc.syscall(
                    ctypes.c_long(_SYS_LANDLOCK_ADD_RULE),
                    ctypes.c_long(ruleset),
                    ctypes.c_long(_LANDLOCK_RULE_PATH_BENEATH),
                    rule,
                    ctypes.c_uint32(0),
                )
            finally:
                os.close(fd)
        if libc.syscall(ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_long(ruleset), ctypes.c_uint32(0)) != 0:
            return []
    finally:
        os.close(ruleset)
    applied = [f"landlock-files (ABI {abi})"]
    if abi >= 4:
        applied.append("landlock-tcp")
    if abi >= 6:
        applied.append("landlock-scope")
    return applied


def _deny_sockets(libc: ctypes.CDLL) -> bool:
    """A seccomp filter: `socket` fails with EPERM, and so does every system call of another architecture's numbering"""
    known = _SECCOMP_ARCHITECTURES.get(os.uname().machine)
    if known is None:
        return False
    arch, socket_nr, has_x32 = known
    deny = _SECCOMP_RET_ERRNO | _EPERM

    program = [
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 4),  # seccomp_data.arch
        _SockFilter(_BPF_JMP_JEQ_K, 1, 0, arch),
        _SockFilter(_BPF_RET_K, 0, 0, deny),
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    if has_x32:
        program += [_SockFilter(_BPF_JMP_JGE_K, 0, 1, _X32_SYSCALL_BIT), _SockFilter(_BPF_RET_K, 0, 0, deny)]
    program += [
        _SockFilter(_BPF_JMP_JEQ_K, 0, 1, socket_nr),
        _SockFilter(_BPF_RET_K, 0, 0, deny),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
    ]
    filters = (_SockFilter * len(program))(*program)
    fprog = _SockFprog(len(program), filters)
    zero = ctypes.c_ulong(0)
    return libc.prctl(_PR_SET_SECCOMP, ctypes.c_ulong(_SECCOMP_MODE_FILTER), ctypes.byref(fprog), zero, zero) == 0


def _close_inherited_fds() -> None:
    """Every descriptor but stdin, stdout and stderr: the parent's files and sockets have no business here"""
    try:
        import resource

        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except ImportError, ValueError, OSError:
        soft = 1024
    os.closerange(3, soft if soft != -1 and soft < 1 << 20 else 1 << 20)


def _set_limit(name: str, value: int) -> bool:
    try:
        import resource

        limit = getattr(resource, name)
        _, hard = resource.getrlimit(limit)
        value = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(limit, (value, value))  # the hard limit too: it can't be raised again
    except ImportError, AttributeError, ValueError, OSError:
        return False
    return True


def _limit_resources() -> None:
    """Memory, CPU time and core dumps, from the start (not on Windows: the parent's timeout still applies)"""
    for name, value in (("RLIMIT_AS", MEMORY_LIMIT), ("RLIMIT_CPU", CPU_SECONDS), ("RLIMIT_CORE", 0)):
        _set_limit(name, value)


def sandbox() -> list[str]:
    """
    Confines this process for the rest of its life, as far as the system allows; the protections that applied. Call it
    once everything needed has been imported and opened: afterwards no file can be opened but a font.
    """
    applied: list[str] = []
    if sys.platform == "linux":
        try:
            libc = _libc()
        except OSError:
            libc = None
        if libc is not None and _no_new_privs(libc):
            applied.append("no-new-privileges")
            applied += _landlock(libc)
            if _deny_sockets(libc):
                applied.append("seccomp-sockets")
    if _set_limit("RLIMIT_FSIZE", 0):  # writing any file is SIGXFSZ: stdout is a pipe
        applied.append("no-file-writes")
    if _set_limit("RLIMIT_NOFILE", OPEN_FILES):
        applied.append(f"open-files-{OPEN_FILES}")
    return applied


# ==========================================
# Rendering


def render_scale(width: float, height: float, max_side: int, max_pixels: int) -> float:
    """
    The scale (pixels per PDF unit) that gives a page of `width` x `height` units a long side of `max_side` pixels,
    smaller when its rendering (PDFium rounds each side up) would have more than `max_pixels`
    """
    scale = min(max_side / max(width, height), math.sqrt(max_pixels / (width * height)))
    while scale > 0 and math.ceil(width * scale) * math.ceil(height * scale) > max_pixels:
        scale *= 0.99
    return scale


def write_frame(kind: bytes, data: bytes) -> None:
    out = sys.stdout.buffer
    out.write(kind + len(data).to_bytes(8, "big"))
    out.write(data)
    out.flush()


def render(data: bytes, max_side: int, max_pixels: int, max_pages: int) -> dict:
    """Renders the document `data` holds, each page's PNG written as a frame; the result"""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
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
                png = io.BytesIO()
                image.save(png, format="PNG", compress_level=1)
                image.close()
                bitmap.close()
                if png.tell() > OUTPUT_LIMIT:
                    return {"error": "pdf_not_supported"}
                write_frame(PAGE_FRAME, png.getvalue())
            finally:
                page.close()
    except pdfium.PdfiumError:
        return {"error": "pdf_not_supported"}
    finally:
        pdf.close()
    return {"pages": count}


def main(argv: list[str]) -> int:
    _close_inherited_fds()
    _limit_resources()
    document, max_side, max_pixels, max_pages = argv
    data = Path(document).read_bytes()

    # everything rendering needs, loaded while files can still be opened
    import pypdfium2  # noqa: F401
    from PIL import Image

    Image.init()  # its format plugins, PNG's among them

    protections = sandbox()
    result = render(data, int(max_side), int(max_pixels), int(max_pages))
    write_frame(RESULT_FRAME, json.dumps({**result, "sandbox": protections}).encode())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
