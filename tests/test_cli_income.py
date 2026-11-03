"""`income`: movements classified and totalled per month, year, type or account."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.errors import InputError, PartialResultError
from myinvestor.psd2 import free_start
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CASH_UUID, CPERF, cash_account, flow, ok
from tests.test_transfers_match import ACCOUNT_A, ACCOUNT_B, op_ref

runner = CliRunner()
FLOWS = f"{API}/account/api/v3/account/{CASH_UUID}/flows"
SECOND_UUID = "aaaaaaaa-0000-0000-0000-000000000002"
OCTOBER = ("--from", "2026-09-15", "--to", "2026-10-31")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def json_rows(*args: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(invoke("income", *args, "-f", "json").stdout, parse_float=Decimal)
    return rows


def income_flows() -> list[dict[str, Any]]:
    return [
        flow(1, 10.0, operationType="LIQUIDACIÓN INTERESES"),
        flow(2, -1.9, operationType="RETENCION IRPF"),
        flow(3, 5.0, operationType="ABONO DIVIDENDO"),
        flow(4, -2.0, operationType="COMISION CUSTODIA"),
        flow(5, -100.0, operationType="COMPRA RV CONTADO"),
        flow(6, 300.0, operationType="ABONO POR TRANSFERENCIA"),
        flow(7, -7.0, operationType="PAGO CON TARJETA"),
        flow(8, 1.0, operationType="LIQUIDACION INTERESES", operationDate="2026-09-30T00:00:00.000+0000"),
    ]


def mock_one_account(mock_api: respx.MockRouter) -> respx.Route:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    return mock_api.get(FLOWS).respond(200, json=ok({"flowList": income_flows()}))


def test_income_by_month_totals_gross_withholding_net_and_fees(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    route = mock_one_account(mock_api)

    rows = json_rows(*OCTOBER)

    assert dict(route.calls.last.request.url.params) == {"dateFrom": "2026-09-15", "dateTo": "2026-10-31"}
    assert [r["periodo"] for r in rows] == ["2026-09", "2026-10", "total"]
    october = rows[1]
    assert {k: october[k] for k in ("intereses", "dividendos", "bruto", "retenciones", "neto", "comisiones")} == {
        "intereses": Decimal("10.00"),
        "dividendos": Decimal("5.00"),
        "bruto": Decimal("15.00"),
        "retenciones": Decimal("-1.90"),
        "neto": Decimal("13.10"),
        "comisiones": Decimal("-2.00"),
    }
    assert october["movimientos"] == 4
    assert (rows[0]["bruto"], rows[-1]["bruto"], rows[-1]["neto"]) == (
        Decimal("1.00"),
        Decimal("16.00"),
        Decimal("14.10"),
    )


def test_income_reports_unknown_types(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_one_account(mock_api)
    result = invoke("income", *OCTOBER)
    assert "PAGO CON TARJETA" in result.stderr and "--by type" in result.stderr
    assert "13,10 €" in result.stdout


def test_income_months_without_movements_are_listed(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": []}))
    start = date.today().replace(day=1) - timedelta(days=40)
    rows = json_rows("--from", start.isoformat())
    months = sorted({(start + timedelta(days=d)).isoformat()[:7] for d in range((date.today() - start).days + 1)})
    assert [r["periodo"] for r in rows] == [*months, "total"] and len(months) == 3
    assert all(r["neto"] == 0 and r["movimientos"] == 0 for r in rows)


def test_income_by_type_shows_category_type_and_rule(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_one_account(mock_api)

    rows = json_rows(*OCTOBER, "--by", "type")

    summary = [(r["categoria"], r["tipo"], r["movimientos"], r["importe"]) for r in rows]
    assert summary == [
        ("intereses", "LIQUIDACION INTERESES", 1, Decimal("1.00")),
        ("intereses", "LIQUIDACIÓN INTERESES", 1, Decimal("10.00")),
        ("dividendos", "ABONO DIVIDENDO", 1, Decimal("5.00")),
        ("retencion", "RETENCION IRPF", 1, Decimal("-1.90")),
        ("comision", "COMISION CUSTODIA", 1, Decimal("-2.00")),
        ("compra", "COMPRA RV CONTADO", 1, Decimal("-100.00")),
        ("aportacion_externa", "ABONO POR TRANSFERENCIA", 1, Decimal("300.00")),
        ("otros", "PAGO CON TARJETA", 1, Decimal("-7.00")),
        ("total", None, 8, Decimal("205.10")),
    ]
    rules = {r["tipo"]: r["regla"] for r in rows}
    assert rules["COMPRA RV CONTADO"] == "tipo exacto"
    assert rules["RETENCION IRPF"] == "palabra clave: RETENC"
    assert rules["PAGO CON TARJETA"] is None


def test_income_by_type_puts_paired_own_transfers_in_traspaso_interno(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    """Both legs of a transfer between my accounts are internal; a gift from another client stays external."""
    key, stamp = "120261020700001", "2026-10-20T00:00:00.000+0000"
    day = {"operationDate": stamp, "valueDate": stamp}
    out = op_ref(key, "000010", account=ACCOUNT_A)
    into = op_ref(key, "000020", account=ACCOUNT_B, check="7")
    gift = op_ref("120261014477050", "000021", account=ACCOUNT_B, block="3000000099", check="7")
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(FLOWS).respond(
        200,
        json=ok(
            {
                "flowList": [
                    flow(1, -50.0, reference="000010", operationType="CARGO POR TRANSFERENCIA", operationRef=out, **day)
                ]
            }
        ),
    )
    mock_api.get(f"{API}/account/api/v3/account/{SECOND_UUID}/flows").respond(
        200,
        json=ok(
            {
                "flowList": [
                    flow(
                        2, 50.0, reference="000020", operationType="ABONO POR TRANSFERENCIA", operationRef=into, **day
                    ),
                    flow(
                        3, 50.0, reference="000021", operationType="ABONO POR TRANSFERENCIA", operationRef=gift, **day
                    ),
                ]
            }
        ),
    )

    rows = json_rows(*OCTOBER, "--by", "type")

    assert [(r["categoria"], r["tipo"], r["regla"], r["movimientos"], r["importe"]) for r in rows] == [
        ("aportacion_externa", "ABONO POR TRANSFERENCIA", "tipo exacto", 1, Decimal("50.00")),
        ("traspaso_interno", "ABONO POR TRANSFERENCIA", "traspaso emparejado", 1, Decimal("50.00")),
        ("traspaso_interno", "CARGO POR TRANSFERENCIA", "traspaso emparejado", 1, Decimal("-50.00")),
        ("total", None, None, 3, Decimal("50.00")),
    ]


def test_income_by_year(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_one_account(mock_api)
    rows = json_rows(*OCTOBER, "--by", "year")
    assert [(r["periodo"], r["neto"]) for r in rows] == [("2026", Decimal("14.10")), ("total", Decimal("14.10"))]


def test_income_by_account(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": income_flows()}))
    mock_api.get(f"{API}/account/api/v3/account/{SECOND_UUID}/flows").respond(
        200, json=ok({"flowList": [flow(1, 3.0, operationType="LIQUIDACION INTERESES")]})
    )

    rows = json_rows(*OCTOBER, "--by", "account")

    assert [(r["cuenta"], r["cuenta_id"], r["intereses"]) for r in rows] == [
        ("Cuenta 1", "101", Decimal("11.00")),
        ("Cuenta 2", "102", Decimal("3.00")),
        ("total", None, Decimal("14.00")),
    ]


def test_income_account_filter(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": income_flows()}))
    rows = json_rows(*OCTOBER, "-a", "Cuenta 1", "--by", "account")
    assert [r["cuenta"] for r in rows] == ["Cuenta 1", "total"]


def test_income_defaults_to_the_free_90_days(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    route = mock_one_account(mock_api)
    result = invoke("income", "-f", "json")
    params = route.calls.last.request.url.params
    assert (params["dateFrom"], params["dateTo"]) == (free_start(date.today()).isoformat(), date.today().isoformat())
    assert route.call_count == 1 and result.exit_code == 0 and "días" not in result.stderr


def test_income_beyond_the_free_window_without_signature_exits_13(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    route = mock_one_account(mock_api)
    result = runner.invoke(app, ["income", "--days", "200", "-f", "json"])
    assert isinstance(result.exception, PartialResultError)
    assert route.call_count == 1  # only the free span: older ones need a second factor


def test_income_allow_partial_and_free_only(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    route = mock_one_account(mock_api)
    partial = invoke("income", "--year", str(date.today().year - 1), "--allow-partial", "-f", "json")
    assert partial.exit_code == 0 and "Resultado parcial" in partial.stderr
    assert len(json.loads(partial.stdout)) == 13 and route.call_count == 0

    clipped = invoke("income", "--days", "200", "--free-only", "-f", "json")
    assert clipped.exit_code == 0 and "Recortado" in clipped.stderr
    assert route.calls.last.request.url.params["dateFrom"] == free_start(date.today()).isoformat()


@pytest.mark.parametrize(
    "args",
    [
        ["--year", "2025", "--from", "2025-01-01"],
        ["--year", "2025", "--days", "30"],
        ["--year", str(date.today().year + 1)],
        ["--from", "2026-10-01", "--days", "30"],
    ],
)
def test_income_rejects_conflicting_ranges(store: MemorySessionStore, args: list[str]) -> None:
    assert isinstance(runner.invoke(app, ["income", *args]).exception, InputError)
