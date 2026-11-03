"""`movements` and `accounts`: exported fields, stable ids and holders."""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.errors import InputError
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CASH_UUID, CPERF, cash_account, flow, ok

runner = CliRunner()
FLOWS = f"{API}/account/api/v3/account/{CASH_UUID}/flows"
SECOND_UUID = "aaaaaaaa-0000-0000-0000-000000000002"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def holder(kind: str, name: str, *, me: bool = False) -> dict[str, Any]:
    return {"name": name, "surname1": "Pérez", "documentNumber": "00000000T", "me": me, "participantType": kind}


def junior_account() -> dict[str, Any]:
    holders = [holder("REPRESENTATIVE", "Ana", me=True), holder("JUNIOR", "Leo"), holder("REPRESENTATIVE", "Luis")]
    return cash_account(2, alias="Cuenta Leo", holders=holders)


def own_account() -> dict[str, Any]:
    return cash_account(1, holders=[holder("OWNER", "Ana", me=True)])


def mock_accounts(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([own_account(), junior_account()]))


def transfer_out() -> dict[str, Any]:
    return flow(
        1,
        -50.0,
        reference="000259",
        operationType="CARGO POR TRANSFERENCIA",
        operationClass="TR",
        operationOriginLabel="",
        operationTargetLabel="Beneficiario Sintético",
        operationRef="0000000000OPREF0001",
        fileRef="FILE-1",
        isMigrated=False,
    )


# ---- #13: exported fields ------------------------------------------------------


def test_movements_json_exports_account_direction_labels_and_refs(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_accounts(mock_api)
    mock_api.get(FLOWS).respond(
        200,
        json=ok(
            {
                "flowList": [transfer_out(), flow(2, 20.0, operationOriginLabel="Ordenante Sintético")],
                "iban": "ES0000000000000000000001",
            }
        ),
    )

    rows = json.loads(invoke("movements", "-a", "Cuenta 1", "-f", "json").stdout)

    by_ref = {r["ref"]: r for r in rows}
    out = by_ref["000259"]
    assert {k: v for k, v in out.items() if k not in ("importe", "saldo")} == {
        "id": f"{CASH_UUID}:000259",
        "fecha": "2026-10-11",
        "valor_fecha": "2026-10-11",
        "cuenta": "Cuenta 1",
        "cuenta_id": "101",
        "cuenta_uuid": CASH_UUID,
        "iban": "ES0015440000000000000001",
        "sentido": "OUT",
        "tipo": "CARGO POR TRANSFERENCIA",
        "clase": "TR",
        "concepto": "Concepto 1",
        "origen": "",
        "destino": "Beneficiario Sintético",
        "contraparte": "Beneficiario Sintético",
        "ref": "000259",
        "operacion_ref": "0000000000OPREF0001",
        "fichero_ref": "FILE-1",
    }
    assert Decimal(str(out["importe"])) == Decimal("-50.0")
    assert Decimal(str(out["saldo"])) == Decimal("501.0")
    credit = by_ref["ref2"]
    assert (credit["sentido"], credit["contraparte"], credit["destino"]) == ("IN", "Ordenante Sintético", None)
    assert (credit["operacion_ref"], credit["fichero_ref"]) == (None, None)


def test_movements_iban_falls_back_to_the_statement(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1, iban=None)]))
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": [flow(1, -1.0)], "iban": "ES0000000000000000000009"}))

    [row] = json.loads(invoke("movements", "-f", "json").stdout)

    assert row["iban"] == "ES0000000000000000000009"


def test_movements_csv_has_the_new_columns(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_accounts(mock_api)
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": [transfer_out()]}))

    [row] = list(csv.DictReader(io.StringIO(invoke("movements", "-a", "101", "-f", "csv").stdout)))

    assert (row["id"], row["sentido"], row["contraparte"], row["operacion_ref"]) == (
        f"{CASH_UUID}:000259",
        "OUT",
        "Beneficiario Sintético",
        "0000000000OPREF0001",
    )


def test_movements_table_keeps_its_columns(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_accounts(mock_api)
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": [transfer_out()]}))

    out = invoke("movements", "-a", "101").stdout

    assert "000259" in out and "fecha" in out
    assert "contraparte" not in out and "cuenta_uuid" not in out and CASH_UUID not in out


# ---- #14: stable ids -------------------------------------------------------------


def no_ref(i: int, amount: float, **overrides: Any) -> dict[str, Any]:
    return flow(i, amount, reference=None, **overrides)


def test_overlapping_ranges_give_the_same_ids(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([own_account()]))
    early = flow(1, -1.0)
    twin_a = no_ref(5, -9.99, totalBalance=100.0)
    twin_b = no_ref(5, -9.99, totalBalance=90.01)
    lonely = no_ref(6, 3.0)
    late = flow(9, 4.0)
    by_range = {
        "2026-10-01": [early, twin_a, twin_b, lonely],
        "2026-10-14": [lonely, twin_b, twin_a, late],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok({"flowList": by_range[request.url.params["dateFrom"]]}))

    mock_api.get(FLOWS).mock(side_effect=respond)

    def ids(date_from: str, date_to: str) -> dict[tuple[str, str], str]:
        rows = json.loads(invoke("movements", "--from", date_from, "--to", date_to, "-f", "json").stdout)
        return {(r["fecha"], str(r["saldo"])): r["id"] for r in rows}

    first = ids("2026-10-01", "2026-10-16")
    second = ids("2026-10-14", "2026-10-31")

    shared = first.keys() & second.keys()
    assert len(shared) == 3
    assert {k: first[k] for k in shared} == {k: second[k] for k in shared}
    assert len(set(first.values())) == 4 and len(set(second.values())) == 4
    assert sum(i.endswith("#2") for i in first.values()) == 1


def test_movement_by_id(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_accounts(mock_api)
    own = mock_api.get(FLOWS).respond(200, json=ok({"flowList": [transfer_out(), flow(2, 20.0)]}))
    other = mock_api.get(f"{API}/account/api/v3/account/{SECOND_UUID}/flows").respond(200, json=ok({"flowList": []}))

    row = json.loads(invoke("movements", "--id", f"{CASH_UUID}:000259", "-f", "json").stdout)

    assert row["ref"] == "000259" and row["contraparte"] == "Beneficiario Sintético"
    assert own.called and not other.called


def test_movement_by_id_table_is_one_field_per_line(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_accounts(mock_api)
    mock_api.get(FLOWS).respond(200, json=ok({"flowList": [transfer_out()]}))

    out = invoke("movements", "--id", f"{CASH_UUID}:000259").stdout

    assert "operacion_ref" in out and "0000000000OPREF0001" in out and "-50,00 €" in out


def test_movement_by_unknown_id_says_which_range_was_searched(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_accounts(mock_api)
    mock_api.get(url__regex=r".*/account/api/v3/account/.*/flows").respond(200, json=ok({"flowList": []}))

    result = runner.invoke(app, ["movements", "--id", "nope", "--from", "2026-10-01", "--to", "2026-10-31"])

    assert isinstance(result.exception, InputError)
    assert "2026-10-01" in str(result.exception) and "--from" in str(result.exception)


# ---- #15: holders ------------------------------------------------------------------


def test_accounts_show_type_role_and_holder(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_accounts(mock_api)

    rows = json.loads(invoke("accounts", "-f", "json").stdout)

    assert [(r["tipo"], r["rol"], r["titular"]) for r in rows] == [
        ("CASH_ACCOUNT", "OWNER", "Ana Pérez"),
        ("CASH_ACCOUNT", "REPRESENTATIVE", "Leo Pérez"),
    ]
    assert rows[1]["titulares"] == [
        {"nombre": "Ana Pérez", "rol": "REPRESENTATIVE", "yo": True},
        {"nombre": "Leo Pérez", "rol": "JUNIOR", "yo": False},
        {"nombre": "Luis Pérez", "rol": "REPRESENTATIVE", "yo": False},
    ]
    assert "00000000T" not in json.dumps(rows)


def test_accounts_csv_and_table_have_no_holder_list(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_accounts(mock_api)

    [header, *_] = invoke("accounts", "-f", "csv").stdout.splitlines()
    table = invoke("accounts").stdout

    assert "rol" in header.split(",") and "titulares" not in header
    assert "titulares" not in table and "00000000T" not in table


@pytest.mark.parametrize(("owner", "expected"), [("me", ["101"]), ("junior", ["102"]), ("other", [])])
def test_accounts_owner_filter(
    mock_api: respx.MockRouter, store: MemorySessionStore, owner: str, expected: list[str]
) -> None:
    mock_accounts(mock_api)
    rows = json.loads(invoke("accounts", "--owner", owner, "-f", "json").stdout)
    assert [r["id"] for r in rows] == expected


def test_movements_owner_filter(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_accounts(mock_api)
    own = mock_api.get(FLOWS).respond(200, json=ok({"flowList": [flow(1, -1.0)]}))
    minor = mock_api.get(f"{API}/account/api/v3/account/{SECOND_UUID}/flows").respond(
        200, json=ok({"flowList": [flow(2, 5.0)]})
    )

    rows = json.loads(invoke("movements", "--owner", "junior", "-f", "json").stdout)

    assert [r["cuenta_uuid"] for r in rows] == [SECOND_UUID]
    assert minor.called and not own.called
