"""`fiscal`: tax cost of a fund's lots and the real gain columns (pure)."""

from __future__ import annotations

from decimal import Decimal

from myinvestor.fiscal import columns, lots_cost, percent, strict_total, total_columns
from myinvestor.models import FundLots


def test_lots_cost_sums_every_lot_or_knows_nothing() -> None:
    lots = FundLots.model_validate({"plusMinusList": [{"investment": 100.5}, {"investment": 40}]})
    missing = FundLots.model_validate({"plusMinusList": [{"investment": 100.5}, {"securities": 1}]})

    assert lots_cost(lots) == Decimal("140.5")
    assert lots_cost(missing) is None
    assert lots_cost(FundLots.model_validate({"plusMinusList": []})) is None


def test_columns_and_totals() -> None:
    assert columns(Decimal(110), Decimal(100)) == {
        "coste_fiscal": 100,
        "ganancia_real": 10,
        "rentab_real": Decimal("10.00"),
    }
    assert columns(None, Decimal(100)) == {"coste_fiscal": 100, "ganancia_real": None, "rentab_real": None}
    assert percent(Decimal(5), Decimal(0)) is None
    assert strict_total([]) is None
    assert strict_total([Decimal(1), None]) is None
    rows = [columns(Decimal(110), Decimal(100)), columns(Decimal(45), Decimal(50))]
    assert total_columns(rows) == {"coste_fiscal": 150, "ganancia_real": 5, "rentab_real": Decimal("3.33")}
