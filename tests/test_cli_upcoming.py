"""`upcoming` command: what it fetches, how it renders, and the shortfall warning."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common, cli_upcoming
from myinvestor.cli import app
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CASH_UUID, CPERF, cash_account, ok, order, securities_account

runner = CliRunner()
NOW = datetime(2026, 11, 3, 9, 0).astimezone()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_upcoming, "now_local", lambda: NOW)
    return memory


def mock_everything(mock_api: respx.MockRouter, *, contribution_amount: float = 60.0) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(
        200, json=ok([cash_account(1, enabledBalance=100.0, withheldBalance=25.0)])
    )
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/holds").respond(
        200,
        json=ok([{"id": "h1", "accountId": CASH_UUID, "type": "INVESTMENT_ORDER", "amount": 25.0, "currency": "EUR"}]),
    )
    mock_api.get(f"{CPERF}/v2/automatic-contributions/self").respond(
        200,
        json=ok(
            [
                {
                    "contributionId": "c1",
                    "amount": contribution_amount,
                    "status": "ACTIVE",
                    "isin": "IE0000000002",
                    "fundName": "Fondo mensual",
                    "fromAccountId": "101",
                    "fromAccountAlias": "Cuenta 1",
                    "contributionTimeFrame": {"recurrence": "ONE_MONTH", "nextExecutionDate": "2026-11-05"},
                }
            ]
        ),
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(
        200, json=ok(securities_account("201", cashAccountId="101"))
    )
    mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(
        200,
        json=ok([order("o1", "2026-11-02", status="PENDING", cash=25.0), order("o0", "2026-10-01")]),
    )
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(
        200, json=ok([{"accountId": "301", "cashAccountId": "101"}])
    )
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders").respond(
        200,
        json=ok(
            [
                order(
                    "p1",
                    "2026-11-03",
                    securitiesAccountId=None,
                    pensionAccountId="301",
                    isin=None,
                    dgsCode="N0001",
                    fundName="Plan de pensiones",
                    status="IN_PROGRESS",
                    cash=10.0,
                    effectiveDate="2027-11-04T00:00:00.000+0000",  # a year later: not the execution day
                )
            ]
        ),
    )


def test_upcoming_json_joins_holds_orders_and_contributions(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_everything(mock_api)

    result = runner.invoke(app, ["upcoming", "-f", "json"], catch_exceptions=False)

    rows: list[dict[str, Any]] = json.loads(result.stdout, parse_float=Decimal)
    assert [
        (r["fecha"], r["tipo"], r["producto"], r["importe"], r["retenido"], r["disponible_tras"]) for r in rows
    ] == [
        ("2026-11-02", "orden", "Fondo indexado", Decimal("-25.00"), True, Decimal("100.00")),
        ("2026-11-03", "orden", "Plan de pensiones", Decimal("-10.00"), False, Decimal("100.00")),
        ("2026-11-05", "aportacion", "Fondo mensual", Decimal("-60.00"), False, Decimal("40.00")),
    ]
    assert rows[0]["tipo_retencion"] == "INVESTMENT_ORDER"
    assert {r["cuenta_cargo"] for r in rows} == {"Cuenta 1"}
    assert '"disponible_tras": 40.00' in result.stdout
    assert result.stderr == ""
    assert {call.request.method for call in mock_api.calls} == {"GET"}


def test_upcoming_warns_on_stderr_when_the_balance_does_not_cover_it(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_everything(mock_api, contribution_amount=130.0)

    result = runner.invoke(app, ["upcoming", "-f", "json"], catch_exceptions=False)

    rows = json.loads(result.stdout, parse_float=Decimal)
    assert rows[-1]["falta"] == Decimal("30.00")
    assert "Cuenta 1" in result.stderr and "30,00 €" in result.stderr and "2026-11-05" in result.stderr
    assert result.exit_code == 0


def test_upcoming_days_limits_the_window(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_everything(mock_api)

    result = runner.invoke(app, ["upcoming", "--days", "1", "-f", "json"], catch_exceptions=False)

    assert "aportacion" not in {r["tipo"] for r in json.loads(result.stdout)}


def test_upcoming_table(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_everything(mock_api)

    result = runner.invoke(app, ["--wide", "upcoming"], catch_exceptions=False)

    assert "Próximos 14 días" in result.stdout
    assert "2026-11-05" in result.stdout and "40,00 €" in result.stdout
    assert "retenido" in result.stdout and " sí " in result.stdout
    assert "tipo_retencion" not in result.stdout
