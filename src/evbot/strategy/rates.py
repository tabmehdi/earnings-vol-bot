from dataclasses import dataclass
from datetime import date
from typing import Protocol


class RateSource(Protocol):
    def rate_for(self, day: date) -> float: ...


@dataclass(frozen=True, slots=True)
class ConstantRate:
    rate: float

    def rate_for(self, day: date) -> float:
        return self.rate
