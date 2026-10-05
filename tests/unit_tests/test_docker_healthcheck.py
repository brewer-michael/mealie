"""
docker/healthcheck.sh, which the image's HEALTHCHECK runs (fork hook): while a backup restore runs, every `/api/`
request answers 503 `paused_for_restore` (`restore_guard`), the about page it asks for too. The server is up, so that
is healthy, or an orchestrator that restarts unhealthy containers would kill the restore halfway. Any other error, or
no answer, still fails.
"""

import http.server
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from mealie.services.ai.ingest import restore_guard

SCRIPT = Path(__file__).parents[2] / "docker" / "healthcheck.sh"

pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("curl")), reason="runs bash and curl")


def _paused_body() -> bytes:
    """What the restore guard answers the health check with"""
    scope = {"type": "http", "method": "GET", "path": "/api/app/about", "headers": []}
    return bytes(restore_guard.paused_response(scope).body)


class _Server:
    """Answers every GET with `status` and `body`, and records the paths it was asked for"""

    def __init__(self, status: int, body: bytes) -> None:
        self.paths: list[str] = []
        paths = self.paths

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                paths.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _healthcheck(port: int) -> int:
    env = {key: value for key, value in os.environ.items() if not key.startswith("TLS_")}
    env["API_PORT"] = str(port)
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, timeout=60).returncode


@pytest.mark.parametrize(
    ("status", "body", "healthy"),
    [
        (200, b'{"production":true,"version":"v3.28.0"}', True),
        (503, _paused_body(), True),
        (503, b'{"detail":"Service Unavailable"}', False),
        (500, b"Internal Server Error", False),
        (404, _paused_body(), False),
    ],
    ids=["up", "restoring", "another 503", "500", "paused text on another status"],
)
def test_the_health_check_counts_a_restore_as_healthy(status: int, body: bytes, healthy: bool):
    with _Server(status, body) as server:
        code = _healthcheck(server.port)

    assert server.paths == ["/api/app/about"]
    assert (code == 0) is healthy, f"exit code {code}"


def test_no_answer_is_unhealthy():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens on it

    assert _healthcheck(port) != 0
