"""Realised gains of broker sales (stocks, ETF, ETC), valued FIFO from the executions of their orders.

Unlike a fund order, a broker order carries no tax result, so the cost of each sale is the FIFO
cost (the Spanish rule for homogeneous securities) of the shares bought before it in the same
securities account. Fees count: a purchase costs its net amount (price plus fees) and a sale
yields its net amount (price minus fees), both in the cash account's currency (euros).

Not covered: the two-month rule that defers a loss when the same security is bought back
(art. 33.5 LIRPF), splits and other corporate events, and shares that came in without a purchase
order (a securities transfer from another broker). A sale the known purchases do not cover has
no cost (`None`), and neither has any later sale of that security in that account.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from myinvestor.models import BrokerOrder, BrokerOrderDetail

BUY, SELL = "BUY", "SELL"  # `orderOperationTypeEnum` of a broker order
CENTS = Decimal("0.01")


@dataclass(frozen=True)
class Trade:
    """The executed part of one broker order. `net` is in euros and always positive."""

    account_id: str
    reference: str
    isin: str
    name: str | None
    day: date
    side: str
    shares: Decimal
    price: Decimal | None
    gross: Decimal | None
    commissions: Decimal | None
    net: Decimal | None


@dataclass(frozen=True)
class Sale:
    """A sale with its FIFO cost; both `None`-able when the purchases do not cover it."""

    trade: Trade
    cost: Decimal | None

    @property
    def result(self) -> Decimal | None:
        if self.cost is None or self.trade.net is None:
            return None
        return self.trade.net - self.cost


def trade_of(account_id: str, order: BrokerOrder, detail: BrokerOrderDetail) -> Trade | None:
    """What an order really bought or sold; `None` if nothing was executed or it is not a buy/sell."""
    side = (detail.side or "").upper()
    shares = detail.executed_shares or (detail.shares if (detail.order_status or "").upper() == "COMPLETE" else None)
    day = detail.execution_date or detail.order_date or order.order_date
    product = detail.instrument_isin or detail.instrument_name or order.tool_name
    if side not in (BUY, SELL) or not shares or shares <= 0 or day is None or not product:
        return None
    net = detail.net_amount_account_currency
    return Trade(
        account_id=account_id,
        reference=detail.order_reference or order.id,
        isin=product,
        name=detail.instrument_name or order.tool_name,
        day=day,
        side=side,
        shares=shares,
        price=detail.price_currency,
        gross=detail.gross_amount_operation_currency,
        commissions=detail.commissions,
        net=abs(net) if net is not None else None,
    )


def realise(trades: Iterable[Trade]) -> list[Sale]:
    """Every sale, oldest first, with the FIFO cost of the shares it took out.

    Purchases of a day go before its sales. Once a sale cannot be valued (a purchase without
    amount, or fewer shares bought than sold), the lots of that security are unknown from then on.
    """
    lots: dict[tuple[str, str], deque[tuple[Decimal, Decimal]]] = defaultdict(deque)
    unknown: set[tuple[str, str]] = set()
    sales: list[Sale] = []
    for trade in sorted(trades, key=lambda t: (t.day, t.side != BUY, t.reference)):
        key = (trade.account_id, trade.isin)
        if trade.side == BUY:
            if trade.net is None:
                unknown.add(key)
            else:
                lots[key].append((trade.shares, trade.net))
            continue
        cost = None if key in unknown else _take(lots[key], trade.shares)
        if cost is None:
            unknown.add(key)
        sales.append(Sale(trade, cost))
    return sales


def _take(lots: deque[tuple[Decimal, Decimal]], shares: Decimal) -> Decimal | None:
    """Cost of `shares` taken from the oldest lots (which shrink); `None` if there are not enough."""
    if sum((held for held, _ in lots), Decimal(0)) < shares:
        return None
    cost, left = Decimal(0), shares
    while left > 0:
        held, paid = lots.popleft()
        used = min(held, left)
        cost += paid * used / held
        left -= used
        if used < held:
            lots.appendleft((held - used, paid * (held - used) / held))
    return cost.quantize(CENTS, ROUND_HALF_UP)
