from datetime import date
from typing import Protocol

from evbot.domain.orders import OrderTicket
from evbot.domain.scans import ScanRecord
from evbot.domain.trades import Position


class TradeRepository(Protocol):
    def save_scan(self, scan: ScanRecord) -> None: ...

    def scans_on(self, day: date) -> tuple[ScanRecord, ...]:
        """Scans that started on day in New York time, oldest first."""
        ...

    def save_order(self, ticket: OrderTicket) -> None:
        """Appends this state of the order; every change is kept."""
        ...

    def orders_on(self, day: date) -> tuple[OrderTicket, ...]:
        """Order states saved for orders submitted on day (New York), in saving order."""
        ...

    def save_position(self, position: Position) -> None:
        """Stores the position's current state, replacing any earlier one with its id."""
        ...

    def open_positions(self) -> tuple[Position, ...]:
        """Positions with no exit fill yet, including those marked exit_failed."""
        ...
