"""`fund_history`: range, one point per day/week/month and the change against the first point."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from myinvestor.fund_history import Every, history_rows, in_range, resample
from myinvestor.models import NavPoint


def point(day: str, nav: str) -> NavPoint:
    return NavPoint(position_date=date.fromisoformat(day), market_value=Decimal(nav))


# irregular like the API: weekly in the old part, daily at the end
POINTS = [
    point("2026-09-07", "10.00"),  # Monday, ISO week 37
    point("2026-09-14", "10.50"),  # week 38
    point("2026-09-28", "11.00"),  # week 40 (week 39 missing)
    point("2026-09-29", "11.20"),
    point("2026-09-30", "11.10"),  # last of September and of week 40
    point("2026-10-01", "11.30"),
    point("2026-10-02", "11.40"),  # last of October
]


def days(points: list[NavPoint]) -> list[str]:
    return [p.position_date.isoformat() for p in points]


def test_in_range_is_inclusive_and_open_ended() -> None:
    assert days(in_range(POINTS, date(2026, 9, 14), date(2026, 9, 29))) == [
        "2026-09-14",
        "2026-09-28",
        "2026-09-29",
    ]
    assert days(in_range(POINTS, None, date(2026, 9, 7))) == ["2026-09-07"]
    assert days(in_range(POINTS, date(2026, 10, 2), None)) == ["2026-10-02"]
    assert in_range(POINTS, None, None) == POINTS


def test_resample_keeps_the_last_point_of_each_week() -> None:
    assert days(resample(POINTS, Every.week)) == ["2026-09-07", "2026-09-14", "2026-10-02"]


def test_resample_week_uses_iso_weeks_across_the_new_year() -> None:
    # 2026-12-31 (Thursday) and 2027-01-01 (Friday) are the same ISO week (2026-W53)
    points = [point("2026-12-31", "1"), point("2027-01-01", "2"), point("2027-01-04", "3")]

    assert days(resample(points, Every.week)) == ["2027-01-01", "2027-01-04"]


def test_resample_keeps_the_last_point_of_each_month() -> None:
    assert days(resample(POINTS, Every.month)) == ["2026-09-30", "2026-10-02"]


def test_resample_day_drops_repeated_days_keeping_the_last() -> None:
    points = [point("2026-10-01", "1"), point("2026-10-01", "2"), point("2026-10-02", "3")]

    assert [p.market_value for p in resample(points, Every.day)] == [Decimal(2), Decimal(3)]


def test_rows_have_date_nav_and_currency_by_default() -> None:
    assert history_rows(POINTS[:2], "USD") == [
        {"fecha": "2026-09-07", "vl": Decimal("10.00"), "moneda": "USD"},
        {"fecha": "2026-09-14", "vl": Decimal("10.50"), "moneda": "USD"},
    ]


def test_rows_with_change_against_the_first_point() -> None:
    points = [point("2026-09-07", "10"), point("2026-09-14", "10.5"), point("2026-09-21", "9.123")]
    rows = history_rows(points, None, with_change=True)

    assert [r["variacion_pct"] for r in rows] == [Decimal("0.00"), Decimal("5.00"), Decimal("-8.77")]


@pytest.mark.parametrize("first", ["0", "0.00"])
def test_change_is_empty_when_the_first_nav_is_zero(first: str) -> None:
    rows = history_rows([point("2026-09-07", first), point("2026-09-14", "1")], "EUR", with_change=True)

    assert [r["variacion_pct"] for r in rows] == [None, None]


def test_no_rows_without_points() -> None:
    assert history_rows([], "EUR", with_change=True) == []
