from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from myinvestor.allocation import (
    CASH,
    UNCLASSIFIED,
    Grouping,
    Holding,
    from_cash,
    from_investment,
    from_pension,
    group,
    parse_target,
    portfolios_by_cash_account,
    rebalance,
    share,
    total_row,
    unbalanced_keys,
    weighted_ter,
)
from myinvestor.errors import InputError
from myinvestor.models import (
    CashAccount,
    FundSheet,
    Investment,
    PensionAccount,
    PensionPlanPosition,
    PensionPlanSheet,
    SecuritiesAccount,
)
from tests.conftest import cash_account, investment, securities_account


def holding(product: str, value: str, **overrides: Any) -> Holding:
    base: dict[str, Any] = {
        "product": product,
        "name": f"Fondo {product}",
        "account_id": "201",
        "account_name": "S201",
        "account_kind": "valores",
        "value": Decimal(value),
        "invested": Decimal(value) - 10,
        "profit": Decimal(10),
    }
    return Holding(**{**base, **overrides})


def sec(**overrides: Any) -> SecuritiesAccount:
    return SecuritiesAccount.model_validate(securities_account(**overrides))


def inv(isin: str, kind: str = "INDEXED_FUND", **overrides: Any) -> Investment:
    return Investment.model_validate(investment(isin, kind, **overrides))


def test_group_by_asset_weights_and_weighted_ter() -> None:
    holdings = [
        holding("A", "600", asset="VARIABLE_INCOME", ter=Decimal("0.10")),
        holding("B", "300", asset="VARIABLE_INCOME", ter=Decimal("0.40")),
        holding("C", "100", asset="FIXED_INCOME", ter=None),
    ]

    rows = group(holdings, Grouping.asset)

    assert rows == [
        {
            "clave": "VARIABLE_INCOME",
            "valor": Decimal("900"),
            "peso_pct": Decimal("90.00"),
            "invertido": Decimal("880"),
            "ganancia": Decimal("20"),
            "ter": Decimal("0.2000"),  # (600 * 0.10 + 300 * 0.40) / 900
        },
        {
            "clave": "FIXED_INCOME",
            "valor": Decimal("100"),
            "peso_pct": Decimal("10.00"),
            "invertido": Decimal("90"),
            "ganancia": Decimal("10"),
            "ter": None,
        },
    ]


def test_total_row_has_portfolio_ter_over_known_ter_only() -> None:
    holdings = [
        holding("A", "600", ter=Decimal("0.10")),
        holding("B", "300", ter=Decimal("0.40")),
        holding(CASH, "1000", invested=Decimal(1000), profit=Decimal(0)),
    ]
    assert total_row(holdings, Grouping.product) == {
        "clave": "total",
        "nombre": None,
        "valor": Decimal("1900"),
        "peso_pct": Decimal("100.00"),
        "invertido": Decimal("1880"),
        "ganancia": Decimal("20"),
        "ter": Decimal("0.2000"),
    }


def test_weighted_ter_and_share_edge_cases() -> None:
    assert weighted_ter([holding("A", "0", ter=Decimal("0.2"))]) is None
    assert weighted_ter([holding("A", "100")]) is None
    assert share(Decimal(1), Decimal(0)) is None
    assert share(Decimal(1), Decimal(3)) == Decimal("33.33")


def test_group_by_product_and_account_add_label_columns() -> None:
    holdings = [
        holding("A", "100"),
        holding("B", "300", account_id="202", account_name="METAL", account_kind="cartera"),
        holding("A", "50", account_id="202", account_name="METAL", account_kind="cartera"),
    ]

    by_product = group(holdings, Grouping.product)
    by_account = group(holdings, Grouping.account)

    assert [(r["clave"], r["nombre"], r["valor"]) for r in by_product] == [
        ("B", "Fondo B", Decimal("300")),
        ("A", "Fondo A", Decimal("150")),
    ]
    assert [(r["clave"], r["cuenta_id"], r["tipo"], r["valor"]) for r in by_account] == [
        ("METAL", "202", "cartera", Decimal("350")),
        ("S201", "201", "valores", Decimal("100")),
    ]
    assert list(total_row(holdings, Grouping.account))[:3] == ["clave", "cuenta_id", "tipo"]


def test_unknown_amounts_stay_unknown() -> None:
    [row] = group([holding("A", "100", invested=None, profit=None)], Grouping.asset)
    assert (row["clave"], row["invertido"], row["ganancia"]) == (UNCLASSIFIED, None, None)


def test_fund_is_classified_with_its_sheet() -> None:
    sheet = FundSheet.model_validate(
        {"assetType": "VARIABLE_INCOME", "geoZone": "GLOBAL", "category": "RV Global", "ter": Decimal("0.12")}
    )

    h = from_investment(sec(), inv("IE0000000001"), sheet)

    assert (h.asset, h.geo, h.category, h.ter) == ("VARIABLE_INCOME", "GLOBAL", "RV Global", Decimal("0.12"))
    assert (h.product, h.account_id, h.account_name, h.account_kind) == ("IE0000000001", "201", "S201", "valores")
    assert (h.value, h.invested, h.profit) == (Decimal("1100.55"), Decimal("1000.0"), Decimal("100.55"))


def test_fund_without_sheet_is_unclassified_but_keeps_myinvestor_category() -> None:
    h = from_investment(sec(), inv("IE0000000001", myInvestorCategory="INDEXADO"), None)
    assert (h.asset, h.geo, h.category, h.ter) == (UNCLASSIFIED, UNCLASSIFIED, "INDEXADO", None)


@pytest.mark.parametrize(("broker_type", "asset"), [("STOCK", "VARIABLE_INCOME"), ("ETF", "ETF"), (None, UNCLASSIFIED)])
def test_broker_asset_comes_from_its_product_type(broker_type: str | None, asset: str) -> None:
    h = from_investment(sec(), inv("US0000000001", "BROKER", brokerProductType=broker_type), None)
    assert (h.asset, h.geo, h.ter) == (asset, UNCLASSIFIED, None)


def test_broker_without_isin_uses_ticker() -> None:
    stock = Investment.model_validate({**investment("X", "BROKER"), "isin": None, "ticker": "TICK"})
    h = from_investment(sec(), stock, None)
    assert h.product == "TICK"


def test_managed_portfolio_uses_its_name_and_owns_its_cash_account() -> None:
    metal = sec(account_id="202", accountType="SECURITIES_PORTFOLIO", portfolioName="METAL", cashAccountId="102")
    plain = sec(cashAccountId="101")
    cash = CashAccount.model_validate(cash_account(2))  # accountId "102"

    h = from_investment(metal, inv("IE0000000001"), None)
    portfolios = portfolios_by_cash_account([metal, plain])
    money = from_cash(cash, portfolios.get(cash.account_id))
    loose = from_cash(CashAccount.model_validate(cash_account(1)), portfolios.get("101"))

    assert (h.account_id, h.account_name, h.account_kind) == ("202", "METAL", "cartera")
    assert list(portfolios) == ["102"]
    assert (money.account_id, money.account_name, money.account_kind) == ("202", "METAL", "cartera")
    assert (money.product, money.asset, money.geo, money.category) == (CASH, CASH, CASH, CASH)
    assert (money.value, money.invested, money.profit) == (Decimal("1000.30"), Decimal("1000.30"), Decimal(0))
    assert (loose.account_id, loose.account_name, loose.account_kind) == ("101", "Cuenta 1", CASH)


def test_pension_plan_is_classified_with_its_sheet() -> None:
    account = PensionAccount.model_validate({"accountId": "301", "externalAccountCode": "P301"})
    plan = PensionPlanPosition.model_validate(
        {"dgsCode": "N0001", "name": "Plan", "marketValue": 1500.0, "totalInvested": 1000.0, "profit": 500.0}
    )
    sheet = PensionPlanSheet.model_validate({"activeType": "RENTA VARIABLE", "category": "RV MIXTA"})

    h = from_pension(account, plan, sheet)
    bare = from_pension(account, plan, None)

    assert (h.product, h.account_name, h.account_kind, h.asset, h.category, h.geo) == (
        "N0001",
        "P301",
        "pensiones",
        "RENTA VARIABLE",
        "RV MIXTA",
        UNCLASSIFIED,
    )
    assert (bare.asset, bare.category, bare.value) == (UNCLASSIFIED, UNCLASSIFIED, Decimal("1500.0"))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([80, 20], "objeto JSON"),
        ({"A": "80", "B": 20}, "número"),
        ({"A": True}, "número"),
        ({"A": -1, "B": 101}, "negativo"),
        ({"A": 80, "B": 10}, "suman 90"),
        ({}, "vacío"),
        ({"": 100}, "clave"),
    ],
)
def test_parse_target_rejects_bad_files(data: Any, message: str) -> None:
    with pytest.raises(InputError, match=message):
        parse_target(data)


def test_parse_target_accepts_ints_and_decimals() -> None:
    assert parse_target({"A": 60, "B": Decimal("39.995"), "C": 0}) == {
        "A": Decimal(60),
        "B": Decimal("39.995"),
        "C": Decimal(0),
    }


def test_rebalance_without_selling() -> None:
    holdings = [holding("A", "700", asset="RV"), holding("B", "300", asset="RF")]
    rows = group(holdings, Grouping.asset)

    result = rebalance(rows, {"RV": Decimal(60), "RF": Decimal(40)}, Grouping.asset)

    # smallest total where RV is not above 60 % without selling: 700 / 0.6 = 1166.67, so RF goes to 466.67
    assert [(r["clave"], r["objetivo_pct"], r["desviacion_pct"]) for r in result] == [
        ("RV", Decimal(60), Decimal("10.00")),
        ("RF", Decimal(40), Decimal("-10.00")),
    ]
    assert result[0]["aportar"].quantize(Decimal("0.01")) == Decimal("0.00")
    assert result[1]["aportar"].quantize(Decimal("0.01")) == Decimal("166.67")


def test_rebalance_adds_missing_targets_and_leaves_out_untargeted_keys() -> None:
    holdings = [
        holding("A", "800", asset="RV"),
        holding("B", "200", asset="RF"),
        holding("C", "500", asset="OTRO"),
    ]
    rows = group(holdings, Grouping.asset)
    targets = {"RV": Decimal(80), "RF": Decimal(10), "ORO": Decimal(10)}

    result = rebalance(rows, targets, Grouping.asset)

    by_key = {r["clave"]: r for r in result}
    assert list(by_key) == ["RV", "OTRO", "RF", "ORO"]
    # base = RV + RF (OTRO has no target): 1000; RF at 20 % already sets the final base at 2000
    assert by_key["RF"]["aportar"] == Decimal(0)
    assert by_key["RV"]["aportar"] == Decimal(800)
    assert by_key["ORO"]["aportar"] == Decimal(200)
    assert (by_key["ORO"]["valor"], by_key["ORO"]["peso_pct"], by_key["ORO"]["desviacion_pct"]) == (
        Decimal(0),
        Decimal("0.00"),
        Decimal("-10.00"),
    )
    assert (by_key["OTRO"]["objetivo_pct"], by_key["OTRO"]["desviacion_pct"], by_key["OTRO"]["aportar"]) == (
        None,
        None,
        None,
    )
    assert unbalanced_keys(rows, targets) == ["OTRO"]


def test_rebalance_with_nothing_held_needs_no_contribution() -> None:
    [row] = rebalance([], {"RV": Decimal(100)}, Grouping.product)
    assert (row["clave"], row["nombre"], row["valor"], row["peso_pct"], row["aportar"]) == (
        "RV",
        None,
        Decimal(0),
        None,
        Decimal(0),
    )
