"""Tax cost of pension plans, rebuilt from the position history of their account. No I/O.

MyInvestor's `invertido` of a plan that receives a mobilisation (a transfer from another plan) starts at
the money received, while the source plan only loses the *cost* of the units that leave. The tax cost,
the one a mobilisation carries over, is rebuilt here, walking every plan of the account day by day:

- units in that match no mobilisation are a contribution: they add the rise of `invertido`;
- units out take their proportional share of the plan's tax cost (units out * average tax cost); a plan
  whose series stops while it still had units lost them all on the next day of the account's series;
- a mobilisation: one plan loses units and, that same day or up to `WINDOW_DAYS` later, another plan of
  the account gains units for the same money (within `TOLERANCE`, or `MIN_TOLERANCE` euros). The
  destination then adds the cost that left the source instead of the money received.

The history reaches one year back at most: a plan already held on its first day starts with MyInvestor's
`invertido` (plus `opening`, its adjustment from a longer series), so a mobilisation older than that is
not seen. A point without units or `invertido` makes that plan's cost unknown from then on.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from myinvestor.models import PositionPoint

WINDOW_DAYS = 7
TOLERANCE = Decimal("0.02")  # relative difference between the money out of the source and into the destination
MIN_TOLERANCE = Decimal(1)
ZERO = Decimal(0)


@dataclass(frozen=True)
class CostPoint:
    """One day of a plan: MyInvestor's `invertido` and the rebuilt tax cost."""

    date: date
    invested: Decimal | None
    cost: Decimal | None

    @property
    def adjustment(self) -> Decimal | None:
        """Tax cost - `invertido`: what to add to MyInvestor's figure."""
        return None if self.invested is None or self.cost is None else self.cost - self.invested


@dataclass(frozen=True)
class Mobilization:
    origin: str
    destination: str
    left: date  # the day the source lost the units
    arrived: date  # the day the destination gained them
    amount: Decimal  # money that left the source (units * its NAV)
    cost: Decimal | None  # tax cost handed to the destination


@dataclass(frozen=True)
class PlanCosts:
    points: Mapping[str, Sequence[CostPoint]]
    mobilizations: tuple[Mobilization, ...] = ()

    def at(self, code: str, day: date | None = None) -> CostPoint | None:
        """The plan's last point on or before `day` (its last one without `day`)."""
        found = self.points.get(code) or ()
        if day is None:
            return found[-1] if found else None
        index = bisect.bisect_right([p.date for p in found], day)
        return found[index - 1] if index else None

    def adjustment(self, code: str, day: date | None = None) -> Decimal | None:
        point = self.at(code, day)
        return point.adjustment if point else None

    def cost(self, code: str, invested_now: Decimal | None) -> Decimal | None:
        """Today's tax cost of a plan: today's `invertido` plus the adjustment of its last point.

        Contributions after that point add the same to both. None if the plan has no history.
        """
        adjustment = self.adjustment(code)
        return None if adjustment is None or invested_now is None else invested_now + adjustment


@dataclass(frozen=True)
class _State:
    shares: Decimal
    cost: Decimal | None
    invested: Decimal | None
    nav: Decimal | None
    value: Decimal | None
    gone: bool = False


@dataclass
class _Out:
    plan: str
    day: date
    amount: Decimal
    cost: Decimal | None
    taken: bool = False


@dataclass
class _Walk:
    opening: Mapping[str, Decimal]
    states: dict[str, _State] = field(default_factory=dict)
    pending: list[_Out] = field(default_factory=list)
    moves: list[Mobilization] = field(default_factory=list)
    points: dict[str, list[CostPoint]] = field(default_factory=lambda: defaultdict(list))

    def open(self, code: str, p: PositionPoint) -> None:
        invested = p.investment_added_value
        cost = None if invested is None or p.total_shares is None else invested + self.opening.get(code, ZERO)
        self.states[code] = _State(p.total_shares or ZERO, cost, invested, _nav(p), p.market_value)

    def step(self, code: str, p: PositionPoint, day: date) -> tuple[str, Decimal | None] | None:
        """Apply one point; units in are returned as `(plan, money)`, to match them once the day's outs are known."""
        before = self.states.get(code) or _State(ZERO, ZERO, ZERO, None, None)
        nav = _nav(p) or before.nav
        if p.total_shares is None or before.cost is None:
            self.states[code] = _State(before.shares, None, p.investment_added_value, nav, p.market_value)
            return None
        moved = p.total_shares - before.shares
        cost: Decimal | None = before.cost
        if moved < 0:
            out_cost = before.cost * -moved / before.shares
            cost = before.cost - out_cost
            if nav:
                self.pending.append(_Out(code, day, -moved * nav, out_cost))
        self.states[code] = _State(p.total_shares, cost, p.investment_added_value, nav, p.market_value)
        if moved <= 0:
            return None
        if p.investment_added_value is not None and before.invested is not None:
            return code, p.investment_added_value - before.invested
        return code, moved * nav if nav else None

    def vanish(self, code: str, day: date) -> None:
        """A plan whose series stopped while it had units: they all left on `day`."""
        state = self.states[code]
        if state.value:
            self.pending.append(_Out(code, day, state.value, state.cost))
        self.states[code] = replace(state, shares=ZERO, cost=ZERO if state.cost is not None else None, gone=True)

    def arrive(self, code: str, money: Decimal | None, day: date) -> None:
        state = self.states[code]
        match = self._match(code, money, day)
        if match is None:
            added = money
        else:
            match.taken = True
            added = match.cost
            self.moves.append(Mobilization(match.plan, code, match.day, day, match.amount, match.cost))
        cost = state.cost + added if state.cost is not None and added is not None else None
        self.states[code] = replace(state, cost=cost)

    def _match(self, code: str, money: Decimal | None, day: date) -> _Out | None:
        if money is None:
            return None
        candidates = [
            out
            for out in self.pending
            if not out.taken
            and out.plan != code
            and 0 <= (day - out.day).days <= WINDOW_DAYS
            and abs(money - out.amount) <= max(MIN_TOLERANCE, out.amount * TOLERANCE)
        ]
        return min(candidates, key=lambda out: (abs(money - out.amount), -out.day.toordinal()), default=None)

    def record(self, code: str, day: date) -> None:
        state = self.states[code]
        self.points[code].append(CostPoint(day, state.invested, state.cost))


def rebuild(series: Mapping[str, Sequence[PositionPoint]], opening: Mapping[str, Decimal] | None = None) -> PlanCosts:
    """Tax cost of each plan of one pension account, day by day (see the module docstring).

    `opening`: adjustment (tax cost - `invertido`) of the plans held on the first day, from a longer series.
    """
    by_day: dict[date, dict[str, PositionPoint]] = defaultdict(dict)
    for code, points in series.items():
        for p in points:
            by_day[p.date][code] = p
    days = sorted(by_day)
    last_seen = {code: max(p.date for p in points) for code, points in series.items() if points}
    walk = _Walk(opening or {})
    for day in days:
        today = by_day[day]
        if day == days[0]:
            for code, p in sorted(today.items()):
                walk.open(code, p)
                walk.record(code, day)
            continue
        for code, state in list(walk.states.items()):
            if code not in today and not state.gone and last_seen[code] < day and state.shares > 0:
                walk.vanish(code, day)
        arrivals = [hit for code, p in sorted(today.items()) if (hit := walk.step(code, p, day))]
        for code, money in arrivals:
            walk.arrive(code, money, day)
        for code in sorted(today):
            walk.record(code, day)
    return PlanCosts({code: tuple(points) for code, points in walk.points.items()}, tuple(walk.moves))


def _nav(p: PositionPoint) -> Decimal | None:
    if p.market_value is None or not p.total_shares:
        return None
    return p.market_value / p.total_shares
