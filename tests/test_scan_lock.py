import os
import subprocess
import sys
from pathlib import Path

import pytest

from evbot.scan_lock import ScanLock


def test_only_one_holder_at_a_time(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "scan_2026-11-09.lock"
    first, second = ScanLock(path), ScanLock(path)

    assert first.acquire()
    assert not second.acquire()


def test_release_lets_the_next_scan_in(tmp_path: Path) -> None:
    path = tmp_path / "scan.lock"
    first = ScanLock(path)
    first.acquire()

    first.release()

    assert ScanLock(path).acquire()


def test_release_without_holding_leaves_another_holders_lock(tmp_path: Path) -> None:
    path = tmp_path / "scan.lock"
    holder, other = ScanLock(path), ScanLock(path)
    holder.acquire()
    other.acquire()

    other.release()

    assert path.exists()


@pytest.mark.skipif(os.name != "posix", reason="process liveness is only checked on Linux")
def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path: Path) -> None:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    path = tmp_path / "session.lock"
    path.write_text(str(finished.pid), encoding="utf-8")

    assert ScanLock(path).acquire()


def test_a_lock_with_unreadable_contents_is_respected(tmp_path: Path) -> None:
    path = tmp_path / "session.lock"
    path.write_text("not a pid", encoding="utf-8")

    assert not ScanLock(path).acquire()


def test_a_lock_held_by_a_live_process_is_respected(tmp_path: Path) -> None:
    path = tmp_path / "session.lock"
    path.write_text(str(os.getpid()), encoding="utf-8")

    assert not ScanLock(path).acquire()
