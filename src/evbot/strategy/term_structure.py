from bisect import bisect_left
from dataclasses import dataclass
from itertools import pairwise


@dataclass(frozen=True, slots=True)
class TermStructure:
    """ATM IV by days to expiry: linear between points, flat beyond the ends."""

    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError(f"need at least 2 points, got {len(self.points)}")
        if any(a[0] >= b[0] for a, b in pairwise(self.points)):
            raise ValueError("points must be in strictly ascending day order")

    @property
    def first_day(self) -> float:
        return self.points[0][0]

    def iv_at(self, days: float) -> float:
        first, last = self.points[0], self.points[-1]
        if days <= first[0]:
            return first[1]
        if days >= last[0]:
            return last[1]
        upper = bisect_left(self.points, days, key=lambda point: point[0])
        (d0, iv0), (d1, iv1) = self.points[upper - 1], self.points[upper]
        return iv0 + (iv1 - iv0) * (days - d0) / (d1 - d0)

    def slope(self, from_days: float, to_days: float) -> float:
        if from_days == to_days:
            raise ValueError(f"slope needs two different days, got {from_days} twice")
        return (self.iv_at(to_days) - self.iv_at(from_days)) / (to_days - from_days)
