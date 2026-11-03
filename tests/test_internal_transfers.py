"""Pairing the two orders of an internal transfer (`internal_transfers`), and deducing plan moves (`plan_moves`)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from myinvestor.internal_transfers import pair_orders
from myinvestor.models import Order, PositionPoint
from myinvestor.plan_moves import PlanMove, deduce_plan_moves
from tests.conftest import order

OUT, IN = "IIC_SWITCH_DEREGISTRATION", "IIC_SWITCH_REGISTRATION"


def leg(ref: str, day: str, operation: str, **overrides: Any) -> Order:
    return Order.model_validate(order(ref, day, operationType=operation, **overrides))


def pairs(*orders: Order) -> list[tuple[str | None, str | None]]:
    return [(t.out.reference if t.out else None, t.into.reference if t.into else None) for t in pair_orders(orders)]


def test_the_closest_leg_of_the_same_kind_wins() -> None:
    got = pairs(
        leg("O1", "2026-06-01", OUT, cash=1000),
        leg("far", "2026-06-06", IN, cash=1000),
        leg("other_kind", "2026-06-01", "INTERNAL_TRANSFER_SUBSCRIPTION", cash=1000),
        leg("near", "2026-06-02", IN, cash=1000),
    )
    assert got == [("O1", "near"), (None, "other_kind"), (None, "far")]


def test_legs_of_another_kind_pair_when_nothing_better_fits() -> None:
    got = pairs(leg("O1", "2026-06-01", "INTERNAL_TRANSFER_REIMBURSEMENT"), leg("I1", "2026-06-01", IN))
    assert got == [("O1", "I1")]


def test_amounts_too_different_or_days_too_far_do_not_pair() -> None:
    got = pairs(
        leg("O1", "2026-06-01", OUT, cash=1000),
        leg("I1", "2026-06-01", IN, cash=1100),  # 10 % more
        leg("O2", "2026-07-01", OUT, cash=None),
        leg("I2", "2026-07-09", IN, cash=None),  # 8 days later
    )
    assert sorted(got, key=str) == sorted([("O1", None), ("O2", None), (None, "I1"), (None, "I2")], key=str)


def test_an_unknown_amount_pairs_by_date() -> None:
    assert pairs(leg("O1", "2026-06-01", OUT, cash=None), leg("I1", "2026-06-03", IN, cash=999)) == [("O1", "I1")]


def test_legs_of_different_accounts_never_pair_and_other_orders_are_ignored() -> None:
    got = pairs(
        leg("O1", "2026-06-01", OUT),
        leg("I1", "2026-06-01", IN, securitiesAccountId="202"),
        leg("B1", "2026-06-01", "INVESTMENT_FUNDS_SUBSCRIPTION"),
    )
    assert sorted(got, key=str) == sorted([("O1", None), (None, "I1")], key=str)


def test_each_order_is_used_once() -> None:
    got = pairs(leg("O1", "2026-06-01", OUT), leg("O2", "2026-06-01", OUT), leg("I1", "2026-06-01", IN))
    assert got == [("O1", "I1"), ("O2", None)]


def test_a_transfer_takes_the_incoming_status_and_the_first_known_amount() -> None:
    [t] = pair_orders(
        [
            leg("O1", "2026-06-01", OUT, cash=None, status="EXECUTED"),
            leg("I1", "2026-06-02", IN, cash=50, status="IN_PROGRESS"),
        ]
    )
    assert (t.day.isoformat(), t.status, t.amount) == ("2026-06-01", "IN_PROGRESS", Decimal(50))


# ---- plan moves deduced from the history --------------------------------------------------------


def series(**plans: list[tuple[str, float, float]]) -> dict[str, list[PositionPoint]]:
    return {
        code: [
            PositionPoint.model_validate({"date": d, "totalShares": shares, "marketValue": value})
            for d, shares, value in points
        ]
        for code, points in plans.items()
    }


def test_a_partial_move_into_a_new_plan() -> None:
    history = series(
        N1=[("2026-06-01", 100, 1000), ("2026-06-02", 25, 250), ("2026-06-03", 25, 251)],
        N2=[("2026-06-02", 50, 760), ("2026-06-03", 50, 765)],
    )
    assert deduce_plan_moves(history) == [PlanMove(date(2026, 6, 2), "N1", "N2", Decimal(75), Decimal(750))]


def test_a_whole_plan_moved_into_an_existing_one_a_few_days_later() -> None:
    history = series(
        N1=[("2026-06-01", 10, 500), ("2026-06-02", 10, 505)],  # gone after the 2nd
        N2=[("2026-06-01", 100, 1000), ("2026-06-03", 100, 1000), ("2026-06-05", 150, 1500)],
    )
    [move] = deduce_plan_moves(history)
    assert (move.day.isoformat(), move.origin, move.destination, move.shares) == ("2026-06-03", "N1", "N2", 10)


def test_contributions_redemptions_and_mismatched_values_are_not_moves() -> None:
    history = series(
        N1=[("2026-06-01", 100, 1000), ("2026-06-02", 50, 500), ("2026-06-10", 50, 500)],  # out 500 …
        N2=[("2026-06-01", 10, 100), ("2026-06-02", 70, 700), ("2026-06-10", 70, 700)],  # … in 600: no
        N3=[("2026-06-01", 10, 100), ("2026-06-10", 60, 600)],  # 500 in, but 8 days later
    )
    assert deduce_plan_moves(history) == []


def test_rounding_noise_and_an_empty_history_are_not_moves() -> None:
    history = series(N1=[("2026-06-01", 100, 1000), ("2026-06-02", 99.999, 1000)], N2=[("2026-06-02", 1, 0.1)])
    assert deduce_plan_moves(history) == []
    assert deduce_plan_moves({}) == []
