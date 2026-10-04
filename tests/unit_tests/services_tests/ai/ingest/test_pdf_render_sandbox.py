"""
Fork: the PDF renderer confines itself before PDFium parses the document (pdf_render.sandbox): no new privileges, no
file opened but fonts and no TCP connection (Landlock), no socket (seccomp), no file written, few descriptors. Each
check runs in a child process of its own, as the renderer does, and is skipped where this kernel lacks that protection.
"""

import io
import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from mealie.services.ai.ingest import images, limits, pdf_render

PROBE = """
import ctypes, json, os, signal, socket, sys
sys.path.insert(0, sys.argv[1])
import pdf_render

outside, port, existing = sys.argv[2], int(sys.argv[3]), sys.argv[4]
listening = socket.create_connection  # imported before; the socket below is made before the sandbox too
early = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
written = open(existing, "wb")
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past RLIMIT_FSIZE fails instead of killing the probe

protections = pdf_render.sandbox()
seen = {"protections": protections}

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
fonts = [os.path.join(root, name) for root, _, names in os.walk("/usr/share/fonts") for name in names][:1]
if fonts:
    attempt("read-font", lambda: open(fonts[0], "rb").read(16))
libc = ctypes.CDLL(None)
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


@pytest.fixture()
def probe(tmp_path: Path, listener: socket.socket) -> dict:
    """What a sandboxed child could still do"""
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


def test_no_socket_can_be_made(probe: dict):
    _requires(probe, "seccomp-sockets")
    assert probe["new-socket"] == "PermissionError"


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
