"""Internal transfers rebuilt from orders: switches between two funds (or two plans) of the same account.

Pure logic, no I/O. `…/transfers` lists transfer requests, and the web only shows the ones in
progress («No tienes traspasos en curso»): a switch inside MyInvestor already executed lives in
the orders, as two legs, one taking shares out of the origin and one putting them into the
destination. Two legs are the same transfer when they belong to the same account, the incoming
one is dated at most `MAX_GAP_DAYS` from the outgoing one and, when both carry an amount, the
amounts differ by at most `AMOUNT_TOLERANCE`. Among several candidates the closest wins: same
kind of order first (switch with switch), then fewer days apart, then the nearer amount. Each
order ends up in at most one transfer; a leg with no partner is a transfer of its own, without
the other side (it is never made up).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from myinvestor.models import Order

MAX_GAP_DAYS = 7
AMOUNT_TOLERANCE = Decimal("0.05")  # relative

# `operationType` of each leg (labels from the web bundle), with the kind of transfer it belongs to.
OUT_LEGS = {
    "INTERNAL_TRANSFER_REIMBURSEMENT": "internal",  # "Reembolso por Traspaso Interno"
    "IIC_SWITCH_DEREGISTRATION": "switch",  # "Baja switch"
    "INTERNAL_TRANSFER_IIC": "iic",  # "Traspaso interno IIC LP": direction unknown, taken as out (as `gains`)
    "DEREGISTRATION_BY_INTERNAL_PLAN_TRANSFER": "plan",  # "Baja por traspaso interno"
    "DEREGISTRATION_TRANSFER_PLANS_INTERNAL_FROM": "plan_from",  # "Baja traspaso interno DE"
}
IN_LEGS = {
    "INTERNAL_TRANSFER_SUBSCRIPTION": "internal",  # "Suscripción por Traspaso Interno"
    "IIC_SWITCH_REGISTRATION": "switch",  # "Alta switch"
    "REGISTRATION_BY_INTERNAL_PLAN_TRANSFER": "plan",  # "Alta por traspaso interno"
    "REGISTRATION_TRANSFER_PLANS_INTERNAL_FROM": "plan_from",  # "Alta traspaso interno DE"
}


@dataclass(frozen=True)
class InternalTransfer:
    """One transfer between two products of the same account: either leg may be missing, never both."""

    account_id: str
    out: Order | None
    into: Order | None

    @property
    def first(self) -> Order:
        leg = self.out or self.into
        assert leg is not None
        return leg

    @property
    def day(self) -> date:
        return self.first.order_date

    @property
    def status(self) -> str | None:
        """The incoming leg's: the transfer is done when the destination receives the shares."""
        return (self.into or self.first).status

    @property
    def amount(self) -> Decimal | None:
        legs = (self.out, self.into)
        return next((leg.cash for leg in legs if leg is not None and leg.cash is not None), None)


def _is_leg(order: Order) -> bool:
    return order.operation_type in OUT_LEGS or order.operation_type in IN_LEGS


def _amounts_differ(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    """Relative difference of two amounts, None when either is unknown (or zero)."""
    if a is None or b is None or not a:
        return None
    return abs(abs(a) - abs(b)) / abs(a)


def _score(out: Order, into: Order) -> tuple[int, int, Decimal] | None:
    """How well `into` fits as the other leg of `out` (lower is better), None when it cannot be."""
    gap = abs((into.order_date - out.order_date).days)
    if gap > MAX_GAP_DAYS:
        return None
    diff = _amounts_differ(out.cash, into.cash)
    if diff is not None and diff > AMOUNT_TOLERANCE:
        return None
    same_kind = OUT_LEGS[out.operation_type or ""] == IN_LEGS[into.operation_type or ""]
    return (0 if same_kind else 1, gap, diff if diff is not None else AMOUNT_TOLERANCE)


def _pair_account(account_id: str, legs: list[Order]) -> list[InternalTransfer]:
    outs = sorted((o for o in legs if o.operation_type in OUT_LEGS), key=lambda o: (o.order_date, o.reference))
    ins = sorted((o for o in legs if o.operation_type in IN_LEGS), key=lambda o: (o.order_date, o.reference))
    used: set[str] = set()
    found: list[InternalTransfer] = []
    for out in outs:
        scored = [(s, i) for i in ins if i.reference not in used and (s := _score(out, i)) is not None]
        into = min(scored, key=lambda pair: pair[0])[1] if scored else None
        if into is not None:
            used.add(into.reference)
        found.append(InternalTransfer(account_id, out, into))
    found.extend(InternalTransfer(account_id, None, i) for i in ins if i.reference not in used)
    return found


def pair_orders(orders: Iterable[Order]) -> list[InternalTransfer]:
    """Internal transfers among `orders` (any account; legs of different accounts never pair)."""
    by_account: dict[str, list[Order]] = defaultdict(list)
    for order in orders:
        if _is_leg(order) and order.account_id:
            by_account[order.account_id].append(order)
    return [t for account_id, legs in by_account.items() for t in _pair_account(account_id, legs)]
