"""
A page's turn lock (`review.page_turn_lock`, docs/ai/PHASE2.md §4.4): one turn of a page at a time, across threads and
worker processes, with a deadline, and still within a process where the filesystem has no locks.
"""

import errno
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mealie.services.ai.ingest import review
from mealie.services.ai.ingest.review import TURN_LOCK_FILE, page_turn_lock


def test_one_thread_at_a_time(tmp_path: Path):
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with page_turn_lock(tmp_path):
            held.set()
            assert release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(10)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with page_turn_lock(tmp_path, wait=0.2):
            pass
    assert time.monotonic() - started >= 0.2

    release.set()
    holder.join(10)
    with page_turn_lock(tmp_path, wait=1):
        pass
    assert (tmp_path / TURN_LOCK_FILE).is_file()
    assert review._turn_gates == {}  # nothing kept once no thread wants the page


def test_another_pages_lock_is_independent(tmp_path: Path):
    first, second = tmp_path / "0", tmp_path / "1"
    first.mkdir()
    second.mkdir()
    with page_turn_lock(first), page_turn_lock(second, wait=0.2):
        pass


def test_a_waiting_thread_gets_the_lock_when_it_is_released(tmp_path: Path):
    order: list[str] = []
    held = threading.Event()

    def hold() -> None:
        with page_turn_lock(tmp_path):
            held.set()
            time.sleep(0.3)
            order.append("first")

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(10)
    with page_turn_lock(tmp_path, wait=10):
        order.append("second")
    holder.join(10)
    assert order == ["first", "second"]


def test_another_process_holding_it_is_waited_for(tmp_path: Path):
    script = (
        "import fcntl, os, sys\n"
        f"fd = os.open({str(tmp_path / TURN_LOCK_FILE)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "sys.stdin.read()\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    ) as other:
        assert other.stdin is not None and other.stdout is not None
        try:
            assert other.stdout.readline().strip() == "held"
            with pytest.raises(TimeoutError):
                with page_turn_lock(tmp_path, wait=0.3):
                    pass
        finally:
            other.stdin.close()  # the other process ends, and its lock with it
            other.wait(10)
    with page_turn_lock(tmp_path, wait=1):
        pass


def test_without_file_locks_a_process_still_turns_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(review.fcntl, "flock", no_locks)
    monkeypatch.setattr(review, "_turn_lock_warned", False)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with page_turn_lock(tmp_path):
            held.set()
            assert release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(10)
    with pytest.raises(TimeoutError):
        with page_turn_lock(tmp_path, wait=0.2):
            pass
    release.set()
    holder.join(10)
    with page_turn_lock(tmp_path):
        pass
    assert review._turn_lock_warned is True


def test_other_lock_errors_are_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def broken(fd: int, operation: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(review.fcntl, "flock", broken)
    with pytest.raises(OSError, match="I/O error"):
        with page_turn_lock(tmp_path):
            pass
    assert review._turn_gates == {}


def test_a_page_whose_directory_is_gone(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        with page_turn_lock(tmp_path / "gone"):
            pass
    assert review._turn_gates == {}


def test_an_error_inside_releases_it(tmp_path: Path):
    with pytest.raises(RuntimeError):
        with page_turn_lock(tmp_path):
            raise RuntimeError("stop")
    with page_turn_lock(tmp_path, wait=0.2):
        pass
