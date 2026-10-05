"""
Fork: renders an uploaded PDF's pages as PNG images, in a process of its own (docs/ai/PHASE2.md §2).

`images.expand_document` runs this file with the server's Python in isolated mode, under a time limit, so a hostile
PDF that crashes or hangs PDFium can't take the server with it. It imports nothing of Mealie's and takes the limits it
applies on the command line:

    python -I pdf_render.py <document> <page max side> <max pixels> <max pages> <cpu seconds> <unconfined>

It reads the document into memory, loads PDFium and Pillow, and then confines itself (`sandbox`, before PDFium parses a
byte of the document): no new privileges; no file opened but fonts, read-only (Landlock, where the kernel has it; from
its ABI 4 no TCP connection or listening socket either, from ABI 6 no signal to another process); a seccomp filter
(x86-64 and arm64) refusing sockets, io_uring (whose requests could make sockets past that), new processes (threads
only), every way into another process (`ptrace`, reading or writing its memory, profiling it, taking its file
descriptors through a pidfd, signalling it, changing its resource limits, priority, scheduling or memory placement),
every change to a file short of writing it (removing, renaming, truncating, linking, making one, changing its mode,
owner, times or extended attributes: what Landlock covers only in part, or from a later ABI), and where Landlock didn't
apply, opening any file (PDFium then draws the fonts a PDF doesn't embed with its built-in ones); no file written, few
file descriptors, no new process (`RLIMIT_NPROC`, but for root), and its memory, CPU time (`cpu seconds`) and core
dumps capped. Inherited file descriptors are closed first. **Unconfined, it renders nothing:** where the seccomp filter
didn't apply (another architecture, a kernel or container without seccomp), Landlock alone would still leave it UDP,
TCP below ABI 4, and the server's process within reach, so it answers `{"error": "unconfined"}` without parsing the
document, unless `unconfined` is 1 (`AI_INGEST_PDF_UNCONFINED`).

Each page is rendered upright on white at the resolution it holds, and never below: a scan or a photo (an image covering
at least half the page) at its image's own, the finest of several (a scanner's compact PDF draws a full-resolution text
layer over a downsampled background), so a PDF of 1200 x 1600 photos gives 1200 x 1600 pages as a TIFF of them would;
text and drawings, which have no resolution of their own, at `VECTOR_DPI` at least, over a scan as on their own. Never
more than `page max side` pixels on the long side, nor `max pixels` in all. The result goes to stdout, the only thing it
writes, as frames of a kind byte and an 8-byte big-endian length: a `P` frame holding each page's PNG, in order, then
one `R` frame holding JSON: `{"pages": n}`, or `{"error": "pdf_not_supported"}` (encrypted, empty, damaged or
unrenderable), `{"error": "too_many_pages"}` or `{"error": "unconfined"}`, each with `"sandbox"`: the protections that
applied. A crash, a timeout or a stream without its `R` frame means the same as `pdf_not_supported`.
"""

import ctypes
import io
import json
import math
import os
import struct
import sys
from pathlib import Path
from typing import NamedTuple

MEMORY_LIMIT = 1024 * 1024 * 1024
"""Address space: a page bitmap at the default limits is at most 64 MB"""
OUTPUT_LIMIT = 256 * 1024 * 1024
"""Per page's PNG (the parent refuses a larger frame)"""
OPEN_FILES = 32
"""File descriptors once confined: what Python and PDFium have open, and a font file or two"""
FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts", "/usr/share/X11/fonts", "/usr/X11R6/lib/X11/fonts")
"""What PDFium may read once confined: the fonts a PDF names without embedding them"""

VECTOR_DPI = 300
"""The resolution of text and drawings, which have none of their own: a page with any is rendered at it at least"""
SCAN_SHARE = 0.5
"""An image covering at least this share of a page makes it a scan or a photo"""
MAX_PAGE_OBJECTS = 1000
"""Objects looked at on a page for its images and drawings; a page with more is a drawing"""
MAX_FORM_DEPTH = 2
"""How deep into a page's forms (form XObjects) its images are looked for: a scanner's PDF has them at most one deep"""
POINTS_PER_INCH = 72

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


class _Syscalls(NamedTuple):
    """The system calls an architecture's seccomp filter checks, by number"""

    audit_arch: int
    """`AUDIT_ARCH_*`: any other numbering (a 32-bit call) is refused"""
    x32: bool
    """Whether x32 numbers exist beside the native ones (all refused)"""
    refused: tuple[int, ...]
    """Refused with EPERM"""
    changing: tuple[int, ...]
    """Changing a file short of writing to it, refused with EACCES (Landlock or not)"""
    opening: tuple[int, ...]
    """Opening a file, refused with EACCES where Landlock didn't apply"""
    signalling: tuple[int, ...]
    """Allowed only on itself: their first argument its pid"""
    limiting: tuple[int, ...]
    """Allowed only on itself: their first argument 0 or its pid"""
    clone: int
    """Allowed only for a thread of its own (`CLONE_THREAD` in its first argument, the flags)"""
    clone3: int
    """ENOSYS: its flags are in memory the filter can't read, and the C library then uses `clone`"""


_SECCOMP_ARCHITECTURES = {
    # machine: the numbers from the kernel's system call tables (x86-64: arch/x86/entry/syscalls/syscall_64.tbl;
    # arm64: scripts/syscall.tbl, its "common", "64", "renameat", "rlimit" and "memfd_secret" ABIs), Linux 6.18
    "x86_64": _Syscalls(
        audit_arch=0xC000003E,
        x32=True,
        refused=(
            # sockets: socket, io_uring_setup, io_uring_enter, io_uring_register (its requests make sockets)
            *(41, 425, 426, 427),
            # another process: ptrace, process_vm_readv, process_vm_writev, perf_event_open, pidfd_open, pidfd_getfd,
            # pidfd_send_signal, kcmp, get_robust_list, setpriority, sched_setparam, sched_setscheduler,
            # sched_setaffinity, sched_setattr, ioprio_set, migrate_pages, move_pages, process_madvise, process_mrelease
            *(101, 310, 311, 298, 434, 438, 424, 312, 274, 141, 142, 144, 203, 314, 251, 256, 279, 440, 448),
            # a new process: fork, vfork
            *(57, 58),
        ),
        changing=(
            # truncate, ftruncate, fallocate, rename, renameat, renameat2, mkdir, mkdirat, rmdir, link, linkat, symlink,
            # symlinkat, unlink, unlinkat, mknod, mknodat
            *(76, 77, 285, 82, 264, 316, 83, 258, 84, 86, 265, 88, 266, 87, 263, 133, 259),
            # chmod, fchmod, fchmodat, fchmodat2, chown, fchown, lchown, fchownat, utime, utimes, futimesat, utimensat
            *(90, 91, 268, 452, 92, 93, 94, 260, 132, 235, 261, 280),
            # setxattr, lsetxattr, fsetxattr, setxattrat, removexattr, lremovexattr, fremovexattr, removexattrat,
            # file_setattr
            *(188, 189, 190, 463, 197, 198, 199, 466, 469),
        ),
        opening=(2, 85, 257, 304, 437),  # open, creat, openat, open_by_handle_at, openat2
        signalling=(62, 129, 200, 234, 297),  # kill, rt_sigqueueinfo, tkill, tgkill, rt_tgsigqueueinfo
        limiting=(302,),  # prlimit64 (the C library's setrlimit is prlimit64 on pid 0)
        clone=56,
        clone3=435,
    ),
    "aarch64": _Syscalls(  # no fork, vfork, open, creat or the calls by path without "at"
        audit_arch=0xC00000B7,
        x32=False,
        refused=(
            *(198, 425, 426, 427),
            *(117, 270, 271, 241, 434, 438, 424, 272, 100, 140, 118, 119, 122, 274, 30, 238, 239, 440, 448),
        ),
        changing=(
            # truncate, ftruncate, fallocate, renameat, renameat2, mkdirat, linkat, symlinkat, unlinkat, mknodat
            *(45, 46, 47, 38, 276, 34, 37, 36, 35, 33),
            # fchmod, fchmodat, fchmodat2, fchown, fchownat, utimensat
            *(52, 53, 452, 55, 54, 88),
            # setxattr, lsetxattr, fsetxattr, setxattrat, removexattr, lremovexattr, fremovexattr, removexattrat,
            # file_setattr
            *(5, 6, 7, 463, 14, 15, 16, 466, 469),
        ),
        opening=(56, 265, 437),  # openat, open_by_handle_at, openat2
        signalling=(129, 138, 130, 131, 240),
        limiting=(261,),
        clone=220,
        clone3=435,
    ),
}
_X32_SYSCALL_BIT = 0x40000000
_CLONE_THREAD = 0x00010000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_EPERM = 1
_EACCES = 13
_ENOSYS = 38
_SECCOMP_DATA_ARG0 = 16
"""Where `struct seccomp_data` holds a system call's first argument (64 bits, little-endian on both architectures)"""
_BPF_LD_W_ABS = 0x00 | 0x00 | 0x20
_BPF_JMP_JEQ_K = 0x05 | 0x10 | 0x00
_BPF_JMP_JGE_K = 0x05 | 0x30 | 0x00
_BPF_JMP_JSET_K = 0x05 | 0x40 | 0x00
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


def _filter_program(numbers: _Syscalls, *, files: bool, pid: int) -> list[_SockFilter]:
    """
    The seccomp filter for process `pid` (`_deny_syscalls`), in classic BPF: each jump names the label it goes to,
    resolved here (a jump goes forward only, past at most 255 instructions)
    """
    code: list[tuple[int, int | str, int | str, int]] = []
    labels: dict[str, int] = {}

    def load(offset: int) -> None:  # a 32-bit word of `struct seccomp_data`
        code.append((_BPF_LD_W_ABS, 0, 0, offset))

    def jump(test: int, value: int, true: int | str, false: int | str) -> None:
        code.append((test, true, false, value))

    def answer(value: int) -> None:
        code.append((_BPF_RET_K, 0, 0, value))

    load(4)  # seccomp_data.arch: another architecture's numbering means other system calls
    jump(_BPF_JMP_JEQ_K, numbers.audit_arch, 0, "deny")
    load(0)  # seccomp_data.nr
    if numbers.x32:
        jump(_BPF_JMP_JGE_K, _X32_SYSCALL_BIT, "deny", 0)
    for number in numbers.refused:
        jump(_BPF_JMP_JEQ_K, number, "deny", 0)
    for number in numbers.changing + (numbers.opening if files else ()):
        jump(_BPF_JMP_JEQ_K, number, "refuse", 0)
    jump(_BPF_JMP_JEQ_K, numbers.clone3, "unsupported", 0)
    jump(_BPF_JMP_JEQ_K, numbers.clone, "thread", 0)
    for number in numbers.signalling:
        jump(_BPF_JMP_JEQ_K, number, "itself", 0)
    for number in numbers.limiting:
        jump(_BPF_JMP_JEQ_K, number, "itself or 0", 0)
    answer(_SECCOMP_RET_ALLOW)

    labels["thread"] = len(code)  # clone's flags, its first argument: a thread of this process
    load(_SECCOMP_DATA_ARG0)
    jump(_BPF_JMP_JSET_K, _CLONE_THREAD, "allow", "deny")
    labels["itself or 0"] = len(code)  # a pid, its first argument (64 bits): 0 is the caller
    load(_SECCOMP_DATA_ARG0 + 4)
    jump(_BPF_JMP_JEQ_K, 0, 0, "deny")
    load(_SECCOMP_DATA_ARG0)
    jump(_BPF_JMP_JEQ_K, 0, "allow", "its pid")
    labels["itself"] = len(code)
    load(_SECCOMP_DATA_ARG0 + 4)
    jump(_BPF_JMP_JEQ_K, 0, 0, "deny")
    load(_SECCOMP_DATA_ARG0)
    labels["its pid"] = len(code)
    jump(_BPF_JMP_JEQ_K, pid, "allow", "deny")
    for name, value in (
        ("allow", _SECCOMP_RET_ALLOW),
        ("deny", _SECCOMP_RET_ERRNO | _EPERM),
        ("refuse", _SECCOMP_RET_ERRNO | _EACCES),
        ("unsupported", _SECCOMP_RET_ERRNO | _ENOSYS),
    ):
        labels[name] = len(code)
        answer(value)

    program = []
    for index, (operation, true, false, value) in enumerate(code):
        offsets = [labels[to] - index - 1 if isinstance(to, str) else to for to in (true, false)]
        if not all(0 <= offset <= 255 for offset in offsets):
            raise ValueError("a seccomp filter's jump goes forward, at most 255 instructions")
        program.append(_SockFilter(operation, offsets[0], offsets[1], value))
    return program


def _deny_syscalls(libc: ctypes.CDLL, *, files: bool) -> bool:
    """
    A seccomp filter. Failing with EPERM: `socket` and io_uring (its requests can make sockets without `socket`); what
    reaches into another process (`ptrace`, `process_vm_readv`/`writev`, `perf_event_open`, `pidfd_open`/`pidfd_getfd`,
    which take its open files, `pidfd_send_signal`, `kcmp`, `get_robust_list`, and changing its priority, scheduling,
    I/O priority or memory placement), a signal to or a resource limit of any process but this one (the server is its
    parent, under the same user); a new process (`fork`, `vfork`, `clone` but for a thread; `clone3` fails with ENOSYS,
    so the C library makes a thread with `clone`); and every system call of another architecture's numbering. Failing
    with EACCES: every change to a file short of writing to it (removing, renaming, truncating or allocating, linking,
    making one, changing its mode, owner, times or extended attributes: Landlock has no right for some, and truncating
    only from ABI 3), and with `files`, opening a file by name. Whether it applied.
    """
    numbers = _SECCOMP_ARCHITECTURES.get(os.uname().machine)
    if numbers is None:
        return False
    program = _filter_program(numbers, files=files, pid=os.getpid())
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


def _limit_resources(cpu_seconds: int) -> None:
    """Memory, CPU time and core dumps, from the start (not on Windows: the parent's timeout still applies)"""
    for name, value in (("RLIMIT_AS", MEMORY_LIMIT), ("RLIMIT_CPU", cpu_seconds), ("RLIMIT_CORE", 0)):
        _set_limit(name, value)


def sandbox() -> list[str]:
    """
    Confines this process for the rest of its life, as far as the system allows; the protections that applied. Call it
    once everything needed has been imported and opened (the document read into memory): afterwards no file can be
    opened but a font, and none at all where Landlock didn't apply.
    """
    applied: list[str] = []
    if sys.platform == "linux":
        try:
            libc = _libc()
        except OSError:
            libc = None
        if libc is not None and _no_new_privs(libc):
            applied.append("no-new-privileges")
            landlock = _landlock(libc)
            applied += landlock
            files = not landlock  # without Landlock, seccomp keeps every file closed (fonts included)
            if _deny_syscalls(libc, files=files):
                applied += ["seccomp", "seccomp-files"] if files else ["seccomp"]
    if _set_limit("RLIMIT_FSIZE", 0):  # writing any file is SIGXFSZ: stdout is a pipe
        applied.append("no-file-writes")
    if _set_limit("RLIMIT_NOFILE", OPEN_FILES):
        applied.append(f"open-files-{OPEN_FILES}")
    # its user has more processes than this one (the server): no new one, nor thread (the renderer makes none), but for
    # root, whom the limit doesn't hold (the seccomp filter does)
    if _set_limit("RLIMIT_NPROC", 1) and hasattr(os, "getuid") and os.getuid() != 0:
        applied.append("no-new-processes")
    return applied


def confined(protections: list[str]) -> bool:
    """
    Whether a compromised renderer is confined: the seccomp filter applied (no socket, no new process, nothing of
    another process's, no change to a file), and the files it could read are confined, by Landlock or by the filter.
    Landlock alone isn't enough: it leaves UDP, TCP below ABI 4, and the server's resource limits and priority.
    """
    files = any(item.startswith("landlock-files") or item == "seccomp-files" for item in protections)
    return files and "seccomp" in protections


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


def native_scale(page: object, width: float, height: float) -> float:
    """
    The scale (pixels per PDF unit) that renders a page of `width` x `height` units at the resolution it holds, and
    never below. A scan's or a photo's (an image covering at least `SCAN_SHARE` of the page) is that image's own, along
    its finer axis, whatever it's scaled or turned by; of several such images, the finest (a scanner's compact PDF
    draws its full-resolution text layer, an image mask, over a downsampled background). Text and drawings have no
    resolution of their own: a page holding any (visible text, a path or a shading; an image mask beside other images,
    a compact PDF's text) is rendered at `VECTOR_DPI` at least, as is a page without a scan. Invisible text (an OCR'd
    scan's) doesn't count. Objects in forms count, `MAX_FORM_DEPTH` deep; a page with more than `MAX_PAGE_OBJECTS`
    objects is a drawing.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    scan, drawn, found = 0.0, False, []
    objects = page.get_objects(max_depth=MAX_FORM_DEPTH + 1)  # type: ignore[attr-defined]
    for count in range(MAX_PAGE_OBJECTS + 1):
        try:
            item = next(objects)
            if count == MAX_PAGE_OBJECTS:
                drawn = True
                break
            if item.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
                mode = pdfium_c.FPDFTextObj_GetTextRenderMode(item)
                drawn = drawn or mode != pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE
                continue
            if item.type in (pdfium_c.FPDF_PAGEOBJ_PATH, pdfium_c.FPDF_PAGEOBJ_SHADING):
                drawn = True
                continue
            if item.type != pdfium_c.FPDF_PAGEOBJ_IMAGE or not isinstance(item, pdfium.PdfImage):
                continue
            found.append(item)
            columns, rows = item.get_px_size()
            matrix = item.get_matrix()  # the unit square onto the form it's in, or the page
            container = item.container
            while container is not None:
                matrix = matrix.multiply(container.get_matrix())
                container = container.container
        except StopIteration:
            break
        except pdfium.PdfiumError:
            break  # the objects PDFium can't list: the page is rendered with what was found
        across, down = math.hypot(matrix.a, matrix.b), math.hypot(matrix.c, matrix.d)
        area = abs(matrix.a * matrix.d - matrix.b * matrix.c)
        if columns > 0 and rows > 0 and across > 0 and down > 0 and math.isfinite(area):
            density = max(columns / across, rows / down)
            if area >= SCAN_SHARE * width * height and math.isfinite(density):
                scan = max(scan, density)

    vector = VECTOR_DPI / POINTS_PER_INCH
    if scan <= 0:
        return vector
    if drawn or (len(found) > 1 and any(_image_mask(image) for image in found)):
        return max(scan, vector)
    return scan


def _image_mask(image: object) -> bool:
    """
    Whether a page's image is an image mask (`/ImageMask`: one bit a pixel, painted in the fill colour), what a compact
    PDF's text layer is. Looked at only on a page of several images: PDFium may decode an image to answer.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    try:
        metadata = image.get_metadata()  # type: ignore[attr-defined]
    except pdfium.PdfiumError:
        return False
    return metadata.bits_per_pixel == 1 and metadata.colorspace == pdfium_c.FPDF_COLORSPACE_UNKNOWN


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
                scale = min(native_scale(page, width, height), render_scale(width, height, max_side, max_pixels))
                bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255))
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
    document, max_side, max_pixels, max_pages, cpu_seconds, unconfined = argv
    _limit_resources(int(cpu_seconds))
    data = Path(document).read_bytes()

    # everything rendering needs, loaded while files can still be opened (importing pypdfium2 initializes PDFium)
    import pypdfium2  # noqa: F401
    from PIL import Image

    Image.init()  # its format plugins, PNG's among them

    protections = sandbox()
    if not confined(protections) and unconfined != "1":
        result: dict = {"error": "unconfined"}  # the document is never parsed
    else:
        result = render(data, int(max_side), int(max_pixels), int(max_pages))
    write_frame(RESULT_FRAME, json.dumps({**result, "sandbox": protections}).encode())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
