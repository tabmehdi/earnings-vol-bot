from datetime import date

from evbot.strategy.expiries import monthly_expiries, standard_monthly, third_friday


def always_open(day: date) -> bool:
    return True


def test_third_friday_of_a_month_starting_on_a_sunday() -> None:
    # November 2026 starts on a Sunday: Fridays are the 6th, 13th and 20th.
    assert third_friday(2026, 11) == date(2026, 11, 20)


def test_third_friday_of_a_month_starting_on_a_friday() -> None:
    # January 2027 starts on a Friday: Fridays are the 1st, 8th and 15th.
    assert third_friday(2027, 1) == date(2027, 1, 15)


def test_standard_monthly_is_the_third_friday_on_a_normal_month() -> None:
    assert standard_monthly(2026, 12, always_open) == date(2026, 12, 18)


def test_good_friday_moves_the_monthly_to_thursday() -> None:
    # Good Friday 2025 fell on April 18, the third Friday, so April options expired on the 17th.
    def closed_on_good_friday(day: date) -> bool:
        return day != date(2025, 4, 18)

    assert standard_monthly(2025, 4, closed_on_good_friday) == date(2025, 4, 17)


def test_monthlies_between_two_dates_in_order() -> None:
    found = monthly_expiries(date(2026, 11, 19), date(2027, 2, 2), always_open)

    assert found == (date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15), date(2027, 2, 19))


def test_a_monthly_already_past_is_left_out() -> None:
    assert monthly_expiries(date(2026, 11, 21), date(2026, 12, 31), always_open) == (
        date(2026, 12, 18),
    )
