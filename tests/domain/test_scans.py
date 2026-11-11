from datetime import UTC, datetime, timedelta

import pytest

from evbot.domain.scans import ScanKind, ScanRecord

STARTED = datetime(2026, 11, 9, 20, 25, tzinfo=UTC)
FINISHED = STARTED + timedelta(minutes=4, seconds=40)


def test_finished_before_entry_compares_with_the_deadline() -> None:
    on_time = ScanRecord(ScanKind.SCHEDULED, STARTED, FINISHED, (), STARTED + timedelta(minutes=20))
    late = ScanRecord(ScanKind.SCHEDULED, STARTED, FINISHED, (), STARTED + timedelta(minutes=3))

    assert on_time.finished_before_entry is True
    assert late.finished_before_entry is False


def test_finished_before_entry_is_unknown_without_a_deadline() -> None:
    assert ScanRecord(ScanKind.PREVIEW, STARTED, FINISHED, ()).finished_before_entry is None


def test_rejects_a_finish_before_the_start() -> None:
    with pytest.raises(ValueError):
        ScanRecord(ScanKind.SCHEDULED, FINISHED, STARTED, ())


def test_rejects_naive_times() -> None:
    with pytest.raises(ValueError):
        ScanRecord(ScanKind.SCHEDULED, STARTED.replace(tzinfo=None), FINISHED, ())
