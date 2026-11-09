from datetime import date
from typing import Protocol

from evbot.domain.events import EarningsEvent


class EarningsCalendar(Protocol):
    def events_on(self, day: date) -> tuple[EarningsEvent, ...]: ...
