from datetime import date
from typing import Protocol

from evbot.domain.scans import ScanRecord


class TradeRepository(Protocol):
    def save_scan(self, scan: ScanRecord) -> None: ...

    def scans_on(self, day: date) -> tuple[ScanRecord, ...]:
        """Scans that started on day in New York time, oldest first."""
        ...
