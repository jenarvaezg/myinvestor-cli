"""`coste_fiscal`, `ganancia_real` and `rentab_real`: the gain over the cost that transfers carry over. No I/O.

`invertido` is what MyInvestor shows, and after a transfer between funds (or a mobilisation between
pension plans) the destination's starts at the money transferred, its market value that day. The tax
cost (`coste_fiscal`, the acquisition cost the tax office uses) is the one the source had, which the
transfer carries over: a fund's is the sum of the cost of its open lots (`fund_lots`), a pension plan's
is rebuilt from its history (`plan_costs`). `ganancia_real` = value - tax cost; `rentab_real`, its percent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from myinvestor.models import FundLots

COST, GAIN, RETURN = "coste_fiscal", "ganancia_real", "rentab_real"
FISCAL_KEYS = (COST, GAIN, RETURN)
BLANK: Mapping[str, None] = dict.fromkeys(FISCAL_KEYS)  # a row whose tax cost does not apply (cash)
HUNDRED = Decimal(100)
PCT_STEP = Decimal("0.01")


def lots_cost(lots: FundLots) -> Decimal | None:
    """Sum of the cost of a fund's open lots; None without lots or if any lacks its cost."""
    costs = [lot.investment for lot in lots.plus_minus_list]
    if not costs or any(c is None for c in costs):
        return None
    return sum((c for c in costs if c is not None), Decimal(0))


def strict_total(values: Iterable[Decimal | None]) -> Decimal | None:
    """The sum, or None if any value (or every one, when there are none) is unknown."""
    known = list(values)
    if not known or any(v is None for v in known):
        return None
    return sum((v for v in known if v is not None), Decimal(0))


def percent(gain: Decimal | None, cost: Decimal | None) -> Decimal | None:
    """`gain` as a percent of `cost`, to cents; None when either is unknown or the cost is zero."""
    if gain is None or not cost:
        return None
    return (gain * HUNDRED / cost).quantize(PCT_STEP, rounding=ROUND_HALF_UP)


def columns(value: Decimal | None, cost: Decimal | None) -> dict[str, Decimal | None]:
    """`{coste_fiscal, ganancia_real, rentab_real}` of a position worth `value`."""
    gain = value - cost if value is not None and cost is not None else None
    return {COST: cost, GAIN: gain, RETURN: percent(gain, cost)}


def total_columns(rows: Iterable[Mapping[str, Any]]) -> dict[str, Decimal | None]:
    """The three columns of a total: the sums of the rows' cost and gain (None if any is unknown)."""
    chosen = list(rows)
    cost, gain = strict_total(r[COST] for r in chosen), strict_total(r[GAIN] for r in chosen)
    return {COST: cost, GAIN: gain, RETURN: percent(gain, cost)}
