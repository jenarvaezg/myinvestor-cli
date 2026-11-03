from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_auth, cli_common
from myinvestor.auth import REFRESH_PATH
from myinvestor.cli import app
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import (
    API,
    CASH_UUID,
    CPERF,
    DEVICE,
    cash_account,
    flow,
    investment,
    make_jwt,
    ok,
    order,
    securities_account,
    signature_pending,
    tokens,
)

runner = CliRunner()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_auth, "get_store", lambda: memory)
    return memory


@pytest.fixture
def no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    monkeypatch.setattr(cli_auth, "stdin_is_tty", lambda: False)


def invoke(*args: str, stdin: str | None = None) -> Any:
    return runner.invoke(app, list(args), input=stdin, catch_exceptions=False)


def mock_portfolio(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    funds_and_stocks = securities_account(
        securitiesAccountInvestments={
            "INDEXED_FUND": {"investmentList": [investment("IE0000000001")]},
            "BROKER": {"investmentList": [investment("JE0000000002", "BROKER", marketValue=50.0)]},
        }
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(funds_and_stocks))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(
        200, json=ok([{"accountId": "301", "marketValue": 2000.0, "totalInvested": 1500.0, "profit": 500.0}])
    )


def test_position_json(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)

    result = invoke("position", "-f", "json")

    rows = json.loads(result.stdout)
    assert [(r["producto"], r["valor"]) for r in rows] == [
        ("efectivo", "1000.3"),
        ("efectivo", "1000.3"),
        ("valores", "1100.55"),
        ("pensiones", "2000.0"),
    ]


def test_position_table_has_total(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    result = invoke("position")
    assert "TOTAL" in result.stdout and "5.101,15 €" in result.stdout


def test_holdings_filter_broker(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    rows = json.loads(invoke("holdings", "--kind", "broker", "-f", "json").stdout)
    assert [r["isin"] for r in rows] == ["JE0000000002"]


def test_movements_csv_for_one_account(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    route = mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(
        200, json=ok({"flowList": [flow(1, -12.5), flow(2, 100.0, concept="Nómina")]})
    )

    result = invoke("movements", "-a", "Cuenta 1", "--from", "2026-10-01", "--to", "2026-10-31", "-f", "csv")

    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [(r["fecha"], r["importe"]) for r in rows] == [("2026-10-12", "100.0"), ("2026-10-11", "-12.5")]
    assert route.calls.last.request.url.params["dateFrom"] == "2026-10-01"


def test_movements_search_and_iban_suffix(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(
        200, json=ok({"flowList": [flow(1, -12.5), flow(2, 100.0, concept="Nómina")]})
    )
    rows = json.loads(invoke("movements", "-a", "0001", "-s", "nómina", "-f", "json").stdout)
    assert [r["concepto"] for r in rows] == ["Nómina"]


def test_movements_unknown_account(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    result = runner.invoke(app, ["movements", "-a", "nope"])
    assert result.exit_code != 0
    assert "Disponibles" in str(result.exception)


def test_old_movements_without_terminal_explain_approval(
    mock_api: respx.MockRouter, store: MemorySessionStore, no_tty: None
) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(202, json=signature_pending())

    result = runner.invoke(app, ["movements", "--from", "2024-01-01"])

    assert result.exit_code != 0
    assert "segundo factor" in str(result.exception)


def test_orders_all_history(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    route = mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(
        200, json=ok([order("a", "2025-05-01"), order("b", "2026-10-01", status="IN_PROGRESS")])
    )

    rows = json.loads(invoke("orders", "--all", "--status", "executed", "-f", "json").stdout)

    assert [r["ref"] for r in rows] == ["a"]
    assert route.calls.last.request.url.params["dateFrom"] == "2010-01-01"


def test_login_refresh_token_stdin(mock_api: respx.MockRouter, store: MemorySessionStore, no_tty: None) -> None:
    store.session = None
    route = mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens())

    result = invoke("login", "--refresh-token-stdin", "--device-id", "dev-web", stdin="rt-123\n")

    assert "Sesión iniciada" in result.stderr
    assert json.loads(route.calls.last.request.content) == {"refreshToken": "rt-123"}
    assert route.calls.last.request.headers["x-device-id"] == "dev-web"
    assert store.session is not None and store.session.device_id == "dev-web"


def test_login_direct_reuses_device_and_reads_password_stdin(
    mock_api: respx.MockRouter, store: MemorySessionStore, no_tty: None
) -> None:
    route = mock_api.post(f"{API}/login/api/v2/auth/token").respond(200, json=tokens())

    invoke("login", "--direct", "-u", "00000000T", "--password-stdin", stdin="pw\n")

    assert json.loads(route.calls.last.request.content) == {"customerId": "00000000T", "password": "pw"}
    assert route.calls.last.request.headers["x-device-id"] == DEVICE


def test_login_direct_without_terminal_or_stdin_refuses(store: MemorySessionStore, no_tty: None) -> None:
    result = runner.invoke(app, ["login", "--direct", "-u", "00000000T"])
    assert result.exit_code != 0
    assert "stdin" in str(result.exception)


def test_logout_keeps_device(store: MemorySessionStore) -> None:
    invoke("logout")
    assert store.session == Session(device_id=DEVICE, customer_id="00000000T")
    invoke("logout", "--forget-device")
    assert store.session is None


def test_status_refreshes_and_greets(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana"}))
    result = invoke("status")
    assert "Sesión activa" in result.stderr and "Ana" in result.stderr


def test_status_when_expired(store: MemorySessionStore) -> None:
    old = make_jwt(timedelta(minutes=-5))
    store.session = Session(device_id=DEVICE, access_token=old, refresh_token=old)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1 and "caducada" in result.stderr


def test_raw_rejects_bad_param(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["raw", "/x", "-p", "nokey"])
    assert result.exit_code != 0 and "clave=valor" in str(result.exception)


def test_raw_get(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    route = mock_api.get(f"{API}/feed/api/x").respond(200, json=ok([1]))
    result = invoke("raw", "feed/api/x", "-p", "a=1")
    assert json.loads(result.stdout)["payload"]["data"] == [1]
    assert route.calls.last.request.url.params["a"] == "1"


def test_history_for_pension_code(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([{"accountId": "301"}]))
    point = {"date": "2026-10-01", "marketValue": 1.0, "totalShares": 1.0}
    route = mock_api.get(f"{CPERF}/v2/pension-accounts/301/positions").respond(
        200, json=ok({"positions": {"N0001": [point], "N0002": [point]}})
    )

    rows = json.loads(invoke("history", "n0001", "--period", "year", "-f", "json").stdout)

    assert {r["producto"] for r in rows} == {"N0001"}
    assert route.calls.last.request.url.params["groupType"] == "ONE_YEAR"


def test_fund_summary_adds_price(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/funds/IE0000000001").respond(
        200, json=ok({"name": "Global", "isinCode": "IE0000000001", "ter": 0.1, "fundData": {"managingEntity": "Gest"}})
    )
    mock_api.get(f"{CPERF}/v2/funds/IE0000000001/prices/last").respond(
        200, json=ok({"date": "2026-11-02", "price": 10.5, "currency": "EUR", "dayProfitPercentage": 0.2})
    )

    data = json.loads(invoke("fund", "ie0000000001").stdout)

    assert data["managementEntity"] == "Gest"
    assert data["lastPrice"]["price"] == "10.5"


def test_not_logged_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = MemorySessionStore()
    monkeypatch.setattr(cli_common, "get_store", lambda: empty)
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code != 0 and "myinvestor login" in str(result.exception)


def test_contributions_hide_cancelled(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    def contribution(cid: str, state: str) -> dict[str, Any]:
        return {
            "contributionId": cid,
            "amount": 100,
            "status": state,
            "isin": "IE0000000001",
            "contributionTimeFrame": {"recurrence": "ONE_MONTH", "nextExecutionDate": str(date.today() + timedelta(1))},
        }

    mock_api.get(f"{CPERF}/v2/automatic-contributions/self").respond(
        200, json=ok([contribution("c1", "ACTIVE"), contribution("c2", "CANCELED")])
    )
    rows = json.loads(invoke("contributions", "-f", "json").stdout)
    assert [r["id"] for r in rows] == ["c1"]
