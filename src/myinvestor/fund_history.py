"""NAV history of a catalog fund: date range, one point per day/week/month and change against the first point."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from myinvestor.models import NavPoint

CENTS = Decimal("0.01")


class Every(StrEnum):
    day = "day"
    week = "week"
    month = "month"


_BUCKETS: dict[Every, Callable[[date], tuple[int, ...]]] = {
    Every.day: lambda d: (d.toordinal(),),
    Every.week: lambda d: tuple(d.isocalendar())[:2],  # ISO year and week: Monday to Sunday
    Every.month: lambda d: (d.year, d.month),
}


def in_range(points: Sequence[NavPoint], since: date | None, until: date | None) -> list[NavPoint]:
    """Points between `since` and `until`, both included; None leaves that end open."""
    return [
        p for p in points if (since is None or p.position_date >= since) and (until is None or p.position_date <= until)
    ]


def resample(points: Sequence[NavPoint], every: Every) -> list[NavPoint]:
    """The last point of each day, ISO week or month of `points` (already sorted by date)."""
    bucket = _BUCKETS[every]
    last: dict[tuple[int, ...], NavPoint] = {}
    for p in points:
        last[bucket(p.position_date)] = p
    return list(last.values())


def change_pct(value: Decimal, base: Decimal) -> Decimal | None:
    """`value` against `base`, in % with 2 decimals; None if `base` is zero."""
    if base.is_zero():
        return None
    return ((value / base - 1) * 100).quantize(CENTS, rounding=ROUND_HALF_UP)


def history_rows(
    points: Sequence[NavPoint], currency: str | None, *, with_change: bool = False
) -> list[dict[str, Any]]:
    """`fecha`, `vl` and `moneda`; with `with_change`, also `variacion_pct` against the first point."""
    rows: list[dict[str, Any]] = [
        {"fecha": p.position_date.isoformat(), "vl": p.market_value, "moneda": currency} for p in points
    ]
    if not with_change or not rows:
        return rows
    base = rows[0]["vl"]
    return [{**r, "variacion_pct": change_pct(r["vl"], base)} for r in rows]
