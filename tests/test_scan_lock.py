from pathlib import Path

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
