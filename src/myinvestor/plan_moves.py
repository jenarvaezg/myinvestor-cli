"""Mobilisations between two plans of the same pension account, deduced from the position history.

Pure logic, no I/O. Neither `…/pension-accounts/{id}/transfers` nor the orders are known to show a
partial mobilisation between two MyInvestor plans, but the daily history does: the origin loses
shares the same day the destination is born (or gains shares) for about the same value. A move is
an outflow of one plan (shares down by at least `MIN_DROP` of what it had, or the plan gone from
the series) matched with an inflow of another plan of the account (born after the series starts,
or shares up) at most `MAX_LAG_DAYS` later and whose value differs by at most `VALUE_TOLERANCE`.
Among several inflows the closest in days, then in value, wins; each inflow is used once and an
outflow with no inflow (a redemption, say) is not a move. Values are approximate: shares moved
times that day's NAV of each plan (`marketValue / totalShares`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from myinvestor.models import PositionPoint

MIN_DROP = Decimal("0.01")  # of the origin's shares: below it, rounding noise
MAX_LAG_DAYS = 5
VALUE_TOLERANCE = Decimal("0.05")  # relative


@dataclass(frozen=True)
class Flow:
    plan: str
    day: date
    shares: Decimal  # always positive
    value: Decimal


@dataclass(frozen=True)
class PlanMove:
    day: date  # the origin's outflow
    origin: str
    destination: str
    shares: Decimal  # shares that left the origin
    value: Decimal  # approximate value that left the origin


def _nav(point: PositionPoint) -> Decimal | None:
    if point.market_value is None or not point.total_shares:
        return None
    return point.market_value / point.total_shares


def _value(shares: Decimal, *points: PositionPoint) -> Decimal | None:
    nav = next((n for p in points if (n := _nav(p)) is not None), None)
    return None if nav is None else shares * nav


def _flows(plan: str, points: Sequence[PositionPoint], days: Sequence[date]) -> tuple[list[Flow], list[Flow]]:
    """(outflows, inflows) of one plan; `days` are every day in the account's history, sorted."""
    known = [p for p in points if p.total_shares is not None]
    outs: list[Flow] = []
    ins: list[Flow] = []
    if not known:
        return outs, ins
    first, last = known[0], known[-1]
    if first.date > days[0] and first.total_shares and (value := _value(first.total_shares, first)) is not None:
        ins.append(Flow(plan, first.date, first.total_shares, value))
    for before, after in pairwise(known):
        assert before.total_shares is not None and after.total_shares is not None
        change = after.total_shares - before.total_shares
        value = _value(abs(change), after, before)
        if value is None or not change:
            continue
        if change > 0:
            ins.append(Flow(plan, after.date, change, value))
        elif -change >= MIN_DROP * before.total_shares:
            outs.append(Flow(plan, after.date, -change, value))
    gone = next((d for d in days if d > last.date), None)
    if gone and last.total_shares and (value := _value(last.total_shares, last)) is not None:
        outs.append(Flow(plan, gone, last.total_shares, value))
    return outs, ins


def _fit(out: Flow, into: Flow) -> tuple[int, Decimal] | None:
    lag = (into.day - out.day).days
    if into.plan == out.plan or not 0 <= lag <= MAX_LAG_DAYS or not out.value:
        return None
    diff = abs(into.value - out.value) / out.value
    return None if diff > VALUE_TOLERANCE else (lag, diff)


def deduce_plan_moves(series: Mapping[str, Sequence[PositionPoint]]) -> list[PlanMove]:
    """Moves between the plans of one pension account's history (`position_history(..., pension=True)`)."""
    days = sorted({p.date for points in series.values() for p in points})
    if not days:
        return []
    outs: list[Flow] = []
    ins: list[Flow] = []
    for plan, points in series.items():
        plan_outs, plan_ins = _flows(plan, sorted(points, key=lambda p: p.date), days)
        outs.extend(plan_outs)
        ins.extend(plan_ins)
    moves: list[PlanMove] = []
    used: set[int] = set()
    for out in sorted(outs, key=lambda f: (f.day, f.plan)):
        fits = [(fit, n) for n, into in enumerate(ins) if n not in used and (fit := _fit(out, into)) is not None]
        if not fits:
            continue
        _, n = min(fits)
        used.add(n)
        moves.append(PlanMove(out.day, out.plan, ins[n].plan, out.shares, out.value))
    return moves
