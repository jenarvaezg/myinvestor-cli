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
    unlocated,
    untranslated_labels,
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
from myinvestor.regions import NOT_APPLICABLE, zone_label
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
        holding("A", "600", asset="renta_variable", asset_label="VARIABLE_INCOME", ter=Decimal("0.10")),
        holding("B", "300", asset="renta_variable", asset_label="Renta Variable", ter=Decimal("0.40")),
        holding("C", "100", asset="renta_fija", ter=None),
    ]

    rows = group(holdings, Grouping.asset)

    assert rows == [
        {
            "clave": "renta_variable",
            "origen": "Renta Variable, VARIABLE_INCOME",
            "valor": Decimal("900"),
            "peso_pct": Decimal("90.00"),
            "invertido": Decimal("880"),
            "ganancia": Decimal("20"),
            "ter": Decimal("0.2000"),  # (600 * 0.10 + 300 * 0.40) / 900
        },
        {
            "clave": "renta_fija",
            "origen": None,
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

    assert (h.asset, h.asset_label, h.geo, h.category, h.ter) == (
        "renta_variable",
        "VARIABLE_INCOME",
        "Global",
        "RV Global",
        Decimal("0.12"),
    )
    assert (h.product, h.account_id, h.account_name, h.account_kind) == ("IE0000000001", "201", "S201", "valores")
    assert (h.value, h.invested, h.profit) == (Decimal("1100.55"), Decimal("1000.0"), Decimal("100.55"))


def test_fund_without_sheet_is_unclassified_but_keeps_myinvestor_category() -> None:
    h = from_investment(sec(), inv("IE0000000001", myInvestorCategory="INDEXADO"), None)
    assert (h.asset, h.geo, h.category, h.ter) == (UNCLASSIFIED, UNCLASSIFIED, "INDEXADO", None)


@pytest.mark.parametrize(
    ("broker_type", "name", "asset", "label"),
    [
        ("RV", "ACME MOTORS", "renta_variable", "RV"),  # a share
        ("ETF", "EUR HEDGED SILVER", "materias_primas", "ETF: SILVER"),
        ("ETF", "PHYSICAL GOLD ETC", "materias_primas", "ETF: PHYSICAL"),
        ("ETF", "CORE MSCI WORLD UCITS ETF", "renta_variable", "ETF: MSCI"),
        ("ETF", "THEMATIC UCITS ETF", UNCLASSIFIED, "ETF"),  # never «ETF» as the class
        (None, "SOMETHING", UNCLASSIFIED, "BROKER"),
    ],
)
def test_broker_asset_comes_from_its_product_type_or_underlying(
    broker_type: str | None, name: str, asset: str, label: str
) -> None:
    position = inv("US0000000001", "BROKER", brokerProductType=broker_type, investmentName=name)
    h = from_investment(sec(), position, None)
    assert (h.asset, h.asset_label, h.ter) == (asset, label, None)
    assert untranslated_labels([h]) == ([label] if asset == UNCLASSIFIED else [])


@pytest.mark.parametrize(
    ("isin", "broker_type", "name", "geo", "label"),
    [
        ("US0000000001", "RV", "ACME MOTORS", "Estados Unidos", "ISIN: US"),  # a share: its issuer country
        ("NL0000000001", "RV", "ACME GLOBAL NV", UNCLASSIFIED, None),  # no zone of its own; never the name
        ("IE0000000001", "ETF", "CORE MSCI WORLD UCITS ETF", "Global", "nombre: WORLD"),
        ("IE0000000002", "ETF", "CORE S&P 500 UCITS ETF", "Estados Unidos", "nombre: S&P 500"),
        ("IE0000000003", "ETF", "PHYSICAL GOLD ETC", NOT_APPLICABLE, "materias_primas"),
        ("IE0000000004", "ETF", "THEMATIC UCITS ETF", UNCLASSIFIED, None),
    ],
)
def test_broker_region(isin: str, broker_type: str, name: str, geo: str, label: str | None) -> None:
    h = from_investment(sec(), inv(isin, "BROKER", brokerProductType=broker_type, investmentName=name), None)
    assert (h.geo, h.geo_label) == (geo, label)
    assert unlocated([h]) == ([name] if geo == UNCLASSIFIED else [])


@pytest.mark.parametrize(
    ("sheet", "geo", "label"),
    [
        ({"assetType": "VARIABLE_INCOME", "geoZone": "GLOBAL"}, "Global", "geoZone"),
        ({"assetType": "VARIABLE_INCOME", "geoZone": "OTHERS", "name": "Fondo Japón"}, "Japón", "nombre: JAPON"),
        ({"assetType": "COMMODITIES", "geoZone": "GLOBAL"}, NOT_APPLICABLE, "materias_primas"),
        ({"assetType": "FIXED_INCOME"}, UNCLASSIFIED, None),
    ],
)
def test_fund_region_is_its_geo_zone_or_its_name(sheet: dict[str, Any], geo: str, label: str | None) -> None:
    h = from_investment(sec(), inv("IE0000000001"), FundSheet.model_validate(sheet))
    assert (h.geo, h.geo_label) == (geo, label)


def test_broker_unknown_category_is_unclassified() -> None:
    position = inv("US0000000001", "BROKER", brokerProductType="RV", myInvestorCategory="Desconocido")
    assert from_investment(sec(), position, None).category == UNCLASSIFIED


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
    assert (money.product, money.asset, money.geo, money.geo_label, money.category) == (
        CASH,
        CASH,
        NOT_APPLICABLE,
        CASH,
        CASH,
    )
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
        "renta_variable",
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


def pension(dgs: str, name: str, value: float, sheet: dict[str, Any] | None) -> Holding:
    account = PensionAccount.model_validate({"accountId": "301", "externalAccountCode": "P301"})
    plan = PensionPlanPosition.model_validate({"dgsCode": dgs, "name": name, "marketValue": value})
    return from_pension(account, plan, PensionPlanSheet.model_validate(sheet) if sheet is not None else None)


def test_pension_plans_get_a_region_from_their_category_or_name() -> None:
    world = pension("N5396", "MyInvestor Indexado Global, PP", 6000.0, {"activeType": "Renta Variable"})
    usa = pension(
        "N5394",
        "MyInvestor Indexado S&P 500, PP",
        2000.0,
        {"activeType": "Renta Variable", "category": "RV Internacional PP"},  # the category says nothing
    )
    by_category = pension("N0001", "Plan", 1000.0, {"category": "RV Global PP"})
    without_sheet = pension("N5394", "MyInvestor Indexado S&P 500, PP", 1000.0, None)
    gold = holding("ETC", "1000", asset="materias_primas", geo=NOT_APPLICABLE, geo_label="materias_primas")

    assert [(h.geo, h.geo_label) for h in (world, usa, by_category, without_sheet)] == [
        ("Global", "nombre: GLOBAL"),
        ("Estados Unidos", "nombre: S&P 500"),
        ("Global", "categoría: GLOBAL"),
        ("Estados Unidos", "nombre: S&P 500"),
    ]
    rows = group([world, usa, by_category, gold], Grouping.geo)
    assert [(r["clave"], r["origen"], r["peso_pct"]) for r in rows] == [
        ("Global", "categoría: GLOBAL, nombre: GLOBAL", Decimal("70.00")),
        ("Estados Unidos", "nombre: S&P 500", Decimal("20.00")),
        (NOT_APPLICABLE, "materias_primas", Decimal("10.00")),
    ]
    assert list(total_row([world], Grouping.geo))[:2] == ["clave", "origen"]


def test_the_most_specific_region_wins_over_a_generic_category() -> None:
    """The real sheet of N5394 (S&P 500) says «RV Global PP»: the name decides (seen with the API)."""
    usa = pension(
        "N5394", "MyInvestor Indexado S&P500, PP", 2000.0, {"activeType": "Renta Variable", "category": "RV Global PP"}
    )

    assert (usa.geo, usa.geo_label) == ("Estados Unidos", "nombre: S&P500")


@pytest.mark.parametrize("zone", ["Europa", "EUROPE", " europe "])
def test_a_geo_zone_code_and_its_label_are_one_key(zone: str) -> None:
    """Fund sheets send the label («Europa»), the searcher the code: both group under the web's label."""
    assert zone_label(zone) == "Europa"
    assert zone_label("Zona rara") == "Zona rara"
