from datetime import date
from typing import Any

import pytest

from evbot.adapters.fred_risk_free_rate import FredRiskFreeRate
from evbot.ports.errors import RiskFreeRateError
from tests.fixtures.http import SeenRequest, json_client, response

URL = "https://fred.test/fred/series/observations"
THURSDAY = date(2026, 11, 12)


def observation(day: str, value: str) -> dict[str, str]:
    """Shaped like a row of FRED's series/observations response."""
    return {
        "realtime_start": "2026-11-12",
        "realtime_end": "2026-11-12",
        "date": day,
        "value": value,
    }


def payload(*rows: dict[str, str]) -> dict[str, Any]:
    return {
        "realtime_start": "2026-11-12",
        "units": "lin",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": len(rows),
        "observations": list(rows),
    }


NORMAL = payload(
    observation("2026-11-09", "3.86"),
    observation("2026-11-10", "3.85"),
    observation("2026-11-12", "3.84"),
)
# Veterans Day: the bond market is closed and FRED records "." for the day.
HOLIDAY = payload(observation("2026-11-10", "3.85"), observation("2026-11-11", "."))


def rates_returning(body: Any, status: int = 200) -> tuple[FredRiskFreeRate, list[SeenRequest]]:
    seen: list[SeenRequest] = []

    def handler(request: SeenRequest) -> Any:
        seen.append(request)
        return response(status, body)

    rates = FredRiskFreeRate(json_client(handler, max_retries=0), URL, "DGS3MO", "secret-key")
    return rates, seen


def test_returns_the_days_yield_as_a_decimal() -> None:
    rates, _ = rates_returning(NORMAL)

    # 3.84 percent -> 0.0384
    assert rates.rate_on(THURSDAY) == pytest.approx(0.0384)


def test_a_holiday_dot_uses_the_latest_prior_value() -> None:
    rates, _ = rates_returning(HOLIDAY)

    # 2026-11-11 is "."; the latest valid value before it is 3.85 on 2026-11-10.
    assert rates.rate_on(date(2026, 11, 11)) == pytest.approx(0.0385)


def test_a_day_not_yet_published_uses_the_latest_prior_value() -> None:
    rates, _ = rates_returning(payload(observation("2026-11-10", "3.85")))

    assert rates.rate_on(THURSDAY) == pytest.approx(0.0385)


def test_observations_after_the_day_are_ignored() -> None:
    rates, _ = rates_returning(NORMAL)

    assert rates.rate_on(date(2026, 11, 10)) == pytest.approx(0.0385)


def test_asks_for_the_series_in_json_ending_on_the_day() -> None:
    rates, seen = rates_returning(NORMAL)

    rates.rate_on(THURSDAY)

    params = seen[0].url.params
    assert params["series_id"] == "DGS3MO"
    assert params["file_type"] == "json"
    assert params["api_key"] == "secret-key"
    assert params["observation_end"] == "2026-11-12"
    assert params["observation_start"] == "2026-10-29"  # two weeks back covers any closure


def test_one_request_per_day_is_cached() -> None:
    rates, seen = rates_returning(NORMAL)

    rates.rate_on(THURSDAY)
    rates.rate_on(THURSDAY)

    assert len(seen) == 1


def test_no_valid_value_is_an_error() -> None:
    rates, _ = rates_returning(payload(observation("2026-11-11", ".")))

    with pytest.raises(RiskFreeRateError):
        rates.rate_on(date(2026, 11, 11))


def test_http_failure_is_an_error_that_hides_the_key() -> None:
    rates, _ = rates_returning({"error_message": "Bad Request"}, status=400)

    with pytest.raises(RiskFreeRateError) as error:
        rates.rate_on(THURSDAY)

    assert "secret-key" not in str(error.value)


def test_unexpected_format_is_an_error() -> None:
    rates, _ = rates_returning({"something": "else"})

    with pytest.raises(RiskFreeRateError):
        rates.rate_on(THURSDAY)


def test_name_says_which_series_is_used() -> None:
    rates, _ = rates_returning(NORMAL)

    assert rates.name == "FRED DGS3MO"
