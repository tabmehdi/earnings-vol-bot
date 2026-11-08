import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class OptionRight(Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True, slots=True)
class OptionContract:
    underlying: str
    expiry: date
    strike: float
    right: OptionRight
    multiplier: int = 100

    def __post_init__(self) -> None:
        if not self.underlying:
            raise ValueError("underlying must not be empty")
        if self.strike <= 0:
            raise ValueError(f"strike must be positive, got {self.strike}")
        if self.multiplier <= 0:
            raise ValueError(f"multiplier must be positive, got {self.multiplier}")


@dataclass(frozen=True, slots=True)
class OptionQuote:
    contract: OptionContract
    bid: float
    ask: float
    iv: float | None
    open_interest: int
    volume: int

    def __post_init__(self) -> None:
        if self.bid < 0:
            raise ValueError(f"bid must not be negative, got {self.bid}")
        if self.bid > self.ask:
            raise ValueError(f"bid {self.bid} is above ask {self.ask}")
        if self.iv is not None and self.iv < 0:
            raise ValueError(f"iv must not be negative, got {self.iv}")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        """Infinite for a zero mid, so an unquoted leg always fails a liquidity check."""
        mid = self.mid
        return math.inf if mid == 0 else (self.ask - self.bid) / mid


@dataclass(frozen=True, slots=True)
class OptionChain:
    underlying: str
    spot: float
    as_of: datetime
    quotes: tuple[OptionQuote, ...]

    def __post_init__(self) -> None:
        if self.spot <= 0:
            raise ValueError(f"spot must be positive, got {self.spot}")
        seen: set[OptionContract] = set()
        for q in self.quotes:
            if q.contract.underlying != self.underlying:
                raise ValueError(f"quote for {q.contract.underlying} in {self.underlying} chain")
            if q.contract in seen:
                raise ValueError(f"duplicate quote for {q.contract}")
            seen.add(q.contract)

    def expiries(self) -> tuple[date, ...]:
        return tuple(sorted({q.contract.expiry for q in self.quotes}))

    def quote(self, contract: OptionContract) -> OptionQuote:
        for q in self.quotes:
            if q.contract == contract:
                return q
        raise KeyError(contract)

    def atm_strike(self, expiry: date) -> float:
        strikes = {q.contract.strike for q in self._at(expiry)}
        # Sorting first makes a tie between two strikes resolve to the lower one.
        return min(sorted(strikes), key=lambda k: abs(k - self.spot))

    def atm_iv(self, expiry: date) -> float:
        """Mean of the ATM call and put IV; falls back to whichever side has an IV."""
        strike = self.atm_strike(expiry)
        ivs = [q.iv for q in self._at(expiry) if q.contract.strike == strike and q.iv is not None]
        if not ivs:
            raise ValueError(f"no IV at ATM strike {strike} for {expiry}")
        return sum(ivs) / len(ivs)

    def _at(self, expiry: date) -> list[OptionQuote]:
        quotes = [q for q in self.quotes if q.contract.expiry == expiry]
        if not quotes:
            raise KeyError(expiry)
        return quotes
