"""Robo-advisor portfolios: `portfolios` (catalog, detail, `--mine`) and how `position`/`holdings` show
a contracted one (a synthetic `SECURITIES_PORTFOLIO` account, with and without positions)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.errors import InputError
from myinvestor.models import Investment
from myinvestor.portfolios import Portfolio, annual_returns, catalog_day, drift
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import CPERF, cash_account, investment, ok, securities_account

runner = CliRunner()
FUND_RV, FUND_RF, OTHER = "IE0000000011", "LU0000000012", "IE0000000013"
SUMMARY = f"{CPERF}/v3/portfolios/202/returns/summary"
POSITION_COLUMNS = ("producto", "id", "nombre", "valor", "invertido", "ganancia", "rentab", "fecha_valor")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def json_out(*args: str) -> Any:
    result = invoke(*args, "-f", "json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout, parse_float=Decimal)


def catalog_entry(pid: int, name: str, profile: str, funds: list[dict[str, Any]]) -> dict[str, Any]:
    """A catalog portfolio with every key the API sends (synthetic values)."""
    return {
        "id": pid,
        "name": name,
        "iban": None,
        "profile": profile,
        "description": f"Cartera {name.lower()} de ejemplo",
        "minimumInvestment": None,
        "fixedIncome": 30,
        "variableIncome": 70,
        "profitability": 8.25,
        "profitabilityFrom": "31/10/2023",
        "profitabilityTo": "31/10/2026",
        "reported": None,
        "status": None,
        "codBenchmark": "BENCH_EJEMPLO",
        "type": "SAVING",
        "recommended": None,
        "image": "https://example.invalid/cartera.png",
        "recommendedTimeHorizon": 10,
        "funds": funds,
    }


def target_fund(isin: str, percentage: int, category: str) -> dict[str, Any]:
    return {
        "isin": isin,
        "name": f"Fondo {isin}",
        "currency": "EUR",
        "marketValue": None,
        "profitability": 5.1,
        "investmentProfitability": None,
        "profitabilityYTD": 2.3,
        "profit": None,
        "percentage": percentage,
        "category": category,
        "myInvestorCategory": f"MI {category}",
        "investmentGains": None,
        "gains": None,
        "profitabilityGains": None,
    }


CATALOG = [
    catalog_entry(3, "PLATA", "MODERATE", [target_fund(FUND_RF, 100, "RF Euro")]),
    catalog_entry(
        7, "METAL", "VERY_AGGRESSIVE", [target_fund(FUND_RV, 70, "RV Global"), target_fund(FUND_RF, 30, "RF Euro")]
    ),
]


def metal(*held: dict[str, Any], market_value: float | None = None) -> dict[str, Any]:
    """The contracted «METAL» portfolio (account 202) with its own cash account 103."""
    value = sum(h["marketValue"] for h in held) if market_value is None else market_value
    return securities_account(
        "202",
        accountType="SECURITIES_PORTFOLIO",
        portfolioName="METAL",
        portfolioProfileType="VERY_AGGRESSIVE",
        portfolioImage="https://example.invalid/metal.png",
        rentId=None,
        investmentAmountReached=bool(held),
        shouldDoContribution=not held,
        cashAccountId="103",
        marketValue=value,
        totalInvested=value,
        profit=0.0,
        totalReturn=0.0,
        securitiesAccountInvestments={"INDEXED_FUND": {"investmentList": list(held)}} if held else {},
    )


def held(isin: str, value: float) -> dict[str, Any]:
    return investment(isin, accountId="202", marketValue=value, investmentName=f"Fondo {isin}")


def mock_api_with(
    mock_api: respx.MockRouter, portfolio: dict[str, Any], *, cash: float = 25.0, years: Any = None
) -> dict[str, respx.Route]:
    """Account 201 (plain, with cash 101), portfolio 202 (with cash 103, «Cartera Metal») and no pensions."""
    accounts = [cash_account(1), cash_account(3, alias="Cartera Metal", enabledBalance=cash, withheldBalance=0)]
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok(accounts))
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(
        200, json=ok([{"accountId": "201"}, {"accountId": "202"}])
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(securities_account("201")))
    mock_api.get(f"{CPERF}/v2/securities-accounts/202").respond(200, json=ok(portfolio))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([]))
    summary = {"indexedAccountId": 202, "portfolioName": "METAL", "years": [] if years is None else years}
    return {
        "catalog": mock_api.get(f"{CPERF}/v2/portfolios").respond(200, json=ok(CATALOG)),
        "summary": mock_api.get(SUMMARY).respond(200, json=ok(summary, pagination={"totalElements": 0})),
    }


# ---- catalog ----------------------------------------------------------------------------------------


def test_catalog_lists_profile_mix_return_and_horizon(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api_with(mock_api, metal())

    rows = json_out("portfolios")

    assert rows[1] == {
        "id": 7,
        "nombre": "METAL",
        "perfil": "VERY_AGGRESSIVE",
        "renta_fija_pct": 30,
        "renta_variable_pct": 70,
        "rentab": Decimal("8.25"),
        "rentab_desde": "2023-10-31",
        "rentab_hasta": "2026-10-31",
        "horizonte_anos": 10,
        "fondos": 2,
    }


def test_catalog_table_shows_the_profile_in_spanish(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_api_with(mock_api, metal())
    monkeypatch.setenv("COLUMNS", "200")
    out = invoke("portfolios").stdout
    assert "muy agresiva" in out and "moderada" in out and "VERY_AGGRESSIVE" not in out
    assert "8,25 %" in out


@pytest.mark.parametrize("ref", ["metal", "METAL", "7"])
def test_detail_by_name_ignoring_case_or_by_id(ref: str, mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api_with(mock_api, metal())

    doc = json_out("portfolios", ref)

    assert doc["cartera"]["nombre"] == "METAL" and doc["cartera"]["benchmark"] == "BENCH_EJEMPLO"
    assert [(f["isin"], f["categoria"], f["peso"]) for f in doc["fondos"]] == [
        (FUND_RV, "RV Global", 70),
        (FUND_RF, "RF Euro", 30),
    ]


def test_detail_csv_is_one_row_per_fund(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api_with(mock_api, metal())
    lines = invoke("portfolios", "metal", "-f", "csv").stdout.splitlines()
    assert lines[0] == "cartera,isin,nombre,categoria,categoria_myinvestor,moneda,peso"
    assert lines[1] == f"METAL,{FUND_RV},Fondo {FUND_RV},RV Global,MI RV Global,EUR,70"


def test_unknown_portfolio_lists_the_catalog(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api_with(mock_api, metal())
    result = runner.invoke(app, ["portfolios", "oro"])
    assert isinstance(result.exception, InputError)
    assert "PLATA (3), METAL (7)" in str(result.exception)


# ---- position and holdings ----------------------------------------------------------------------------


def test_position_groups_the_portfolio_cash_inside_the_portfolio(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_api_with(mock_api, metal(held(FUND_RV, 700.0), held(FUND_RF, 300.0)), cash=25.0)

    rows = json_out("position")

    assert [(r["producto"], r["id"], r["nombre"]) for r in rows] == [
        ("efectivo", "101", "Cuenta 1"),  # 103 («Cartera Metal») is not a loose account any more
        ("valores", "201", "S201"),
        ("valores", "202", "Cartera METAL (muy agresiva)"),
        ("total", "", ""),
    ]
    portfolio = rows[2]
    assert (portfolio["valor"], portfolio["efectivo"], portfolio["efectivo_id"]) == (
        Decimal("1025.00"),
        Decimal("25.00"),
        "103",
    )
    assert (portfolio["cartera"], portfolio["perfil"]) == ("METAL", "VERY_AGGRESSIVE")
    assert rows[-1]["valor"] == Decimal("1000.30") + Decimal("1100.55") + Decimal("1025.00")
    plain = rows[1]
    assert {k: plain[k] for k in ("cartera", "perfil", "efectivo", "efectivo_id")} == dict.fromkeys(
        ("cartera", "perfil", "efectivo", "efectivo_id")
    )


def test_position_with_an_empty_portfolio(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api_with(mock_api, metal(market_value=0.0), cash=0.0)

    portfolio = json_out("position")[2]

    assert (portfolio["nombre"], portfolio["valor"], portfolio["efectivo"]) == (
        "Cartera METAL (muy agresiva)",
        Decimal("0.00"),
        Decimal("0.00"),
    )
    assert portfolio["fecha_valor"] == date.today().isoformat()  # only live cash, no NAV yet


def test_position_table_shows_cash_column_only_with_a_portfolio(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    mock_api_with(mock_api, metal(held(FUND_RV, 100.0)))
    out = invoke("position").stdout
    assert "Cartera METAL (muy agresiva)" in out and "efectivo_id" not in out and "perfil" not in out
    assert "25,00 €" in out


def test_position_table_of_plain_accounts_has_no_new_columns(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(securities_account("201")))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([]))
    header = next(line for line in invoke("position").stdout.splitlines() if "producto" in line)
    assert [c for c in header.split() if c.replace("_", "").isalpha()] == list(POSITION_COLUMNS)


def test_holdings_name_the_portfolio(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_api_with(mock_api, metal(held(FUND_RV, 700.0)))

    rows = json_out("holdings")

    assert [(r["cuenta"], r["cartera"], r["perfil"]) for r in rows] == [
        ("201", None, None),
        ("202", "METAL", "VERY_AGGRESSIVE"),
    ]
    monkeypatch.setenv("COLUMNS", "250")
    assert "METAL (muy agresiva)" in invoke("holdings").stdout


def test_holdings_table_of_plain_accounts_has_no_portfolio_column(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(securities_account("201")))
    out = invoke("--wide", "holdings").stdout
    assert "cartera" not in out and "perfil" not in out


# ---- --mine ----------------------------------------------------------------------------------------


def test_mine_compares_positions_with_the_target_weights(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    years = {"2025": {"amount": 40.5, "percentage": 4.05}, "2024": {"amount": -10.0, "percentage": -1.2}}
    routes = mock_api_with(mock_api, metal(held(FUND_RV, 600.0), held(OTHER, 100.0), held(FUND_RF, 300.0)), years=years)

    [row] = json_out("portfolios", "--mine")

    assert (row["cuenta"], row["cartera"], row["perfil"]) == ("202", "METAL", "VERY_AGGRESSIVE")
    assert (row["valor"], row["efectivo"]) == (Decimal("1000.00"), Decimal("25.00"))
    assert [(p["isin"], p["objetivo_pct"], p["real_pct"], p["desviacion_pct"]) for p in row["posiciones"]] == [
        (FUND_RV, 70, Decimal("60.00"), Decimal("-10.00")),
        (FUND_RF, 30, Decimal("30.00"), Decimal("0.00")),
        (OTHER, 0, Decimal("10.00"), Decimal("10.00")),  # held but not in the target
    ]
    assert row["rentabilidad_anual"] == [
        {"periodo": "2024", "importe": Decimal("-10.00"), "rentab": Decimal("-1.2")},
        {"periodo": "2025", "importe": Decimal("40.50"), "rentab": Decimal("4.05")},
    ]
    assert dict(routes["summary"].calls.last.request.url.params) == {"page": "1", "limit": "3"}


def test_mine_with_an_empty_portfolio_has_targets_but_no_actual_weights(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_api_with(mock_api, metal(market_value=0.0), cash=0.0)

    [row] = json_out("portfolios", "--mine")

    assert [
        (p["isin"], p["objetivo_pct"], p["real_pct"], p["desviacion_pct"], p["valor"]) for p in row["posiciones"]
    ] == [
        (FUND_RV, 70, None, None, Decimal("0.00")),
        (FUND_RF, 30, None, None, Decimal("0.00")),
    ]
    assert row["rentabilidad_anual"] == []
    monkeypatch.setenv("COLUMNS", "250")
    result = invoke("portfolios", "--mine")
    assert (
        "sin posiciones todavía" in " ".join(result.stdout.split())
        and "Sin rentabilidad anual todavía" in result.output
    )


def test_mine_csv_is_one_row_per_fund(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api_with(mock_api, metal(held(FUND_RV, 700.0), held(FUND_RF, 300.0)))
    lines = invoke("portfolios", "--mine", "-f", "csv").stdout.splitlines()
    assert lines[0] == "cuenta,cartera,isin,nombre,objetivo_pct,real_pct,desviacion_pct,valor"
    assert lines[1] == f"202,METAL,{FUND_RV},Fondo {FUND_RV},70,70.00,0.00,700.00"


def test_mine_outside_the_catalog_has_no_targets(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api_with(mock_api, {**metal(held(FUND_RV, 100.0)), "portfolioName": "ORO"})
    result = invoke("portfolios", "--mine", "-f", "json")
    [row] = json.loads(result.stdout, parse_float=Decimal)
    assert [(p["isin"], p["objetivo_pct"], p["real_pct"]) for p in row["posiciones"]] == [
        (FUND_RV, None, Decimal("100.00"))
    ]
    assert "no está en el catálogo" in result.stderr


def test_mine_filters_by_name_and_without_portfolios_is_empty(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_api_with(mock_api, metal(held(FUND_RV, 100.0)))
    assert [r["cuenta"] for r in json_out("portfolios", "metal", "--mine")] == ["202"]
    assert json_out("portfolios", "plata", "--mine") == []


# ---- the tolerant parts ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"years": {"2025": {"amount": 1.5, "percentage": 0.5}}},  # what the web reads
        {"years": [{"year": 2025, "amount": 1.5, "percentage": 0.5}]},  # its read-only variant
    ],
)
def test_annual_returns_accept_both_shapes(data: dict[str, Any]) -> None:
    [year] = annual_returns(data)
    assert (year.year, year.amount, year.percentage) == ("2025", Decimal("1.5"), Decimal("0.5"))


@pytest.mark.parametrize(
    "data", [None, {}, {"years": None}, {"years": []}, {"years": "x"}, {"years": [{"amount": 1}, 3]}]
)
def test_annual_returns_ignore_what_they_do_not_understand(data: Any) -> None:
    assert annual_returns(data) == []


def test_annual_returns_keep_years_with_odd_values() -> None:
    [year] = annual_returns({"years": {"2025": {"amount": "n/a", "percentage": None}}})
    assert (year.year, year.amount, year.percentage) == ("2025", None, None)


def test_drift_matches_by_isin_and_sums_repeated_positions() -> None:
    target = Portfolio.model_validate(CATALOG[1])
    holdings = [Investment(isin=FUND_RV, market_value=Decimal(50)), Investment(isin=FUND_RV, market_value=Decimal(50))]
    [rv, rf] = drift(target, holdings)
    assert (rv.isin, rv.value, rv.actual, rv.deviation) == (FUND_RV, Decimal(100), Decimal("100.00"), Decimal("30.00"))
    assert (rf.isin, rf.value, rf.actual, rf.deviation) == (FUND_RF, Decimal(0), Decimal("0.00"), Decimal("-30.00"))


def test_catalog_tolerates_missing_funds_and_dates() -> None:
    p = Portfolio.model_validate({"id": 1, "name": "X", "funds": None, "profitabilityFrom": ""})
    assert (p.funds, p.profitability_from) == ([], None)


@pytest.mark.parametrize(
    ("raw", "day"),
    [
        ("31/10/2023", date(2023, 10, 31)),  # what the catalog sends
        ("1/2/2024", date(2024, 2, 1)),
        ("2023-10-31", date(2023, 10, 31)),
        ("2023-10-31T00:00:00.000+0000", date(2023, 10, 31)),
        ("31/02/2023", None),  # not a day: null, not an error
        ("ayer", None),
        ("", None),
        (None, None),
        (20231031, None),
    ],
)
def test_catalog_dates_are_spanish_or_iso_and_never_fail(raw: Any, day: date | None) -> None:
    assert catalog_day(raw) == day
    p = Portfolio.model_validate({"id": 1, "name": "X", "profitabilityFrom": raw, "profitabilityTo": raw})
    assert (p.profitability_from, p.profitability_to) == (day, day)


def test_catalog_with_an_unreadable_date_still_lists(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/portfolios").respond(200, json=ok([{**CATALOG[1], "profitabilityTo": "31/13/2026"}]))
    [row] = json_out("portfolios")
    assert (row["rentab_desde"], row["rentab_hasta"]) == ("2023-10-31", None)
