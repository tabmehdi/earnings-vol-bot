from collections.abc import Callable
from datetime import date, timedelta

FRIDAY = 4


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(FRIDAY - first.weekday()) % 7 + 14)


def standard_monthly(year: int, month: int, is_session: Callable[[date], bool]) -> date:
    """The third Friday, or the Thursday before it when that Friday is a market holiday."""
    friday = third_friday(year, month)
    return friday if is_session(friday) else friday - timedelta(days=1)


def monthly_expiries(
    start: date, end: date, is_session: Callable[[date], bool]
) -> tuple[date, ...]:
    """Standard monthlies of every month from start's to end's, not before start."""
    found = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        expiry = standard_monthly(year, month, is_session)
        if expiry >= start:
            found.append(expiry)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(found)
