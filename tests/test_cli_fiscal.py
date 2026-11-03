"""#73: `coste_fiscal`, `ganancia_real` and `rentab_real` after transfers (fund → fund, plan → plan)."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import CPERF, cash_account, investment, ok, securities_account
from tests.test_cli_gains import added_values, fund_lots, lot

runner = CliRunner()
FUND_A, FUND_B, STOCK = "IE0000000001", "IE0000000002", "US0000000001"
PLAN_A, PLAN_B = "N0001", "N0002"
PLAN_HISTORY = f"{CPERF}/v2/pension-accounts/301/positions"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def json_out(*args: str) -> Any:
    result = runner.invoke(app, [*args, "-f", "json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout, parse_float=Decimal)


def total(rows: list[dict[str, Any]], key: str) -> Decimal:
    return sum((r[key] for r in rows), Decimal(0))


# ---- fund → fund --------------------------------------------------------------------------------


def mock_funds(mock_api: respx.MockRouter, *, transferred: bool) -> None:
    """A fund bought for 920 and worth 1000, and a stock; after the transfer the fund is another one.

    MyInvestor shows the destination with `invertido` = the value transferred (1000); its lots keep 920.
    """
    if transferred:
        fund = investment(FUND_B, initialInvestment=1000.0, marketValue=1000.0, profit=0.0, totalReturn=0.0)
        lots = fund_lots(lot("N2", "2021-05-10", operationDesc="SUSC.TRASPASO. INT.", investment=920.0))
    else:
        fund = investment(FUND_A, initialInvestment=920.0, marketValue=1000.0, profit=80.0, totalReturn=8.7)
        lots = fund_lots(lot("N1", "2021-05-10", investment=920.0))
    stock = investment(STOCK, "BROKER", initialInvestment=50.0, marketValue=60.0, profit=10.0)
    groups = {"INDEXED_FUND": {"investmentList": [fund]}, "BROKER": {"investmentList": [stock]}}
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(
        200,
        json=ok(securities_account(securitiesAccountInvestments=groups, marketValue=1060.0, totalInvested=1050.0)),
    )
    mock_api.get(added_values(FUND_B if transferred else FUND_A)).respond(200, json=ok(lots))


def test_holdings_without_fiscal_asks_for_no_lots(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_funds(mock_api, transferred=False)
    lots = mock_api.get(added_values(FUND_A))

    rows = json_out("holdings")

    assert not lots.called
    assert not {"coste_fiscal", "ganancia_real", "rentab_real"} & set(rows[0])


def test_a_fund_transfer_keeps_the_total_tax_cost_and_the_real_gain(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_funds(mock_api, transferred=False)
    before = json_out("holdings", "--fiscal")
    mock_funds(mock_api, transferred=True)
    after = json_out("holdings", "--fiscal")

    assert (total(before, "invertido"), total(after, "invertido")) == (970, 1050)  # MyInvestor's: jumps
    assert total(before, "coste_fiscal") == total(after, "coste_fiscal") == 970
    assert total(before, "ganancia_real") == total(after, "ganancia_real") == 90
    fund = next(r for r in after if r["isin"] == FUND_B)
    assert (fund["coste_fiscal"], fund["ganancia_real"], fund["rentab_real"]) == (
        Decimal("920.00"),
        Decimal("80.00"),
        Decimal("8.70"),
    )
    stock = next(r for r in after if r["isin"] == STOCK)
    assert stock["coste_fiscal"] == stock["invertido"]  # stocks and ETFs are never transferred


def test_a_fund_without_lots_has_no_tax_cost(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_funds(mock_api, transferred=False)
    mock_api.get(added_values(FUND_A)).respond(200, json=ok(fund_lots()))

    fund = next(r for r in json_out("holdings", "--fiscal") if r["isin"] == FUND_A)

    assert (fund["coste_fiscal"], fund["ganancia_real"], fund["rentab_real"]) == (None, None, None)


# ---- plan → plan --------------------------------------------------------------------------------


def history_point(day: str, shares: float, value: float, invested: float) -> dict[str, Any]:
    return {"date": day, "totalShares": shares, "marketValue": value, "investmentAddedValue": invested}


# A (cost 10/unit) moves half its units to B on 2026-03-03; B is born with `invertido` = the money received.
HISTORY = {
    PLAN_A: [
        history_point("2026-03-01", 100, 1300, 1000),
        history_point("2026-03-02", 100, 1320, 1000),
        history_point("2026-03-03", 50, 660, 500),
        history_point("2026-03-04", 50, 665, 500),
    ],
    PLAN_B: [history_point("2026-03-03", 66, 660, 660), history_point("2026-03-04", 76, 765, 760)],
}


def plan(code: str, shares: float, value: float, invested: float) -> dict[str, Any]:
    return {
        "dgsCode": code,
        "name": f"Plan {code}",
        "shares": shares,
        "totalInvested": invested,
        "marketValue": value,
        "profit": value - invested,
    }


def mock_plans(mock_api: respx.MockRouter, until: str) -> None:
    """The pension account as it was on `until`, with its history up to that day."""
    series = {code: [p for p in points if p["date"] <= until] for code, points in HISTORY.items()}
    series = {code: points for code, points in series.items() if points}
    plans = [
        plan(code, p["totalShares"], p["marketValue"], p["investmentAddedValue"]) for code, (*_, p) in series.items()
    ]
    account = {"accountId": "301", "externalAccountCode": "P301"}
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([account]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/301").respond(200, json=ok(plans))
    mock_api.get(PLAN_HISTORY, params={"groupType": "ONE_YEAR"}).respond(200, json=ok({"positions": series}))


def test_a_plan_mobilization_keeps_the_total_tax_cost_and_the_real_gain(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_plans(mock_api, until="2026-03-02")
    before = json_out("pensions", "--fiscal")
    mock_plans(mock_api, until="2026-03-03")
    after = json_out("pensions", "--fiscal")

    assert (total(before, "invertido"), total(after, "invertido")) == (1000, 1160)  # MyInvestor's: jumps
    assert total(before, "coste_fiscal") == total(after, "coste_fiscal") == 1000
    assert total(before, "ganancia_real") == total(after, "ganancia_real") == 320
    assert {r["codigo"]: r["coste_fiscal"] for r in after} == {PLAN_A: 500, PLAN_B: 500}
    assert mock_api.calls.last.request.url.params["groupType"] == "ONE_YEAR"


def by_day(rows: list[dict[str, Any]], key: str) -> dict[str, Decimal]:
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        sums[r["fecha"]] += r[key]
    return dict(sums)


def test_history_tax_cost_does_not_jump_on_a_mobilization(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_plans(mock_api, until="2026-03-04")

    rows = json_out("history", "-a", "301", "--period", "year")

    assert by_day(rows, "invertido") == {"2026-03-01": 1000, "2026-03-02": 1000, "2026-03-03": 1160, "2026-03-04": 1260}
    assert by_day(rows, "coste_fiscal") == {
        "2026-03-01": 1000,
        "2026-03-02": 1000,
        "2026-03-03": 1000,
        "2026-03-04": 1100,  # a contribution of 100 to B
    }
    assert by_day(rows, "ganancia_real") == {"2026-03-01": 300, "2026-03-02": 320, "2026-03-03": 320, "2026-03-04": 330}
    assert len(mock_api.calls) == 2  # accounts + one series: `year` is already the whole history


def test_a_shorter_history_starts_from_the_last_year_adjustments(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    """In six months B already exists on the first day: only the yearly series shows that it inherited 500."""
    mock_plans(mock_api, until="2026-03-04")
    recent = {code: [p for p in points if p["date"] >= "2026-03-03"] for code, points in HISTORY.items()}
    mock_api.get(PLAN_HISTORY, params={"groupType": "SIX_MONTHS"}).respond(200, json=ok({"positions": recent}))

    rows = json_out("history", PLAN_B, "-a", "301")

    assert [(r["fecha"], r["invertido"], r["coste_fiscal"]) for r in rows] == [
        ("2026-03-03", Decimal("660.00"), Decimal("500.00")),
        ("2026-03-04", Decimal("760.00"), Decimal("600.00")),
    ]


def test_history_of_a_fund_uses_its_invested_as_tax_cost(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_funds(mock_api, transferred=False)
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([]))
    point = {"date": "2026-03-01", "totalShares": 10, "marketValue": 1000.0, "investmentAddedValue": 920.0}
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/positions").respond(
        200, json=ok({"positions": {FUND_A: [point]}})
    )

    [row] = json_out("history", FUND_A, "-a", "201")

    assert (row["invertido"], row["coste_fiscal"], row["ganancia_real"]) == (
        Decimal("920.00"),
        Decimal("920.00"),
        Decimal("80.00"),
    )


def test_the_table_shows_the_tax_cost_in_euros(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_funds(mock_api, transferred=True)

    result = runner.invoke(app, ["--wide", "holdings", "--fiscal"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "coste_fiscal" in result.stdout
    assert "920,00 €" in result.stdout
    assert "8,70 %" in result.stdout


# ---- position and allocation --------------------------------------------------------------------


def test_position_total_sums_the_products_without_cash(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_funds(mock_api, transferred=True)
    mock_plans(mock_api, until="2026-03-03")
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(
        200, json=ok([{"accountId": "301", "externalAccountCode": "P301", "marketValue": 1320.0}])
    )

    rows = {r["producto"]: r for r in json_out("position", "--fiscal")}

    assert (rows["efectivo"]["coste_fiscal"], rows["efectivo"]["ganancia_real"]) == (None, None)
    assert (rows["valores"]["coste_fiscal"], rows["valores"]["ganancia_real"]) == (970, 90)
    assert (rows["pensiones"]["coste_fiscal"], rows["pensiones"]["ganancia_real"]) == (1000, 320)
    assert (rows["total"]["coste_fiscal"], rows["total"]["ganancia_real"], rows["total"]["rentab_real"]) == (
        Decimal("1970.00"),
        Decimal("410.00"),
        Decimal("20.81"),
    )


def test_position_without_fiscal_keeps_its_keys(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_funds(mock_api, transferred=True)
    mock_plans(mock_api, until="2026-03-03")

    rows = json_out("position")

    assert not any("coste_fiscal" in r for r in rows)
    assert not any(c.request.url.path.endswith(("/positions", "/added-values")) for c in mock_api.calls)


def test_allocation_fiscal_by_product_with_cash(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_funds(mock_api, transferred=True)
    mock_api.get(f"{CPERF}/v2/funds/{FUND_B}").respond(
        200, json=ok({"isinCode": FUND_B, "assetType": "VARIABLE_INCOME"})
    )
    mock_plans(mock_api, until="2026-03-03")

    rows = {r["clave"]: r for r in json_out("allocation", "--by", "product", "--include-cash", "--fiscal")}

    assert rows[FUND_B]["coste_fiscal"] == 920 and rows[FUND_B]["invertido"] == 1000
    assert rows[PLAN_B]["coste_fiscal"] == 500
    assert rows["efectivo"]["coste_fiscal"] == rows["efectivo"]["valor"]
    assert rows["total"]["coste_fiscal"] == 920 + 50 + 500 + 500 + rows["efectivo"]["valor"]
    assert rows["total"]["ganancia_real"] == 80 + 10 + 160 + 160
