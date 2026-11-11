from datetime import date

from evbot.adapters.http_client import HttpRequestError, RetryingJsonClient
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.ports.errors import EarningsCalendarError

# The endpoint is undocumented and rejects clients that do not look like a browser.
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

TIMING_BY_LABEL = {
    "time-pre-market": ReportTiming.BMO,
    "time-after-hours": ReportTiming.AMC,
}


class NasdaqEarningsCalendar:
    def __init__(self, http: RetryingJsonClient, url: str) -> None:
        self._http = http
        self._url = url

    def events_on(self, day: date) -> tuple[EarningsEvent, ...]:
        try:
            payload = self._http.get(
                self._url, params={"date": day.isoformat()}, headers=REQUEST_HEADERS
            )
            rows = payload["data"]["rows"] or []
            return tuple(
                EarningsEvent(
                    row["symbol"],
                    day,
                    TIMING_BY_LABEL.get(row.get("time"), ReportTiming.UNKNOWN),
                )
                for row in rows
            )
        except HttpRequestError as error:
            raise EarningsCalendarError(str(error)) from error
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise EarningsCalendarError(f"unexpected Nasdaq calendar format: {error}") from error
