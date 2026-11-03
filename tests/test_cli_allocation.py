"""`allocation` and the `peso`/`ter` columns of `holdings`, against a synthetic portfolio."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.errors import InputError
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import CPERF, cash_account, investment, ok, securities_account

runner = CliRunner()
FUND_A, FUND_B, ETF, STOCK = "IE0000000001", "LU0000000002", "IE0000000003", "US0000000004"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def json_rows(*args: str) -> list[dict[str, Any]]:
    result = invoke(*args, "-f", "json")
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout, parse_float=Decimal)
    return rows


def position(isin: str, value: float, kind: str = "INDEXED_FUND", **extra: Any) -> dict[str, Any]:
    return investment(isin, kind, marketValue=value, initialInvestment=value - 10, profit=10.0, **extra)


def mock_portfolio(mock_api: respx.MockRouter) -> dict[str, respx.Route]:
    """201: a fund, an ETF and a stock; 202: the «METAL» managed portfolio, with cash account 102; plan N0001."""
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(
        200, json=ok([{"accountId": "201"}, {"accountId": "202"}])
    )
    plain = securities_account(
        "201",
        cashAccountId="101",
        securitiesAccountInvestments={
            "INDEXED_FUND": {"investmentList": [position(FUND_A, 600.0)]},
            "BROKER": {
                "investmentList": [
                    position(ETF, 100.0, "BROKER", brokerProductType="ETF", investmentName="PHYSICAL GOLD ETC"),
                    position(STOCK, 100.0, "BROKER", brokerProductType="RV", investmentName="ACME MOTORS"),
                ]
            },
        },
    )
    metal = securities_account(
        "202",
        accountType="SECURITIES_PORTFOLIO",
        portfolioName="METAL",
        cashAccountId="102",
        securitiesAccountInvestments={
            "INDEXED_FUND": {"investmentList": [position(FUND_A, 200.0), position(FUND_B, 300.0)]},
        },
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(plain))
    mock_api.get(f"{CPERF}/v2/securities-accounts/202").respond(200, json=ok(metal))
    sheet_a = {"isinCode": FUND_A, "assetType": "VARIABLE_INCOME", "geoZone": "GLOBAL", "category": "RV Global"}
    sheet_b = {"isinCode": FUND_B, "assetType": "FIXED_INCOME", "geoZone": "EUROPE", "category": "RF Euro"}
    routes = {
        FUND_A: mock_api.get(f"{CPERF}/v2/funds/{FUND_A}").respond(200, json=ok({**sheet_a, "ter": 0.10})),
        FUND_B: mock_api.get(f"{CPERF}/v2/funds/{FUND_B}").respond(200, json=ok({**sheet_b, "ter": 0.20})),
        ETF: mock_api.get(f"{CPERF}/v2/funds/{ETF}").respond(404, json={}),
        "plan": mock_api.get(f"{CPERF}/v2/pension-plans/N0001").respond(
            200, json=ok({"dgsCode": "N0001", "activeType": "RENTA VARIABLE", "category": "RV MIXTA"})
        ),
        "cash": mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(
            200, json=ok([cash_account(1), cash_account(2)])
        ),
    }
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(
        200, json=ok([{"accountId": "301", "externalAccountCode": "P301"}])
    )
    plans = [{"dgsCode": "N0001", "name": "Plan", "marketValue": 700.0, "totalInvested": 600.0, "profit": 100.0}]
    mock_api.get(f"{CPERF}/v2/pension-accounts/301").respond(200, json=ok(plans))
    return routes


def test_allocation_by_asset(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    routes = mock_portfolio(mock_api)

    rows = json_rows("allocation")

    # equity: fund A in both accounts (VARIABLE_INCOME), the share (RV) and the plan («RENTA VARIABLE»)
    assert [(r["clave"], r["origen"], r["valor"], r["peso_pct"]) for r in rows] == [
        ("renta_variable", "RENTA VARIABLE, RV, VARIABLE_INCOME", Decimal("1600.00"), Decimal("80.00")),
        ("renta_fija", "FIXED_INCOME", Decimal("300.00"), Decimal("15.00")),
        ("materias_primas", "ETF: PHYSICAL", Decimal("100.00"), Decimal("5.00")),
        ("total", None, Decimal("2000.00"), Decimal("100.00")),
    ]
    total = rows[-1]
    assert (total["invertido"], total["ganancia"]) == (Decimal("1850.00"), Decimal("150.00"))
    assert total["ter"] == Decimal("0.1273")  # (800 * 0.10 + 300 * 0.20) / 1100: funds only
    assert rows[0]["ter"] == Decimal("0.1000")  # the share and the plan have no TER: they do not dilute it
    assert all("consultado_en" in r for r in rows)
    # one sheet per distinct fund; ETFs and stocks are classified without one; no cash by default
    assert (routes[FUND_A].call_count, routes[FUND_B].call_count, routes[ETF].call_count) == (1, 1, 0)
    assert routes["plan"].call_count == 1 and routes["cash"].call_count == 0


@pytest.mark.parametrize(
    ("by", "keys"),
    [
        ("geo", ["sin_clasificar", "GLOBAL", "EUROPE"]),
        ("category", ["RV Global", "RV MIXTA", "RF Euro", "sin_clasificar"]),
    ],
)
def test_allocation_by_geo_and_category(
    mock_api: respx.MockRouter, store: MemorySessionStore, by: str, keys: list[str]
) -> None:
    mock_portfolio(mock_api)
    assert [r["clave"] for r in json_rows("allocation", "--by", by)] == [*keys, "total"]


def test_allocation_by_product_needs_no_plan_sheet(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    routes = mock_portfolio(mock_api)

    rows = json_rows("allocation", "--by", "product")

    assert [(r["clave"], r["nombre"], r["valor"], r["ter"]) for r in rows] == [
        (FUND_A, f"Fondo {FUND_A}", Decimal("800.00"), Decimal("0.1000")),
        ("N0001", "Plan", Decimal("700.00"), None),
        (FUND_B, f"Fondo {FUND_B}", Decimal("300.00"), Decimal("0.2000")),
        (ETF, "PHYSICAL GOLD ETC", Decimal("100.00"), None),
        (STOCK, "ACME MOTORS", Decimal("100.00"), None),
        ("total", None, Decimal("2000.00"), Decimal("0.1273")),
    ]
    assert routes["plan"].call_count == 0


def test_allocation_by_account_groups_portfolio_with_its_cash(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_portfolio(mock_api)

    rows = json_rows("allocation", "--by", "account", "--include-cash")

    assert [(r["clave"], r["cuenta_id"], r["tipo"], r["valor"]) for r in rows] == [
        ("METAL", "202", "cartera", Decimal("1500.30")),  # 500 in funds + its cash account 102
        ("Cuenta 1", "101", "efectivo", Decimal("1000.30")),
        ("S201", "201", "valores", Decimal("800.00")),
        ("P301", "301", "pensiones", Decimal("700.00")),
        ("total", None, None, Decimal("4000.60")),
    ]


def test_allocation_include_cash_is_its_own_class(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    rows = {r["clave"]: r for r in json_rows("allocation", "--include-cash")}
    assert (rows["efectivo"]["valor"], rows["efectivo"]["ganancia"]) == (Decimal("2000.60"), Decimal("0.00"))
    assert rows["total"]["valor"] == Decimal("4000.60")


def test_allocation_target_shows_deviation_and_contribution(
    mock_api: respx.MockRouter, store: MemorySessionStore, tmp_path: Path
) -> None:
    mock_portfolio(mock_api)
    target = tmp_path / "objetivo.json"
    target.write_text(json.dumps({"renta_variable": 70, "renta_fija": 20, "monetario": 10}))

    result = invoke("allocation", "--target", str(target), "-f", "json")

    rows = {r["clave"]: r for r in json.loads(result.stdout, parse_float=Decimal)}
    # base = 1600 + 300 = 1900 (commodities have no target); equity at 1600 / 0.7 sets the final one at 2285.71
    assert (rows["renta_variable"]["desviacion_pct"], rows["renta_variable"]["aportar"]) == (
        Decimal("14.21"),
        Decimal("0.00"),
    )
    assert (rows["renta_fija"]["desviacion_pct"], rows["renta_fija"]["aportar"]) == (
        Decimal("-4.21"),
        Decimal("157.14"),
    )
    assert (rows["monetario"]["valor"], rows["monetario"]["aportar"]) == (Decimal("0.00"), Decimal("228.57"))
    assert rows["materias_primas"]["objetivo_pct"] is None and rows["materias_primas"]["aportar"] is None
    assert (rows["total"]["objetivo_pct"], rows["total"]["aportar"]) == (Decimal(100), Decimal("385.71"))
    assert "materias_primas" in result.stderr


def test_allocation_reports_untranslated_asset_labels(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    mock_api.get(f"{CPERF}/v2/pension-plans/N0001").respond(200, json=ok({"activeType": "Garantizado"}))

    result = invoke("allocation", "-f", "json")

    rows = {r["clave"]: r for r in json.loads(result.stdout, parse_float=Decimal)}
    assert (rows["sin_clasificar"]["origen"], rows["sin_clasificar"]["valor"]) == ("Garantizado", Decimal("700.00"))
    assert "«Garantizado»" in result.stderr


@pytest.mark.parametrize(
    ("content", "message"), [("{", "no es JSON válido"), ('{"A": 50}', "suman 50"), (None, "No se puede leer")]
)
def test_allocation_bad_target_fails_before_any_request(
    mock_api: respx.MockRouter, store: MemorySessionStore, tmp_path: Path, content: str | None, message: str
) -> None:
    target = tmp_path / "objetivo.json"
    if content is not None:
        target.write_text(content)

    with pytest.raises(InputError, match=message):
        invoke("allocation", "--target", str(target))

    assert not mock_api.calls


def test_allocation_table_has_total(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_portfolio(mock_api)
    monkeypatch.setenv("COLUMNS", "200")
    result = invoke("allocation", "--by", "geo")
    assert "Reparto por zona" in result.stdout and "TOTAL" in result.stdout and "2.000,00 €" in result.stdout
    assert "45,00 %" in result.stdout and "consultado_en" not in result.stdout


def test_holdings_weight_over_listed_positions(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    routes = mock_portfolio(mock_api)

    rows = json_rows("holdings")
    funds = json_rows("holdings", "--kind", "fondos")

    assert [(r["isin"], r["peso"]) for r in rows] == [
        (FUND_A, Decimal("46.15")),  # 600 of 1300
        (FUND_B, Decimal("23.08")),
        (FUND_A, Decimal("15.38")),
        (ETF, Decimal("7.69")),
        (STOCK, Decimal("7.69")),
    ]
    assert [r["peso"] for r in funds] == [Decimal("54.55"), Decimal("27.27"), Decimal("18.18")]
    assert "ter" not in rows[0] and routes[FUND_A].call_count == 0


def test_holdings_ter_costs_one_request_per_fund(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    routes = mock_portfolio(mock_api)

    rows = json_rows("holdings", "--ter")

    assert [(r["isin"], r["ter"]) for r in rows] == [
        (FUND_A, Decimal("0.10")),
        (FUND_B, Decimal("0.20")),
        (FUND_A, Decimal("0.10")),
        (ETF, None),
        (STOCK, None),
    ]
    assert (routes[FUND_A].call_count, routes[FUND_B].call_count, routes[ETF].call_count) == (1, 1, 0)
    assert list(rows[0])[-3:] == ["peso", "ter", "consultado_en"]
