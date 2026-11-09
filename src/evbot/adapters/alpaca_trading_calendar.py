from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from evbot.adapters.http_client import HttpRequestError, JsonHttpClient
from evbot.ports.errors import TradingCalendarError

NEW_YORK = ZoneInfo("America/New_York")
FETCH_RADIUS_DAYS = 14  # longer than any US market closure, so a nearby session is always found


class AlpacaTradingCalendar:
    def __init__(self, http: JsonHttpClient, base_url: str, key_id: str, secret_key: str) -> None:
        self._http = http
        self._url = f"{base_url}/v2/calendar"
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._sessions: dict[date, tuple[datetime, datetime]] = {}
        self._covered: set[date] = set()

    def previous_session(self, day: date) -> date:
        return self._nearest_session(day, step=-1)

    def next_session(self, day: date) -> date:
        return self._nearest_session(day, step=1)

    def open_time(self, day: date) -> datetime:
        return self._session(day)[0]

    def close_time(self, day: date) -> datetime:
        return self._session(day)[1]

    def _nearest_session(self, day: date, step: int) -> date:
        for offset in range(1, FETCH_RADIUS_DAYS + 1):
            candidate = day + timedelta(days=step * offset)
            self._load_around(candidate)
            if candidate in self._sessions:
                return candidate
        raise TradingCalendarError(f"no session within {FETCH_RADIUS_DAYS} days of {day}")

    def _session(self, day: date) -> tuple[datetime, datetime]:
        self._load_around(day)
        if day not in self._sessions:
            raise TradingCalendarError(f"{day} is not a trading session")
        return self._sessions[day]

    def _load_around(self, day: date) -> None:
        if day in self._covered:
            return
        start = day - timedelta(days=FETCH_RADIUS_DAYS)
        end = day + timedelta(days=FETCH_RADIUS_DAYS)
        params = {"start": start.isoformat(), "end": end.isoformat()}
        try:
            rows = self._http.get(self._url, params=params, headers=self._headers)
            for row in rows:
                session = date.fromisoformat(row["date"])
                self._sessions[session] = (
                    self._in_new_york(session, row["open"]),
                    self._in_new_york(session, row["close"]),
                )
        except HttpRequestError as error:
            raise TradingCalendarError(str(error)) from error
        except (KeyError, TypeError, ValueError) as error:
            raise TradingCalendarError(f"unexpected Alpaca calendar format: {error}") from error
        self._covered.update(start + timedelta(days=i) for i in range((end - start).days + 1))

    @staticmethod
    def _in_new_york(session: date, clock_time: str) -> datetime:
        return datetime.combine(session, time.fromisoformat(clock_time), NEW_YORK)
