"""`funds` filters, `fund-filters` and `pension-plans` (synthetic data)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.errors import InputError
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import CPERF, ok

runner = CliRunner()
FILTERS = f"{CPERF}/v2/masters/funds/searcher/filters"
PENSION_FILTERS = f"{CPERF}/v2/masters/pension-plans/searcher/filters"
JAPAN = "\t Japan Equity"  # the API sends some categories with leading blanks
FUND_ROW = {
    "isin": "IE0000000001",
    "name": "Fondo indexado",
    "managingEntity": "Gestora Uno",
    "category": "Japan Equity",
    "ter": 0.12,
    "riskRating": 5,
    "liquidationValue": 10.5,
    "annualizedProfitability": {"ytd": 9.1, "oneYear": 12.3},
}
PLAN_ROW = {
    "dgsCode": "N0001",
    "name": "Plan indexado",
    "managingEntity": "Gestora Uno",
    "morningstarRating": None,
    "currency": "EUR",
    "ter": 0.3,
    "category": "RV Global",
    "annualizedProfitability": {"ytd": 8.5, "oneYear": 11.2, "threeYear": 7.1, "fiveYear": None},
    "cumulativeProfitability": {"threeYear": 22.8},
}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def json_rows(*args: str) -> Any:
    result = invoke(*args, "-f", "json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout, parse_float=Decimal)


def catalog(mock_api: respx.MockRouter, rows: list[dict[str, Any]] | None = None) -> respx.Route:
    return mock_api.get(f"{CPERF}/v2/funds").respond(
        200, json=ok(rows if rows is not None else [FUND_ROW], pagination={"totalElements": 1, "last": True})
    )


# ---- funds -------------------------------------------------------------------------------------


@pytest.mark.usefixtures("store")
def test_funds_without_filters_sends_the_same_request_as_before(mock_api: respx.MockRouter) -> None:
    route = catalog(mock_api)

    [row] = json_rows("funds", "S&P 500")

    assert row["isin"] == "IE0000000001"
    assert mock_api.calls.call_count == 1
    params = route.calls.last.request.url.params
    assert dict(params) == {"page": "0", "limit": "20", "sort": "ter,asc", "mainInputFilter": "S&P 500"}


@pytest.mark.usefixtures("store")
def test_funds_filters_become_query_parameters(mock_api: respx.MockRouter) -> None:
    route = catalog(mock_api)
    mock_api.get(f"{FILTERS}/categories").respond(200, json=ok([JAPAN, "Europe Equity"]))
    mock_api.get(f"{FILTERS}/managers").respond(200, json=ok(["Gestora Uno ", "Gestora Dos"]))

    json_rows(
        "funds",
        "--category",
        "japan equity",
        "--manager",
        "Gestora Uno",
        "--currency",
        "usd",
        "--asset-type",
        "variable_income",
        "--region",
        "united_states",
        "--type",
        "indexados",
        "--min-ter",
        "0.1",
        "--max-ter",
        "0.3",
        "--risk",
        "5-6",
        "--rating",
        "4",
        "--esg",
        "--hedged",
    )

    params = dict(route.calls.last.request.url.params)
    assert params == {
        "page": "0",
        "limit": "20",
        "sort": "ter,asc",
        "category": JAPAN,  # the exact spelling of the API, not what was typed
        "region": "UNITED_STATES",
        "manager": "Gestora Uno ",
        "assetsType": "VARIABLE_INCOME",
        "currency": "USD",
        "mstarRating": "4",
        "currencyHedging": "true",
        "responsibleFund": "true",
        "type": "FONDOS_INDEXADOS",
        "terMinValue": "0.1",
        "terTopValue": "0.3",
        "riskMinValue": "5",
        "riskTopValue": "6",
    }


@pytest.mark.usefixtures("store")
def test_funds_single_risk_level_is_both_bounds(mock_api: respx.MockRouter) -> None:
    route = catalog(mock_api)

    json_rows("funds", "--risk", "4", "--type", "activos")

    params = route.calls.last.request.url.params
    assert (params["riskMinValue"], params["riskTopValue"], params["type"]) == ("4", "4", "FONDOS_INVERSION")


@pytest.mark.usefixtures("store")
def test_funds_unknown_category_points_to_the_listing(mock_api: respx.MockRouter) -> None:
    route = catalog(mock_api)
    mock_api.get(f"{FILTERS}/categories").respond(200, json=ok([JAPAN]))

    result = runner.invoke(app, ["funds", "--category", "Japon Equity"])

    assert isinstance(result.exception, InputError)
    assert "fund-filters categories" in str(result.exception)
    assert not route.called


@pytest.mark.usefixtures("store")
def test_funds_bad_risk_fails_before_any_request(mock_api: respx.MockRouter) -> None:
    result = runner.invoke(app, ["funds", "--risk", "9"])

    assert isinstance(result.exception, InputError)
    assert mock_api.calls.call_count == 0


# ---- fund-filters ------------------------------------------------------------------------------


@pytest.mark.usefixtures("store")
def test_fund_filters_lists_clean_values(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{FILTERS}/categories").respond(200, json=ok([JAPAN, None, "Europe Equity"]))

    rows = json_rows("fund-filters", "categories")

    assert rows == [
        {"filtro": "categories", "opcion": "--category", "nombre": "Japan Equity"},
        {"filtro": "categories", "opcion": "--category", "nombre": "Europe Equity"},
    ]


@pytest.mark.usefixtures("store")
def test_fund_filters_without_kind_lists_every_filter(mock_api: respx.MockRouter) -> None:
    paths = {
        "categories": [JAPAN],
        "managers": ["Gestora Uno"],
        "currencies": ["EUR", "USD"],
        "assets-type": ["VARIABLE_INCOME"],
        "regions": ["GLOBAL", None],
        "types": ["FONDOS_INDEXADOS", "FONDOS_INVERSION"],
    }
    for path, values in paths.items():
        mock_api.get(f"{FILTERS}/{path}").respond(200, json=ok(values))

    rows = json_rows("fund-filters")

    assert [(r["opcion"], r["nombre"]) for r in rows] == [
        ("--category", "Japan Equity"),
        ("--manager", "Gestora Uno"),
        ("--currency", "EUR"),
        ("--currency", "USD"),
        ("--asset-type", "VARIABLE_INCOME"),
        ("--region", "GLOBAL"),
        ("--type", "FONDOS_INDEXADOS"),
        ("--type", "FONDOS_INVERSION"),
    ]


@pytest.mark.usefixtures("store")
def test_fund_filters_of_pension_plans(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{PENSION_FILTERS}/managers").respond(200, json=ok(["Gestora Pensiones"]))
    mock_api.get(f"{PENSION_FILTERS}/assets-type").respond(200, json=ok(["FIXED_INCOME"]))

    assert [r["nombre"] for r in json_rows("fund-filters", "managers", "--pension")] == ["Gestora Pensiones"]
    assert [r["filtro"] for r in json_rows("fund-filters", "--pension")] == ["managers", "asset-types"]


@pytest.mark.usefixtures("store")
def test_fund_filters_of_pension_plans_only_has_manager_and_asset_type(mock_api: respx.MockRouter) -> None:
    result = runner.invoke(app, ["fund-filters", "categories", "--pension"])

    assert isinstance(result.exception, InputError)
    assert mock_api.calls.call_count == 0


@pytest.mark.usefixtures("store")
def test_fund_filters_table_shows_the_values(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{FILTERS}/currencies").respond(200, json=ok(["EUR", "USD"]))

    result = invoke("fund-filters", "currencies")

    assert result.exit_code == 0
    assert "EUR" in result.stdout and "--currency" in result.stdout


# ---- pension-plans -----------------------------------------------------------------------------


def plans(mock_api: respx.MockRouter, rows: list[dict[str, Any]] | None = None) -> respx.Route:
    return mock_api.get(f"{CPERF}/v2/pension-plans").respond(
        200, json=ok(rows if rows is not None else [PLAN_ROW], pagination={"totalElements": 1, "currentPage": 1})
    )


@pytest.mark.usefixtures("store")
def test_pension_plans_lists_the_catalog(mock_api: respx.MockRouter) -> None:
    route = plans(mock_api, [PLAN_ROW, {"dgsCode": None, "name": None, "annualizedProfitability": None}])

    first, empty = json_rows("pension-plans")

    assert first == {
        "dgs": "N0001",
        "nombre": "Plan indexado",
        "gestora": "Gestora Uno",
        "categoria": "RV Global",
        "ter": Decimal("0.3"),
        "ytd": Decimal("8.5"),
        "1a": Decimal("11.2"),
        "3a": Decimal("7.1"),
        "5a": None,
    }
    assert set(empty.values()) == {None}
    assert dict(route.calls.last.request.url.params) == {"page": "1", "limit": "20", "sort": "ter,asc"}


@pytest.mark.usefixtures("store")
def test_pension_plans_filters_become_query_parameters(mock_api: respx.MockRouter) -> None:
    route = plans(mock_api)
    mock_api.get(f"{PENSION_FILTERS}/managers").respond(200, json=ok(["\tGestora Pensiones"]))

    json_rows(
        "pension-plans",
        "indexado",
        "--manager",
        "gestora pensiones",
        "--asset-type",
        "fixed-income",
        "--max-ter",
        "0.5",
        "--risk",
        "2-3",
        "--rating",
        "3",
        "--sort",
        "name,asc",
        "-n",
        "5",
        "--page",
        "2",
    )

    assert dict(route.calls.last.request.url.params) == {
        "page": "2",
        "limit": "5",
        "sort": "name,asc",
        "mainInputFilter": "indexado",
        "manager": "\tGestora Pensiones",
        "assetsType": "FIXED_INCOME",
        "mstarRating": "3",
        "terTopValue": "0.5",
        "riskMinValue": "2",
        "riskTopValue": "3",
    }


@pytest.mark.usefixtures("store")
def test_pension_plans_page_starts_at_one(mock_api: respx.MockRouter) -> None:
    result = runner.invoke(app, ["pension-plans", "--page", "0"])

    assert result.exit_code == 2
    assert mock_api.calls.call_count == 0


@pytest.mark.usefixtures("store")
def test_pension_plans_table_has_the_total(mock_api: respx.MockRouter) -> None:
    plans(mock_api)

    result = invoke("pension-plans")

    assert result.exit_code == 0
    assert "1 en total" in result.stdout and "N0001" in result.stdout
