"""`funds` filters, `fund-filters` and `pension-plans` (synthetic data)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.catalog import CatalogFilters
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
        "terMinValue": "0.1",
        "terTopValue": "0.3",
        "riskMinValue": "5",
        "riskTopValue": "6",
    }


@pytest.mark.usefixtures("store")
def test_funds_single_risk_level_is_both_bounds(mock_api: respx.MockRouter) -> None:
    route = catalog(mock_api)

    json_rows("funds", "--risk", "4")

    params = route.calls.last.request.url.params
    assert (params["riskMinValue"], params["riskTopValue"]) == ("4", "4")


@pytest.mark.usefixtures("store")
def test_funds_has_no_type_filter(mock_api: respx.MockRouter) -> None:
    """`/v2/funds` answers 400 to `type=` with any value (FONDOS_INDEXADOS included): never sent."""
    result = runner.invoke(app, ["funds", "--type", "indexados"])

    assert result.exit_code == 2
    assert mock_api.calls.call_count == 0
    assert "type" not in CatalogFilters().params()


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
    ]


@pytest.mark.usefixtures("store")
def test_fund_filters_of_pension_plans(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{PENSION_FILTERS}/managers").respond(200, json=ok(["Gestora Pensiones"]))
    mock_api.get(f"{PENSION_FILTERS}/assets-type").respond(200, json=ok(["FIXED_INCOME"]))

    assert [r["nombre"] for r in json_rows("fund-filters", "managers", "--pension")] == ["Gestora Pensiones"]
    assert [r["filtro"] for r in json_rows("fund-filters", "--pension")] == ["managers", "asset-types"]


@pytest.mark.usefixtures("store")
def test_fund_filters_has_no_types(mock_api: respx.MockRouter) -> None:
    """`filters/types` exists, but `type` is not a filter that `/v2/funds` accepts."""
    assert runner.invoke(app, ["fund-filters", "types"]).exit_code == 2
    assert mock_api.calls.call_count == 0


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
        "com_gestion": None,
        "com_deposito": None,
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


def numbered_plans(count: int, offset: int = 0) -> list[dict[str, Any]]:
    return [{**PLAN_ROW, "dgsCode": f"N{offset + i:04d}"} for i in range(count)]


def paged_plans(mock_api: respx.MockRouter, total: int) -> respx.Route:
    """A catalog of `total` plans that, like the API, never returns more than 50 rows a page."""

    def page(request: httpx.Request) -> httpx.Response:
        number, limit = int(request.url.params["page"]), int(request.url.params["limit"])
        assert limit <= 50
        start = (number - 1) * limit
        rows = numbered_plans(max(min(limit, total - start), 0), start)
        return httpx.Response(200, json=ok(rows, pagination={"totalElements": total, "last": start + limit >= total}))

    return mock_api.get(f"{CPERF}/v2/pension-plans").mock(side_effect=page)


@pytest.mark.usefixtures("store")
def test_pension_plans_more_than_50_are_several_requests_of_50(mock_api: respx.MockRouter) -> None:
    route = paged_plans(mock_api, 200)

    rows = json_rows("pension-plans", "-n", "120")

    assert [r["dgs"] for r in rows] == [f"N{i:04d}" for i in range(120)]
    assert [(c.request.url.params["page"], c.request.url.params["limit"]) for c in route.calls] == [
        ("1", "50"),
        ("2", "50"),
        ("3", "50"),
    ]


@pytest.mark.usefixtures("store")
def test_pension_plans_page_of_more_than_50_starts_where_it_should(mock_api: respx.MockRouter) -> None:
    route = paged_plans(mock_api, 55)

    rows = json_rows("pension-plans", "-n", "40", "--page", "2")
    big = json_rows("pension-plans", "-n", "100")

    assert [r["dgs"] for r in rows] == [f"N{i:04d}" for i in range(40, 55)]
    assert [r["dgs"] for r in big] == [f"N{i:04d}" for i in range(55)]
    assert route.call_count == 3  # -n 40 is one request; -n 100 stops at the last page


@pytest.mark.usefixtures("store")
def test_funds_more_than_50_pages_from_zero(mock_api: respx.MockRouter) -> None:
    route = mock_api.get(f"{CPERF}/v2/funds").respond(
        200, json=ok([FUND_ROW] * 50, pagination={"totalElements": 500, "last": False})
    )

    rows = json_rows("funds", "-n", "60", "--page", "1")

    assert len(rows) == 60
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]  # rows 60-119


FEATURED_SHEET = {
    "dgsCode": "N5396",
    "name": "MyInvestor Indexado Global, PP",
    "managementEntity": "Gestora MyInvestor",
    "category": "RV Global PP",
    "commissionList": [
        {"name": "Comisión de gestión", "percentage": 0.25},
        {"name": "Comisión de Depósito", "percentage": 0.05},
    ],
    "annualizedProfitability": {"ytd": 9.0, "oneYear": 12.0, "threeYear": None, "fiveYear": None},
}


def sheets(mock_api: respx.MockRouter) -> respx.Route:
    def sheet(request: httpx.Request) -> httpx.Response:
        code = request.url.path.rsplit("/", 1)[-1]
        name = FEATURED_SHEET["name"] if code == "N5396" else f"Plan {code}"
        return httpx.Response(200, json=ok({**FEATURED_SHEET, "dgsCode": code, "name": name}))

    return mock_api.get(url__regex=rf"{CPERF}/v2/pension-plans/N\d+$").mock(side_effect=sheet)


@pytest.mark.usefixtures("store")
def test_pension_plans_featured_reads_each_sheet_with_its_fees(mock_api: respx.MockRouter) -> None:
    route = sheets(mock_api)

    [row] = json_rows("pension-plans", "--featured", "indexado GLOBAL")

    assert row == {
        "dgs": "N5396",
        "nombre": "MyInvestor Indexado Global, PP",
        "gestora": "Gestora MyInvestor",
        "categoria": "RV Global PP",
        "ter": None,
        "com_gestion": Decimal("0.25"),
        "com_deposito": Decimal("0.05"),
        "ytd": Decimal("9.0"),
        "1a": Decimal("12.0"),
        "3a": None,
        "5a": None,
    }
    requested = [c.request.url.path.rsplit("/", 1)[-1] for c in route.calls]
    assert {"N5394", "N5396"} <= set(requested) and len(requested) == len(set(requested)) == 7


@pytest.mark.usefixtures("store")
def test_pension_plans_mine_reads_the_plans_held(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([{"accountId": "301"}]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/301").respond(
        200, json=ok([{"dgsCode": "n5396"}, {"dgsCode": "N5394"}, {"dgsCode": "N5396"}])
    )
    route = sheets(mock_api)

    rows = json_rows("pension-plans", "--mine")

    assert [r["dgs"] for r in rows] == ["N5396", "N5394"]
    assert route.call_count == 2


@pytest.mark.usefixtures("store")
def test_pension_plans_featured_takes_no_searcher_filters(mock_api: respx.MockRouter) -> None:
    with pytest.raises(InputError, match="--featured y --mine"):
        invoke("pension-plans", "--featured", "--max-ter", "0.5")

    assert mock_api.calls.call_count == 0
