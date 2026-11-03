"""FIFO cost of broker sales (`broker_gains`) and the broker order models."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from myinvestor.broker_gains import BUY, SELL, Trade, realise, trade_of
from myinvestor.models import BrokerOrder, BrokerOrderDetail
from tests.test_cli_gains import DNI, GOLD, SILVER, broker_detail, broker_order


def trade(side: str, day: str, shares: str, net: str | None, isin: str = SILVER, account: str = "201") -> Trade:
    return Trade(
        account_id=account,
        reference=f"{side}-{day}",
        isin=isin,
        name=None,
        day=date.fromisoformat(day),
        side=side,
        shares=Decimal(shares),
        price=None,
        gross=None,
        commissions=None,
        net=Decimal(net) if net is not None else None,
    )


def results(*trades: Trade) -> list[tuple[str, Decimal | None, Decimal | None]]:
    return [(s.trade.reference, s.cost, s.result) for s in realise(trades)]


def test_fifo_takes_the_oldest_lots_first_across_several_sales() -> None:
    assert results(
        trade(SELL, "2025-02-01", "6", "90"),
        trade(BUY, "2024-01-01", "4", "40.01"),
        trade(BUY, "2024-06-01", "6", "90"),
        trade(SELL, "2025-03-01", "4", "80"),
    ) == [
        # 4 at 10.0025 + 2 of the 6 at 15 → 40.01 + 30
        ("SELL-2025-02-01", Decimal("70.01"), Decimal("19.99")),
        ("SELL-2025-03-01", Decimal("60.00"), Decimal("20.00")),
    ]


def test_a_purchase_of_the_same_day_goes_before_the_sale() -> None:
    assert results(trade(SELL, "2025-02-01", "1", "12"), trade(BUY, "2025-02-01", "1", "10")) == [
        ("SELL-2025-02-01", Decimal("10.00"), Decimal("2.00"))
    ]


def test_lots_are_per_account_and_security() -> None:
    assert results(
        trade(BUY, "2024-01-01", "1", "10", isin=GOLD),
        trade(BUY, "2024-01-01", "1", "10", account="202"),
        trade(SELL, "2025-01-01", "1", "12"),
    ) == [("SELL-2025-01-01", None, None)]


def test_an_uncovered_sale_leaves_that_security_without_cost_from_then_on() -> None:
    assert results(
        trade(BUY, "2024-01-01", "1", "10"),
        trade(SELL, "2024-06-01", "2", "30"),  # one share came in without a purchase order
        trade(BUY, "2024-07-01", "5", "50"),
        trade(SELL, "2025-01-01", "1", "12"),
    ) == [("SELL-2024-06-01", None, None), ("SELL-2025-01-01", None, None)]


def test_a_purchase_without_amount_leaves_its_security_without_cost() -> None:
    assert results(trade(BUY, "2024-01-01", "1", None), trade(SELL, "2025-01-01", "1", "12")) == [
        ("SELL-2025-01-01", None, None)
    ]


def test_trade_of_an_executed_order() -> None:
    detail = BrokerOrderDetail.model_validate(broker_detail("S1", "SELL", "2025-04-01", 15, 40.0, fees=1.5))
    order = BrokerOrder.model_validate(broker_order("3", "2025-04-01"))

    got = trade_of("201", order, detail)

    assert got is not None
    assert (got.reference, got.isin, got.day, got.side, got.shares) == ("S1", SILVER, date(2025, 4, 1), SELL, 15)
    assert (got.gross, got.commissions, got.net) == (Decimal("600.0"), Decimal("1.5"), Decimal("598.5"))


def test_trade_of_a_signed_net_is_positive_and_a_reference_falls_back_to_the_id() -> None:
    raw = broker_detail("B1", "BUY", "2025-04-01", 1, 10.0, netAmountAccountCurrency=-11.0, orderReference=None)
    got = trade_of(
        "201", BrokerOrder.model_validate(broker_order("7", "2025-04-01")), BrokerOrderDetail.model_validate(raw)
    )

    assert got is not None and (got.reference, got.net) == ("7", Decimal("11.0"))


def test_trade_of_skips_what_was_not_executed() -> None:
    order = BrokerOrder.model_validate(broker_order("3", "2025-04-01", status="PENDING"))
    pending = broker_detail("S1", "SELL", "2025-04-01", 15, 40.0, orderStatus="PENDING", executedShares=0)
    unknown = broker_detail("S1", "SWAP", "2025-04-01", 15, 40.0)

    assert trade_of("201", order, BrokerOrderDetail.model_validate(pending)) is None
    assert trade_of("201", order, BrokerOrderDetail.model_validate(unknown)) is None


def test_broker_models_never_keep_the_dni_and_tolerate_nulls() -> None:
    detail = BrokerOrderDetail.model_validate(broker_detail("S1", "SELL", "2025-04-01", 15, 40.0))
    empty = BrokerOrderDetail.model_validate(dict.fromkeys(broker_detail("S1", "SELL", "2025-04-01", 1, 1.0)))
    order = BrokerOrder.model_validate({**dict.fromkeys(broker_order("1", "2025-01-01")), "id": 42})

    assert DNI not in str(detail.model_dump()) and "ACC-0001" not in str(detail.model_dump())
    assert detail.side == SELL and detail.execution_date == date(2025, 4, 1)
    assert (empty.side, empty.commissions, empty.execution_date) == (None, None, None)
    assert (order.id, order.order_date) == ("42", None)
