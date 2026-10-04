"""
Fork: the PDF renderer confines itself before PDFium parses the document (pdf_render.sandbox): no new privileges, no
file opened but fonts and no TCP connection (Landlock); no socket, io_uring, ptrace, other process's memory or signal
but to itself, and without Landlock no file opened at all (seccomp); no file written, few descriptors. Where it can't
be confined, it renders nothing unless `AI_INGEST_PDF_UNCONFINED` allows it. Each check runs in a child process of its
own, as the renderer does, and is skipped where this kernel lacks that protection; "without Landlock" stubs Landlock
out, as on a kernel or in a container without it.
"""

import io
import json
import logging
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PIL import Image

from mealie.schema.recipe_ingest import IngestRejectReason
from mealie.services.ai.ingest import images, limits, pdf_render
from mealie.services.ai.ingest.settings import IngestSettings

SYSCALLS = {
    # the kernel's numbers (x86-64: asm/unistd_64.h; arm64: asm-generic/unistd.h), apart from pdf_render's own table
    "x86_64": {
        "io_uring_setup": 425,
        "ptrace": 101,
        "process_vm_readv": 310,
        "perf_event_open": 298,
        "pidfd_open": 434,
    },
    "aarch64": {
        "io_uring_setup": 425,
        "ptrace": 117,
        "process_vm_readv": 270,
        "perf_event_open": 241,
        "pidfd_open": 434,
    },
}
PROBE = """
import ctypes, errno, json, os, signal, socket, sys
sys.path.insert(0, sys.argv[1])
import pdf_render

outside, port, existing, landlock = sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
numbers = json.loads(sys.argv[6])
if landlock == "stubbed":
    pdf_render._landlock = lambda libc: []  # a kernel or container without Landlock
listening = socket.create_connection  # imported before; the socket below is made before the sandbox too
early = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
written = open(existing, "wb")
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past RLIMIT_FSIZE fails instead of killing the probe
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long

def syscall(name, *args):
    if name not in numbers:
        return "unknown"
    result = libc.syscall(ctypes.c_long(numbers[name]), *args)
    return "allowed" if result >= 0 else errno.errorcode.get(ctypes.get_errno(), "?")

class IoVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]

source, target = ctypes.create_string_buffer(b"memory", 8), ctypes.create_string_buffer(8)
local, remote = IoVec(ctypes.addressof(target), 6), IoVec(ctypes.addressof(source), 6)
def read_own_memory():
    # process_vm_readv on itself: allowed unconfined, whatever ptrace rules apply
    return syscall("process_vm_readv", ctypes.c_long(os.getpid()), ctypes.byref(local), ctypes.c_ulong(1),
                   ctypes.byref(remote), ctypes.c_ulong(1), ctypes.c_ulong(0))

def profile(pid):
    attr = ctypes.create_string_buffer(128)  # struct perf_event_attr: a software counter, its size
    ctypes.memmove(attr, (1).to_bytes(4, "little") + (128).to_bytes(4, "little"), 8)
    return syscall("perf_event_open", attr, ctypes.c_int(pid), ctypes.c_int(-1), ctypes.c_int(-1), ctypes.c_ulong(0))

ring = ctypes.create_string_buffer(120)
seen = {"io_uring-before": syscall("io_uring_setup", ctypes.c_uint32(4), ring),
        "process-memory-before": read_own_memory(),
        "profile-before": profile(os.getpid()),
        "pidfd-before": syscall("pidfd_open", ctypes.c_int(os.getpid()), ctypes.c_uint(0))}

protections = pdf_render.sandbox()
seen["protections"] = protections

def attempt(name, action):
    try:
        action()
        seen[name] = "allowed"
    except OSError as e:
        seen[name] = type(e).__name__

attempt("read-outside", lambda: open(outside, "rb").read())
attempt("create-file", lambda: open(os.path.join(os.path.dirname(outside), "new.txt"), "wb"))
attempt("list-folder", lambda: os.listdir(os.path.dirname(outside)))
attempt("write-open-file", lambda: (written.write(b"x"), written.flush()))
attempt("new-socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
attempt("connect-tcp", lambda: early.connect(("127.0.0.1", port)))
attempt("signal-parent", lambda: os.kill(os.getppid(), 0))  # signal 0: only whether it may
attempt("signal-everyone", lambda: os.kill(-1, 0))
attempt("signal-itself", lambda: os.kill(os.getpid(), 0))
fonts = [os.path.join(root, name) for root, _, names in os.walk("/usr/share/fonts") for name in names][:1]
if fonts:
    attempt("read-font", lambda: open(fonts[0], "rb").read(16))
seen["io_uring"] = syscall("io_uring_setup", ctypes.c_uint32(4), ctypes.create_string_buffer(120))
seen["ptrace-parent"] = syscall("ptrace", ctypes.c_long(0x4206), ctypes.c_long(os.getppid()), None, None)  # SEIZE
seen["process-memory"] = read_own_memory()
seen["profile-parent"] = profile(os.getppid())
seen["pidfd-parent"] = syscall("pidfd_open", ctypes.c_int(os.getppid()), ctypes.c_uint(0))  # then pidfd_getfd
seen["no-new-privs"] = libc.prctl(39, 0, 0, 0, 0)  # PR_GET_NO_NEW_PRIVS
sys.stdout.write(json.dumps(seen))
"""


@pytest.fixture()
def listener() -> Iterator[socket.socket]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    yield server
    server.close()


def _probe(tmp_path: Path, listener: socket.socket, landlock: str) -> dict:
    """What a sandboxed child could still do, with Landlock as the kernel has it or `stubbed` out"""
    if sys.platform != "linux":
        pytest.skip("the renderer is confined on Linux only")
    outside = tmp_path / "secret.txt"
    outside.write_text("the server's secret")
    existing = tmp_path / "existing.bin"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            PROBE,
            str(Path(pdf_render.__file__).parent),
            str(outside),
            str(listener.getsockname()[1]),
            str(existing),
            landlock,
            json.dumps(SYSCALLS.get(os.uname().machine, {})),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    seen = json.loads(completed.stdout)
    assert not (tmp_path / "new.txt").exists()
    assert existing.read_bytes() == b""
    return seen


@pytest.fixture()
def probe(tmp_path: Path, listener: socket.socket) -> dict:
    return _probe(tmp_path, listener, "as-is")


@pytest.fixture(params=["as-is", "stubbed"], ids=["landlock as the kernel has it", "without landlock"])
def either_probe(request: pytest.FixtureRequest, tmp_path: Path, listener: socket.socket) -> dict:
    return _probe(tmp_path, listener, request.param)


def _requires(seen: dict, protection: str) -> None:
    if not any(item.startswith(protection) for item in seen["protections"]):
        pytest.skip(f"this kernel doesn't provide {protection}: {seen['protections']}")


def test_no_file_can_be_opened_but_fonts(probe: dict):
    _requires(probe, "landlock-files")
    assert probe["read-outside"] == "PermissionError"
    assert probe["create-file"] == "PermissionError"
    assert probe["list-folder"] == "PermissionError"
    if "read-font" in probe:
        assert probe["read-font"] == "allowed"


def test_without_landlock_no_file_can_be_opened_at_all(tmp_path: Path, listener: socket.socket):
    # what the renderer could read where Landlock is missing: the server's secrets, its database, the parent's
    # /proc/<pid>/environ. The seccomp filter refuses opening any file, fonts included.
    probe = _probe(tmp_path, listener, "stubbed")
    _requires(probe, "seccomp")
    assert not any(item.startswith("landlock") for item in probe["protections"])
    assert "seccomp-files" in probe["protections"]
    assert probe["read-outside"] == "PermissionError"
    assert probe["create-file"] == "PermissionError"
    assert probe["list-folder"] == "PermissionError"
    if "read-font" in probe:
        assert probe["read-font"] == "PermissionError"
    assert pdf_render.confined(probe["protections"])


def test_no_socket_can_be_made(either_probe: dict):
    _requires(either_probe, "seccomp")
    assert either_probe["new-socket"] == "PermissionError"


def test_no_io_uring_ptrace_or_other_process_memory(either_probe: dict):
    # io_uring's requests make sockets (and open files) without the system calls the filter refuses; ptrace,
    # process_vm_readv, perf_event_open (sampling its stack) and pidfd_getfd (taking its open files: the database's)
    # would reach into the server, the renderer's parent, where Landlock doesn't stop them
    _requires(either_probe, "seccomp")
    if either_probe["io_uring-before"] == "allowed":
        assert either_probe["io_uring"] == "EPERM"
    assert either_probe["process-memory-before"] == "allowed"
    assert either_probe["process-memory"] == "EPERM"
    assert either_probe["ptrace-parent"] == "EPERM"
    if either_probe["profile-before"] == "allowed":
        assert either_probe["profile-parent"] == "EPERM"
    if either_probe["pidfd-before"] == "allowed":
        assert either_probe["pidfd-parent"] == "EPERM"


def test_no_signal_but_to_itself(either_probe: dict):
    # the server is the renderer's parent, in its process group, under the same user: `kill(-1, SIGKILL)` would stop it
    # (Landlock scopes signals only from ABI 6)
    _requires(either_probe, "seccomp")
    assert either_probe["signal-parent"] == "PermissionError"
    assert either_probe["signal-everyone"] == "PermissionError"
    assert either_probe["signal-itself"] == "allowed"


def test_no_tcp_connection_can_be_made(probe: dict):
    _requires(probe, "landlock-tcp")
    assert probe["connect-tcp"] == "PermissionError"


def test_no_file_can_be_written_and_privileges_cant_grow(probe: dict):
    assert "no-file-writes" in probe["protections"]
    assert probe["write-open-file"] == "OSError"  # EFBIG
    _requires(probe, "no-new-privileges")
    assert probe["no-new-privs"] == 1


def test_the_renderer_reports_its_protections_once(monkeypatch: pytest.MonkeyPatch):
    logged: list[str] = []
    monkeypatch.setattr(images.logger, "info", logged.append)
    monkeypatch.setattr(images.logger, "warning", logged.append)
    monkeypatch.setattr(images.logger, "error", logged.append)
    monkeypatch.setattr(images, "_sandbox_logged", False)
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 200)
    for _ in range(2):
        buffer = io.BytesIO()
        Image.new("RGB", (60, 40), (200, 0, 0)).save(buffer, format="PDF")
        images.close_pages(images.expand_document(io.BytesIO(buffer.getvalue())))

    [message] = [line for line in logged if "PDF pages are rendered" in line]
    assert "no-file-writes" in message
    if sys.platform == "linux":
        assert "no-new-privileges" in message


# ==========================================
# Rendering where the system confines it less, or not at all


@pytest.fixture()
def renderer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """The real renderer, run with the protections a test names stubbed out (as on a system without them)"""

    def install(*stubbed: str) -> None:
        stubs = {
            "landlock": "pdf_render._landlock = lambda libc: []",
            "seccomp": "pdf_render._deny_syscalls = lambda libc, files: False",
        }
        script = tmp_path / "pdf_render_stubbed.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(Path(pdf_render.__file__).parent)!r})\n"
            "import pdf_render\n"
            + "".join(stubs[name] + "\n" for name in stubbed)
            + "sys.exit(pdf_render.main(sys.argv[1:]))\n"
        )
        monkeypatch.setattr(images, "_PDF_RENDERER", script)

    return install


@pytest.fixture()
def logged(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for level, name in ((logging.INFO, "info"), (logging.WARNING, "warning"), (logging.ERROR, "error")):
        monkeypatch.setattr(images.logger, name, lambda message, level=level: lines.append((level, message)))
    monkeypatch.setattr(images, "_sandbox_logged", False)
    return lines


def _unconfined(monkeypatch: pytest.MonkeyPatch, allowed: bool) -> None:
    configured = IngestSettings(PDF_UNCONFINED=allowed, WORKER=False)
    monkeypatch.setattr(images, "get_ingest_settings", lambda: configured)


def _text_pdf(base_font: bytes, subtype: bytes = b"Type1") -> bytes:
    """A one-page PDF of text in a font it doesn't embed (PDFium looks for it among the system's fonts)"""
    content = b"BT /F1 40 Tf 60 700 Td (Grandma's Apple Pie) Tj 0 -60 Td (2 cups flour) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /%s /BaseFont /%s /Encoding /WinAnsiEncoding >>" % (subtype, base_font),
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    data += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(data)


def _scanned_pdf() -> bytes:
    """Two pages that are each a scanned photo (JPEG), as a scanner's PDF holds them"""
    pages = [Image.frombytes("RGB", (300, 400), os.urandom(300 * 400 * 3)) for _ in range(2)]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:], resolution=150)
    return buffer.getvalue()


def _ink(page: images.DocumentPage) -> int:
    """How many of a rendered page's pixels are dark: its text"""
    with Image.open(page.file) as image:
        return sum(image.convert("L").point(lambda value: 255 if value < 128 else 0).histogram()[255:])


@pytest.mark.skipif(sys.platform != "linux" or os.uname().machine not in SYSCALLS, reason="seccomp on Linux only")
def test_without_landlock_pdfs_still_render_their_fonts_and_scans(
    renderer: Callable[..., None], logged: list[tuple[int, str]], monkeypatch: pytest.MonkeyPatch
):
    # no file can be opened, the system's fonts included: PDFium draws the fonts a PDF doesn't embed with its own
    renderer("landlock")
    _unconfined(monkeypatch, False)
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 600)
    for raw in (
        _text_pdf(b"Helvetica"),
        _text_pdf(b"LiberationSerif", b"TrueType"),
        _text_pdf(b"Georgia", b"TrueType"),
    ):
        pages = images.expand_document(io.BytesIO(raw))
        try:
            [page] = pages
            assert _ink(page) > 1000
        finally:
            images.close_pages(pages)
    pages = images.expand_document(io.BytesIO(_scanned_pdf()))
    assert len(pages) == 2
    images.close_pages(pages)

    [(level, message)] = logged
    assert level == logging.INFO and "without Landlock" in message and "seccomp-files" in message


def test_unconfined_pdfs_are_refused_unless_allowed(
    renderer: Callable[..., None], logged: list[tuple[int, str]], monkeypatch: pytest.MonkeyPatch
):
    # neither Landlock nor the seccomp filter (another architecture, a container without them): nothing is rendered
    renderer("landlock", "seccomp")
    _unconfined(monkeypatch, False)
    monkeypatch.setattr(limits, "PAGE_MAX_SIDE", 300)
    for _ in range(2):
        with pytest.raises(images.PageRejected) as e:
            images.expand_document(io.BytesIO(_scanned_pdf()))
        assert e.value.reason == IngestRejectReason.pdf_not_supported
        assert not isinstance(e.value, images.RenderTimedOut)
    [(level, message)] = logged  # one clear line, once
    assert level == logging.ERROR and "AI_INGEST_PDF_UNCONFINED=true" in message

    # the setting renders them with the interpreter's isolation and the limits only, and says so
    logged.clear()
    monkeypatch.setattr(images, "_sandbox_logged", False)
    _unconfined(monkeypatch, True)
    pages = images.expand_document(io.BytesIO(_scanned_pdf()))
    assert len(pages) == 2
    images.close_pages(pages)
    [(level, message)] = logged
    assert level == logging.WARNING and "AI_INGEST_PDF_UNCONFINED" in message


def test_an_unconfined_renderer_never_parses_the_document(tmp_path: Path):
    # `main` answers before PDFium opens the document: a file that isn't a PDF at all gets "unconfined", not a
    # rendering error
    document = tmp_path / "document.pdf"
    document.write_bytes(b"not a pdf")
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(pdf_render.__file__).parent)!r})\n"
        "import pdf_render\n"
        "pdf_render._landlock = lambda libc: []\n"
        "pdf_render._deny_syscalls = lambda libc, files: False\n"
        "sys.exit(pdf_render.main(sys.argv[1:]))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(document), "100", "1000000", "4", "20", "0"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout[:1] == pdf_render.RESULT_FRAME
    assert json.loads(completed.stdout[pdf_render.FRAME_HEADER :])["error"] == "unconfined"
