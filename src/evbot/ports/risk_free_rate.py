from datetime import date
from typing import Protocol


class RiskFreeRateProvider(Protocol):
    name: str

    def rate_on(self, day: date) -> float:
        """Annualized rate as a decimal (0.0425 for 4.25%), the latest value on or before day.

        Date-based so a backtest can ask for historical days. Raises RiskFreeRateError.
        """
        ...
