"""`transfers`, `interest`, `movements --details` and scheduled transfers in `upcoming` (synthetic data)."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_cash, cli_common, cli_upcoming
from myinvestor.cli import app
from myinvestor.errors import InputError
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CASH_UUID, CPERF, cash_account, flow, ok
from tests.test_cli_upcoming import mock_everything
from tests.test_sent_transfers import REF, periodic, sent
from tests.test_transfers_match import op_ref

runner = CliRunner()
NOW = datetime(2026, 11, 3, 9, 0).astimezone()
TRANSFERS = f"{CPERF}/v2/cash-accounts/101/transfers"
REMUNERATION = f"{CPERF}/v2/cash-accounts/101/remuneration"
FLOWS = f"{API}/account/api/v3/account/{CASH_UUID}/flows"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_cash, "now_local", lambda: NOW)
    monkeypatch.setattr(cli_upcoming, "now_local", lambda: NOW)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def mock_transfers(mock_api: respx.MockRouter) -> respx.Route:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    old = sent(reference="120260901000001", emissionDate="2026-09-01T00:00:00.000+0000", executionType="INSTANT")
    return mock_api.get(TRANSFERS).respond(200, json=ok([sent(), old, periodic()]))


# ---- transfers ------------------------------------------------------------------------------------


def test_transfers_json_filters_by_emission_date_locally(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    route = mock_transfers(mock_api)

    result = invoke("transfers", "--from", "2026-10-01", "--to", "2026-11-03", "-f", "json")

    rows = json.loads(result.stdout, parse_float=Decimal)
    assert [(r["fecha"], r["ref"], r["importe"], r["beneficiario"], r["iban_destino"]) for r in rows] == [
        ("2026-11-02", REF, Decimal("-50.00"), "Beneficiaria Ejemplo", "ES0000000000000000000001")
    ]
    assert not route.calls.last.request.url.params  # no date range sent: the API takes none
    assert "00000000T" not in result.stdout and "Ana" not in result.stdout
    assert {call.request.method for call in mock_api.calls} == {"GET"}


def test_transfers_scheduled_shows_the_next_execution(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_transfers(mock_api)

    rows = json.loads(invoke("transfers", "--scheduled", "-f", "json").stdout)

    assert [(r["beneficiario"], r["estado"], r["periodicidad"], r["inicio"], r["proxima"]) for r in rows] == [
        ("Casero", "ACTIVE", "MONTHLY", "2026-10-05", "2026-11-05")
    ]


def test_transfers_ref_finds_one_of_any_date(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_transfers(mock_api)

    rows = json.loads(invoke("transfers", "--ref", "120260901000001", "-f", "json").stdout)
    missing = runner.invoke(app, ["transfers", "--ref", "1"])

    assert [(r["fecha"], r["ejecucion"]) for r in rows] == [("2026-09-01", "INSTANT")]
    assert isinstance(missing.exception, InputError) and "--ref" in str(missing.exception)


def test_transfers_scheduled_rejects_a_date_range(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["transfers", "--scheduled", "--days", "10"])
    assert isinstance(result.exception, InputError)


def test_transfers_table(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_transfers(mock_api)

    result = invoke("--wide", "transfers", "--from", "2026-10-01", "--to", "2026-11-03")

    assert "Transferencias emitidas 2026-10-01 → 2026-11-03" in result.stdout
    assert "-50,00 €" in result.stdout and "Beneficiaria Ejemplo" in result.stdout
    assert "iban_destino" not in result.stdout


# ---- interest -------------------------------------------------------------------------------------

REMUNERATION_DATA = {
    "remunerationDate": "2026-09-01T00:00:00.000+0000",
    "creationUpdateDate": "2026-10-07T00:00:00.000+0000",
    "applicableBaseBalance": 10000,
    "balancePromo": 0,
    "totalBalance": 10000,
    "baseResults": 16.44,
    "promoResults": 0,
    "finalResult": 16.44,
    "taePromotion": 0,
    "calculateTaeAverage": 2.0,
    "taxChargeAmount": 3.12,
    "remunerationPercentage": 2.0,
    "promotionRemunerationPercentage": None,
    "baseRemunerationPercentage": 2.0,
    "paymentDate": "2026-10-07",
    "remunerationType": "DEFAULT",
    "endDateRemuneration": None,
    "snapshotDate": "",
    "cashIncrementReferenceDate": "",
    "cashIncrementPeriodStartDate": None,
    "cashIncrementPeriodEndDate": None,
    "promotionCode": None,
    "officialRemunerationDate": "",
}


def test_interest_json(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(REMUNERATION).respond(200, json=ok(REMUNERATION_DATA))
    mock_api.get(f"{CPERF}/v2/cash-accounts/102/remuneration").respond(
        404, json={"payload": {}, "status": {"code": "NOT_FOUND", "message": "no"}}
    )

    result = invoke("interest", "-f", "json")

    rows = json.loads(result.stdout, parse_float=Decimal)
    assert len(rows) == 1
    row = rows[0]
    assert (row["cuenta"], row["periodo"], row["fecha_pago"], row["fecha_oficial"]) == (
        "Cuenta 1",
        "2026-09",
        "2026-10-07",
        None,
    )
    assert (row["saldo_base"], row["tipo_pct"], row["tae_pct"], row["tipo_promo_pct"]) == (
        Decimal("10000.00"),
        Decimal("2.0"),
        Decimal("2.0"),
        None,
    )
    assert (row["bruto"], row["retencion"], row["neto"]) == (Decimal("16.44"), Decimal("-3.12"), Decimal("13.32"))
    assert "Cuenta 2" in result.stderr


def test_interest_account_filter_and_table(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(REMUNERATION).respond(200, json=ok(REMUNERATION_DATA))

    result = invoke("--wide", "interest", "-a", "101")

    assert "16,44 €" in result.stdout and "13,32 €" in result.stdout and "2,00 %" in result.stdout
    assert "tipo_base_pct" not in result.stdout


# ---- movements --details ----------------------------------------------------------------------------


def charge() -> dict[str, Any]:
    raw = op_ref(REF, "000259", gap="1234567890")
    return flow(1, -50.0, reference="000259", operationType="CARGO POR TRANSFERENCIA", operationRef=raw)


def test_movements_details_adds_the_beneficiary(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": [charge(), flow(2, 10.0)]}))
    route = mock_api.get(TRANSFERS).respond(200, json=ok([sent()]))

    plain = json.loads(invoke("movements", "--days", "60", "-f", "json").stdout)
    assert not route.called and "beneficiario" not in plain[0]

    rows = json.loads(invoke("movements", "--days", "60", "--details", "-f", "json").stdout)

    assert route.call_count == 1
    by_ref = {r["ref"]: (r["beneficiario"], r["iban_destino"]) for r in rows}
    assert by_ref == {"000259": ("Beneficiaria Ejemplo", "ES0000000000000000000001"), "ref2": (None, None)}


def test_movements_details_skips_accounts_without_transfer_charges(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": [flow(2, 10.0)]}))
    route = mock_api.get(TRANSFERS).respond(200, json=ok([]))

    rows = json.loads(invoke("movements", "--days", "60", "--details", "-f", "json").stdout)

    assert not route.called and rows[0]["beneficiario"] is None


# ---- upcoming -------------------------------------------------------------------------------------


def test_upcoming_subtracts_scheduled_transfers(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_everything(mock_api, transfers=[periodic(amount=30.0), sent()])

    rows = json.loads(invoke("upcoming", "-f", "json").stdout, parse_float=Decimal)

    assert [(r["fecha"], r["tipo"], r["producto"], r["importe"], r["disponible_tras"]) for r in rows][2:] == [
        ("2026-11-05", "transferencia", "Casero", Decimal("-30.00"), Decimal("70.00")),
        ("2026-11-05", "aportacion", "Fondo mensual", Decimal("-60.00"), Decimal("10.00")),
    ]
