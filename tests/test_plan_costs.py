"""Rebuilding the tax cost of pension plans across mobilisations (`plan_costs`): pure, synthetic series."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from myinvestor.models import PositionPoint
from myinvestor.plan_costs import Mobilization, rebuild

A, B, C = "N0001", "N0002", "N0003"


def point(day: str, shares: Any, value: Any, invested: Any) -> PositionPoint:
    return PositionPoint.model_validate(
        {
            "date": day,
            "totalShares": shares,
            "marketValue": value,
            "investmentAddedValue": invested,
        }
    )


def partial_mobilization() -> dict[str, list[PositionPoint]]:
    """A (cost 10/unit) moves half its units to B, born that day with `invertido` = the money received."""
    return {
        A: [
            point("2026-03-01", 100, 1300, 1000),
            point("2026-03-02", 100, 1320, 1000),
            point("2026-03-03", 50, 660, 500),  # MyInvestor takes only the cost of the units out
            point("2026-03-04", 50, 665, 500),
        ],
        B: [
            point("2026-03-03", 66, 660, 660),  # born with the money received, not the cost
            point("2026-03-04", 76, 765, 760),  # a contribution of 100
        ],
    }


def costs_by_day(series: dict[str, list[PositionPoint]], **kwargs: Any) -> dict[str, dict[str, Decimal | None]]:
    rebuilt = rebuild(series, **kwargs)
    return {code: {p.date.isoformat(): p.cost for p in points} for code, points in rebuilt.points.items()}


def test_a_mobilization_hands_the_source_cost_to_the_destination() -> None:
    costs = costs_by_day(partial_mobilization())

    assert costs[A] == {"2026-03-01": 1000, "2026-03-02": 1000, "2026-03-03": 500, "2026-03-04": 500}
    assert costs[B] == {"2026-03-03": 500, "2026-03-04": 600}  # inherited 500, then +100 contributed


def test_the_total_cost_does_not_change_and_the_real_gain_does_not_jump() -> None:
    series = partial_mobilization()
    rebuilt = rebuild(series)

    def totals(day: str) -> tuple[Decimal, Decimal]:
        found = [(p, rebuilt.at(code, date.fromisoformat(day))) for code, ps in series.items() for p in ps]
        today = [(p, c) for p, c in found if p.date.isoformat() == day and c]
        cost = sum((c.cost for _, c in today if c.cost is not None), Decimal(0))
        value = sum((p.market_value for p, _ in today if p.market_value is not None), Decimal(0))
        return cost, value - cost

    assert totals("2026-03-02") == (1000, 320)
    assert totals("2026-03-03") == (1000, 320)  # MyInvestor's `invertido` sums 1160: a gain of 160
    assert rebuilt.mobilizations == (
        Mobilization(A, B, date(2026, 3, 3), date(2026, 3, 3), Decimal(660), Decimal(500)),
    )


def test_today_cost_is_today_invested_plus_the_last_adjustment() -> None:
    rebuilt = rebuild(partial_mobilization())

    assert rebuilt.adjustment(B) == -160
    assert rebuilt.cost(B, Decimal(860)) == 700  # another 100 contributed after the history's last day
    assert rebuilt.cost(A, Decimal(500)) == 500
    assert rebuilt.cost(C, Decimal(100)) is None  # no history: unknown
    assert rebuilt.cost(B, None) is None


def test_the_destination_may_arrive_some_days_later() -> None:
    series = {
        A: [point("2026-03-01", 100, 1300, 1000), point("2026-03-02", 0, 0, 0)],
        B: [point("2026-03-01", 0, 0, 0), point("2026-03-06", 130, 1301, 1301)],
    }
    assert costs_by_day(series)[B]["2026-03-06"] == 1000


def test_too_late_or_another_amount_is_a_contribution() -> None:
    late = {
        A: [point("2026-03-01", 100, 1300, 1000), point("2026-03-02", 0, 0, 0)],
        B: [point("2026-03-01", 0, 0, 0), point("2026-03-20", 130, 1300, 1300)],
    }
    other = {
        A: [point("2026-03-01", 100, 1300, 1000), point("2026-03-02", 50, 650, 500)],
        B: [point("2026-03-02", 100, 1000, 1000)],
    }
    assert costs_by_day(late)[B]["2026-03-20"] == 1300
    assert costs_by_day(other)[B]["2026-03-02"] == 1000


def test_a_plan_whose_series_stops_moved_everything_out() -> None:
    """A full mobilisation where the source has no more points: its units left on the next day."""
    series = {
        A: [point("2026-03-01", 100, 1300, 1000)],
        B: [point("2026-03-01", 10, 100, 90), point("2026-03-02", 140, 1400, 1390)],
    }
    rebuilt = rebuild(series)
    assert rebuilt.at(B).cost == 1090  # 90 + the 1000 inherited
    assert [(m.origin, m.destination) for m in rebuilt.mobilizations] == [(A, B)]


def test_chained_mobilizations_carry_the_cost_through() -> None:
    series = {
        A: [point("2026-03-01", 100, 1300, 1000), point("2026-03-02", 0, 0, 0)],
        B: [point("2026-03-02", 130, 1300, 1300), point("2026-03-03", 0, 0, 0)],
        C: [point("2026-03-03", 100, 1300, 1300)],
    }
    assert costs_by_day(series)[C]["2026-03-03"] == 1000


def test_opening_adjusts_the_plans_held_on_the_first_day() -> None:
    series = {B: [point("2026-03-01", 66, 660, 660), point("2026-03-02", 76, 765, 760)]}
    assert costs_by_day(series, opening={B: Decimal(-160)})[B] == {"2026-03-01": 500, "2026-03-02": 600}


def test_without_units_or_invested_the_cost_is_unknown_from_then_on() -> None:
    series = {
        A: [
            point("2026-03-01", 100, 1300, 1000),
            point("2026-03-02", None, 1300, 1000),
            point("2026-03-03", 110, 1430, 1100),
        ]
    }
    assert costs_by_day(series)[A] == {"2026-03-01": 1000, "2026-03-02": None, "2026-03-03": None}
    assert rebuild(series).cost(A, Decimal(1100)) is None


def test_an_external_mobilization_out_only_lowers_the_source_cost() -> None:
    series = {A: [point("2026-03-01", 100, 1300, 1000), point("2026-03-02", 40, 520, 400)]}
    rebuilt = rebuild(series)
    assert rebuilt.at(A).cost == 400
    assert rebuilt.mobilizations == ()


def test_an_empty_history_knows_nothing() -> None:
    rebuilt = rebuild({})
    assert rebuilt.points == {}
    assert rebuilt.cost(A, Decimal(1)) is None
