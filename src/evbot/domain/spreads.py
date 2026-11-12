from dataclasses import dataclass
from enum import Enum

from evbot.domain.options import OptionContract, OptionQuote


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class Leg:
    contract: OptionContract
    side: Side


@dataclass(frozen=True, slots=True)
class CalendarSpread:
    """Long calendar: sell the front expiry, buy the back expiry, same strike and right."""

    front: Leg
    back: Leg

    def __post_init__(self) -> None:
        f, b = self.front.contract, self.back.contract
        if self.front.side is not Side.SELL or self.back.side is not Side.BUY:
            raise ValueError("a long calendar sells the front leg and buys the back leg")
        if b.expiry <= f.expiry:
            raise ValueError(f"back expiry {b.expiry} must be after front expiry {f.expiry}")
        if (f.underlying, f.strike, f.right) != (b.underlying, b.strike, b.right):
            raise ValueError("calendar legs must share underlying, strike and right")

    @property
    def underlying(self) -> str:
        return self.front.contract.underlying

    @property
    def strike(self) -> float:
        return self.front.contract.strike

    @property
    def back_days(self) -> int:
        return (self.back.contract.expiry - self.front.contract.expiry).days

    def net_debit(self, front_quote: OptionQuote, back_quote: OptionQuote) -> float:
        if front_quote.contract != self.front.contract or back_quote.contract != self.back.contract:
            raise ValueError("quotes do not match the spread's legs")
        return back_quote.mid - front_quote.mid
