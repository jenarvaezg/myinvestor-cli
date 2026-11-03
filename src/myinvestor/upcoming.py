"""What is about to hit the cash accounts: contributions due, orders in progress and holds.

Pure (no I/O): `upcoming_rows` takes what the client fetched and returns the flat rows of `upcoming`,
so other commands (e.g. a snapshot) can reuse it.

Balance rule. `disponible` is the charge account's `enabledBalance`, which MyInvestor already gives net
of its holds (`withheldBalance`), and the money of an order in progress is withheld (a hold) or already
charged when the order is placed. So holds and orders are listed for information but never subtracted
again: only what has not happened yet (contributions due, and scheduled transfers once they exist)
lowers `disponible_tras`, accumulated per account in date order. `falta` is how much the account is
short at that row, when `disponible_tras` goes below zero.

Holds and orders. A hold does not say which order it is for (only `{id, accountId, type, amount}`), so
each `INVESTMENT_ORDER` hold is paired with an order in progress charged to the same account for the
same amount, one to one. The pair is a single row: the order's, with `retenido` true. Holds left
unpaired keep their own row.

`importe` is signed like a movement: charges are negative, money coming in (a sale) positive.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from myinvestor.models import CashAccount, Contribution, ContributionTimeFrame, Hold, Order, Ownership

# Order states the web treats as "in progress" (fund and pension orders). The others it knows:
# COMPLETE, CANCEL and REJECTED (older responses also say EXECUTED).
IN_PROGRESS_ORDER_STATUSES = frozenset({"INITIAL", "PENDING", "IN_PROGRESS"})
# Contribution states that will not execute (the web also shows ACTIVE).
INACTIVE_CONTRIBUTION_STATUSES = frozenset({"PAUSED", "FINISHED", "CANCELED", "CANCELLED"})
# Operation types that pay into the cash account (the web flips their sign); the rest charge it.
CREDIT_OPERATIONS = frozenset(
    {
        "EXTERNAL_TRANSFER_REIMBURSEMENT",
        "INTERNAL_TRANSFER_REIMBURSEMENT",
        "INVESTMENT_FUND_REIMBURSEMENT",
        "SALE_VARIABLE_INCOME_CASH",
        "SEPA_RECEIPT",
    }
)
# `contributionTimeFrame.recurrence` → step between executions. Unknown ones only use `nextExecutionDate`.
RECURRENCE_DAYS = {"ONE_WEEK": 7, "TWO_WEEKS": 14}
RECURRENCE_MONTHS = {"ONE_MONTH": 1, "TWO_MONTHS": 2, "THREE_MONTHS": 3, "SIX_MONTHS": 6, "ONE_YEAR": 12}
# Hold `type` of the money withheld by a fund or pension order: paired with its order.
ORDER_HOLD_TYPE = "INVESTMENT_ORDER"
HOLD_LABELS = {ORDER_HOLD_TYPE: "retención por orden de inversión"}
DEFAULT_HOLD_LABEL = "retención"
CENTS = Decimal("0.01")


class Kind(StrEnum):
    """`tipo` of a row."""

    hold = "retencion"
    order = "orden"
    transfer = "transferencia"  # scheduled transfers (#4): not fetched yet, see `upcoming_rows(transfers=…)`
    contribution = "aportacion"


# Same day: what is already withheld first, then what is still to come.
_KIND_RANK = {Kind.hold: 0, Kind.order: 1, Kind.transfer: 2, Kind.contribution: 3}


@dataclass(frozen=True)
class Upcoming:
    """One expected cash event before it becomes a row."""

    day: date
    kind: Kind
    product: str | None
    amount: Decimal | None  # signed: charges negative
    account_id: str | None  # `CashAccount.account_id` it hits, when known
    account_label: str | None = None  # what the API calls that account, shown when no id matches
    pending: bool = True  # not in `enabledBalance` yet, so it lowers `disponible_tras`
    withheld: bool = False  # a hold covers it (the hold itself, or an order paired with its hold)
    hold_type: str | None = None  # the hold's `type`, on a hold or an order paired with one
    withheld_amounts: tuple[Decimal, ...] = ()  # an order's amounts a hold for it may show (cash, amountToBlock)


@dataclass(frozen=True)
class Shortfall:
    """An account whose available balance does not cover what is due."""

    account: str
    since: str  # first day it falls short (ISO)
    missing: Decimal  # the largest shortfall in the window
    available: Decimal


def upcoming_rows(
    *,
    today: date,
    days: int,
    accounts: Sequence[CashAccount],
    contributions: Sequence[Contribution],
    orders: Iterable[tuple[Order, str | None]] = (),
    holds: Mapping[str, Sequence[Hold]] | None = None,
    transfers: Iterable[Upcoming] = (),
    owner: Ownership | None = None,
) -> list[dict[str, Any]]:
    """Rows of `upcoming`, sorted by date, from today to `today + days` (both included).

    `orders` pairs each order with the `account_id` of the cash account it charges; `holds` maps that
    id to the account's holds; `transfers` is the extension point for scheduled transfers. With `owner`,
    only rows charged to accounts of that ownership are kept.
    """
    end = today + timedelta(days=days)
    items = [
        *pair_holds(order_items(orders, end), hold_items(holds or {}, today)),
        *contribution_items(contributions, accounts, today, end),
        *transfers,
    ]
    if owner is not None:
        allowed = {a.account_id for a in accounts if a.ownership is owner}
        items = [i for i in items if i.account_id in allowed]
    return build_rows(items, accounts)


def hold_items(holds: Mapping[str, Sequence[Hold]], today: date) -> list[Upcoming]:
    """Holds have no date: they are money withheld right now."""
    return [
        Upcoming(
            today,
            Kind.hold,
            HOLD_LABELS.get((h.type or "").upper(), DEFAULT_HOLD_LABEL),
            -abs(h.amount),
            account_id,
            pending=False,
            withheld=True,
            hold_type=h.type,
        )
        for account_id, found in holds.items()
        for h in found
    ]


def order_items(orders: Iterable[tuple[Order, str | None]], end: date) -> list[Upcoming]:
    """Orders in progress, dated by `orderDate`, never past `end`.

    Not `effectiveDate`: on pension orders it is a year after the order, and the web never shows it.
    """
    return [
        Upcoming(
            min(o.order_date, end),
            Kind.order,
            o.fund_name or o.product,
            _order_amount(o),
            account_id,
            pending=False,
            withheld_amounts=tuple(_cents(v) for v in (o.cash, o.amount_to_block) if v is not None),
        )
        for o, account_id in orders
        if (o.status or "").upper() in IN_PROGRESS_ORDER_STATUSES
    ]


def _order_amount(o: Order) -> Decimal | None:
    if o.cash is None:
        return None
    return abs(o.cash) if (o.operation_type or "").upper() in CREDIT_OPERATIONS else -abs(o.cash)


def _cents(value: Decimal) -> Decimal:
    return abs(value).quantize(CENTS)


def pair_holds(orders: Sequence[Upcoming], holds: Sequence[Upcoming]) -> list[Upcoming]:
    """Orders, the ones covered by an order hold marked `withheld`, plus the holds left unpaired.

    An `INVESTMENT_ORDER` hold pairs with the first unpaired order (by date) of the same account whose
    cash or `amountToBlock` equals the hold's amount; each hold and order is used once.
    """
    free = list(holds)
    paired: list[Upcoming] = []
    for order in sorted(orders, key=lambda o: (o.day, o.product or "")):
        hit = next((h for h in free if _covers(h, order)), None)
        if hit is None:
            paired.append(order)
            continue
        free.remove(hit)
        paired.append(replace(order, withheld=True, hold_type=hit.hold_type))
    return paired + free


def _covers(hold: Upcoming, order: Upcoming) -> bool:
    return (
        (hold.hold_type or "").upper() == ORDER_HOLD_TYPE
        and order.account_id is not None
        and hold.account_id == order.account_id
        and hold.amount is not None
        and _cents(hold.amount) in order.withheld_amounts
    )


def contribution_items(
    contributions: Sequence[Contribution], accounts: Sequence[CashAccount], start: date, end: date
) -> list[Upcoming]:
    items: list[Upcoming] = []
    for c in contributions:
        frame = c.contribution_time_frame
        if (c.status or "").upper() in INACTIVE_CONTRIBUTION_STATUSES or frame is None:
            continue
        account = charge_account(c, accounts)
        items.extend(
            Upcoming(
                day,
                Kind.contribution,
                c.fund_name or c.isin,
                -abs(c.amount),
                account.account_id if account else None,
                c.from_account_alias,
            )
            for day in contribution_dates(frame, start, end)
        )
    return items


def charge_account(c: Contribution, accounts: Sequence[CashAccount]) -> CashAccount | None:
    """The cash account a contribution is charged to: by `fromAccountId`, else by a unique alias."""
    if c.from_account_id:
        hit = next((a for a in accounts if c.from_account_id in (a.account_id, a.account_uuid)), None)
        if hit:
            return hit
    alias = (c.from_account_alias or "").strip().lower()
    hits = [a for a in accounts if alias and (a.alias or "").strip().lower() == alias]
    return hits[0] if len(hits) == 1 else None


def contribution_dates(frame: ContributionTimeFrame, start: date, end: date) -> Iterator[date]:
    """Executions in [start, end], from `nextExecutionDate` on, every recurrence step, up to `endDate`.

    Only the first is MyInvestor's date; the following ones are estimates (no bank holidays).
    """
    first = frame.next_execution_date
    if first is None:
        return
    last = min(end, frame.end_date) if frame.end_date else end
    n = 0
    while (day := _nth(first, frame.recurrence, n)) is not None and day <= last:
        if day >= start:
            yield day
        n += 1


def _nth(first: date, recurrence: str | None, n: int) -> date | None:
    """The `n`-th execution counted from `first`; None after the first for an unknown recurrence."""
    if n == 0:
        return first
    if recurrence in RECURRENCE_DAYS:
        return first + timedelta(days=RECURRENCE_DAYS[recurrence] * n)
    if recurrence in RECURRENCE_MONTHS:
        return _add_months(first, RECURRENCE_MONTHS[recurrence] * n)
    return None


def _add_months(day: date, months: int) -> date:
    """Same day of the month, or its last day (31 Jan + 1 month = 28 Feb)."""
    year, month = divmod(day.month - 1 + months, 12)
    year += day.year
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def build_rows(items: Iterable[Upcoming], accounts: Sequence[CashAccount]) -> list[dict[str, Any]]:
    """Flat rows sorted by date, with the charge account's balance after the pending items so far."""
    by_id = {a.account_id: a for a in accounts}
    running = {a.account_id: a.enabled_balance for a in accounts}
    rows: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda i: (i.day, _KIND_RANK[i.kind], i.product or "")):
        account = by_id.get(item.account_id) if item.account_id else None
        after: Decimal | None = None
        if account is not None:
            if item.pending and item.amount is not None:
                running[account.account_id] += item.amount
            after = running[account.account_id]
        rows.append(
            {
                "fecha": item.day.isoformat(),
                "tipo": item.kind.value,
                "producto": item.product,
                "importe": item.amount,
                "cuenta_cargo": (account.alias or account.account_id) if account else item.account_label,
                "retenido": item.withheld,
                "disponible": account.enabled_balance if account else None,
                "disponible_tras": after,
                "falta": -after if after is not None and after < 0 else None,
                "tipo_retencion": item.hold_type,
            }
        )
    return rows


def shortfalls(rows: Iterable[dict[str, Any]]) -> list[Shortfall]:
    """Per account (in order of appearance): the first day it falls short and the largest shortfall."""
    found: dict[str, Shortfall] = {}
    for r in rows:
        if r["falta"] is None:
            continue
        name = r["cuenta_cargo"]
        seen = found.get(name)
        found[name] = Shortfall(
            account=name,
            since=seen.since if seen else r["fecha"],
            missing=max(r["falta"], seen.missing) if seen else r["falta"],
            available=r["disponible"],
        )
    return list(found.values())
