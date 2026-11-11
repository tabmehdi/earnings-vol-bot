from datetime import date, timedelta

from evbot.adapters.http_client import HttpRequestError, JsonGetter
from evbot.ports.errors import RiskFreeRateError

# FRED has no value for weekends and bond-market holidays, or for today until it is published.
LOOKBACK_DAYS = 14
MISSING = "."


class FredRiskFreeRate:
    def __init__(self, http: JsonGetter, url: str, series_id: str, api_key: str) -> None:
        self._http = http
        self._url = url
        self._series_id = series_id
        self._api_key = api_key
        self._by_day: dict[date, float] = {}
        self.name = f"FRED {series_id}"

    def rate_on(self, day: date) -> float:
        if day not in self._by_day:
            self._by_day[day] = self._fetch(day)
        return self._by_day[day]

    def _fetch(self, day: date) -> float:
        params = {
            "series_id": self._series_id,
            "api_key": self._api_key,
            "file_type": "json",
            "observation_start": (day - timedelta(days=LOOKBACK_DAYS)).isoformat(),
            "observation_end": day.isoformat(),
        }
        try:
            observations = self._http.get(self._url, params=params)["observations"]
            valid = [
                (date.fromisoformat(row["date"]), float(row["value"]))
                for row in observations
                if row["value"] != MISSING and date.fromisoformat(row["date"]) <= day
            ]
        except HttpRequestError as error:
            # The client reports only the URL without query parameters, so the key is not here.
            raise RiskFreeRateError(f"{self.name}: {error}") from error
        except (KeyError, TypeError, ValueError) as error:
            raise RiskFreeRateError(f"{self.name}: unexpected FRED format: {error!r}") from error
        if not valid:
            raise RiskFreeRateError(f"{self.name}: no value in the {LOOKBACK_DAYS} days to {day}")
        return max(valid)[1] / 100
