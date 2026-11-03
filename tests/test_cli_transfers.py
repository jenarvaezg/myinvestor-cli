"""`internal-transfers` and the `pareja_id` / `traspaso_interno` fields of `movements` (synthetic data)."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.errors import PartialResultError
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CASH_UUID, CPERF, cash_account, flow, ok
from tests.test_psd2_window import FREE, FakeFlows, mock_api_with, on
from tests.test_transfers_match import ACCOUNT_A, ACCOUNT_B, op_ref, sepa_ref

runner = CliRunner()
JUNIOR_UUID = "aaaaaaaa-0000-0000-0000-000000000002"
FIRST, SECOND, FOREIGN = "120261102758295", "120261102758296", "120261014477050"
YESTERDAY = date.today() - timedelta(days=1)
STAMP = f"{YESTERDAY.isoformat()}T00:00:00.000+0000"
INTERNAL = "Transferencia desde MyInvestor"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def leg(i: int, amount: float, counter: str, op_id: str, block: str, **overrides: Any) -> dict[str, Any]:
    out = amount < 0
    ref = op_ref(op_id, counter, account=ACCOUNT_A if out else ACCOUNT_B, block=block, check="9" if out else "7")
    kind = "CARGO POR TRANSFERENCIA" if out else "ABONO POR TRANSFERENCIA"
    stamp = {"operationDate": STAMP, "valueDate": STAMP}
    return flow(i, amount, reference=counter, operationType=kind, operationRef=ref, **stamp, **overrides)


def own_flows() -> list[dict[str, Any]]:
    """Two own transfers of 50 to the junior account the same day, and a card payment."""
    return [
        leg(1, -50.0, "000259", FIRST, "3000000001", concept="Paga"),
        leg(2, -50.0, "000260", SECOND, "3000000002", concept=INTERNAL),
        flow(3, -12.3, concept="Compra con tarjeta", operationType="COMPRA CON TARJETA"),
    ]


def junior_flows() -> list[dict[str, Any]]:
    """Both credits, a gift from another MyInvestor client (an id none of my accounts has) and a SEPA."""
    return [
        leg(4, 50.0, "000032", FIRST, "3000000001", concept=INTERNAL),
        leg(5, 50.0, "000033", FOREIGN, "3000000099", concept=INTERNAL),
        leg(6, 50.0, "000034", SECOND, "3000000002", concept=INTERNAL),
        flow(
            7,
            50.0,
            reference="000035",
            concept="Regalo",
            operationType="TRANSFERENCIA SEPA",
            operationRef=sepa_ref("000035"),
            operationDate=STAMP,
            valueDate=STAMP,
        ),
    ]


@pytest.fixture
def accounts(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(
        200, json=ok([cash_account(1, alias="Mi cuenta"), cash_account(2, alias="Junior")])
    )
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(200, json=ok({"flowList": own_flows()}))
    mock_api.get(f"{API}/account/api/v3/account/{JUNIOR_UUID}/flows").respond(
        200, json=ok({"flowList": junior_flows()})
    )


def pair(out: str, into: str, key: str, concept: str) -> dict[str, Any]:
    return {
        "fecha": YESTERDAY.isoformat(),
        "desde_cuenta": "Mi cuenta",
        "desde_id": f"{CASH_UUID}:{out}",
        "hacia_cuenta": "Junior",
        "hacia_id": f"{JUNIOR_UUID}:{into}",
        "concepto": concept,
        "clave": key,
        "match": "key",
    }


PAIRS = [pair("000260", "000034", SECOND, INTERNAL), pair("000259", "000032", FIRST, "Paga")]


@pytest.mark.usefixtures("accounts", "store")
def test_regression_two_own_transfers_a_gift_and_a_sepa() -> None:
    rows = json.loads(invoke("internal-transfers", "-f", "json").stdout)

    assert [{k: v for k, v in r.items() if k != "importe"} for r in rows] == PAIRS
    assert {Decimal(str(r["importe"])) for r in rows} == {Decimal(50)}


@pytest.mark.usefixtures("accounts", "store")
def test_unmatched_adds_the_gift_and_the_sepa() -> None:
    rows = json.loads(invoke("internal-transfers", "--unmatched", "-f", "json").stdout)

    lonely = [(r["desde_id"], r["hacia_id"], r["clave"], r["match"]) for r in rows if r["match"] is None]
    assert sorted(lonely, key=str) == [
        (None, f"{JUNIOR_UUID}:000033", FOREIGN, None),
        (None, f"{JUNIOR_UUID}:000035", None, None),
    ]
    assert len(rows) == 4


@pytest.mark.usefixtures("accounts", "store")
def test_account_filter_keeps_transfers_touching_it() -> None:
    assert len(json.loads(invoke("internal-transfers", "-a", "Junior", "--unmatched", "-f", "json").stdout)) == 4
    assert len(json.loads(invoke("internal-transfers", "-a", "Mi cuenta", "--unmatched", "-f", "json").stdout)) == 2


@pytest.mark.usefixtures("accounts", "store")
def test_internal_transfers_csv_and_table() -> None:
    rows = list(csv.DictReader(io.StringIO(invoke("internal-transfers", "-f", "csv").stdout)))
    table = invoke("internal-transfers").stdout

    assert rows == [{**p, "importe": "50.00"} for p in PAIRS]
    assert "50,00 €" in table and "Junior" in table and "key" in table
    assert CASH_UUID not in table


@pytest.mark.usefixtures("accounts", "store")
def test_movements_mark_both_legs() -> None:
    rows = {r["ref"]: r for r in json.loads(invoke("movements", "-f", "json").stdout)}

    marks = {ref: (r["pareja_id"], r["traspaso_interno"]) for ref, r in rows.items()}
    assert marks == {
        "000259": (f"{JUNIOR_UUID}:000032", True),
        "000260": (f"{JUNIOR_UUID}:000034", True),
        "ref3": (None, False),
        "000032": (f"{CASH_UUID}:000259", True),
        "000033": (None, False),
        "000034": (f"{CASH_UUID}:000260", True),
        "000035": (None, False),
    }


@pytest.mark.usefixtures("accounts", "store")
def test_movements_search_keeps_the_partner_id() -> None:
    [row] = json.loads(invoke("movements", "-s", "paga", "-f", "json").stdout)

    assert row["pareja_id"] == f"{JUNIOR_UUID}:000032"


@pytest.mark.usefixtures("accounts", "store")
def test_movements_csv_has_the_fields_and_the_table_does_not() -> None:
    header = invoke("movements", "-f", "csv").stdout.splitlines()[0].split(",")
    table = invoke("movements").stdout

    assert {"pareja_id", "traspaso_interno"} <= set(header)
    assert "pareja_id" not in table and "traspaso_interno" not in table


# ---- PSD2 window: same behaviour as `movements` ----------------------------------------


def window_flows() -> dict[str, list[dict[str, Any]]]:
    """A transfer inside the free window, and something older that needs a signature."""
    ref_out = op_ref(FIRST, "000001", account=ACCOUNT_A)
    ref_in = op_ref(FIRST, "000002", account=ACCOUNT_B, check="7")
    return {
        CASH_UUID: [
            on(FREE, 1, -50.0, reference="000001", operationType="CARGO POR TRANSFERENCIA", operationRef=ref_out),
            on(FREE - timedelta(days=30), 3, -10.0),
        ],
        JUNIOR_UUID: [
            on(FREE, 2, 50.0, reference="000002", operationType="ABONO POR TRANSFERENCIA", operationRef=ref_in),
        ],
    }


@pytest.fixture
def no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)


@pytest.mark.usefixtures("store", "no_tty")
def test_missing_signed_span_shows_the_pairs_and_fails_as_partial(mock_api: respx.MockRouter) -> None:
    mock_api_with(mock_api, FakeFlows(window_flows()))
    since = (FREE - timedelta(days=60)).isoformat()

    result = runner.invoke(app, ["internal-transfers", "--from", since, "-f", "json"])

    assert [r["clave"] for r in json.loads(result.stdout)] == [FIRST]
    assert isinstance(result.exception, PartialResultError)


@pytest.mark.parametrize("flag", ["--allow-partial", "--free-only"])
@pytest.mark.usefixtures("store", "no_tty")
def test_allow_partial_and_free_only_exit_0(mock_api: respx.MockRouter, flag: str) -> None:
    fake = FakeFlows(window_flows())
    mock_api_with(mock_api, fake)
    since = (FREE - timedelta(days=60)).isoformat()

    result = runner.invoke(app, ["internal-transfers", flag, "--from", since, "-f", "json"])

    assert result.exit_code == 0, result.output
    assert [r["clave"] for r in json.loads(result.stdout)] == [FIRST]
    if flag == "--free-only":
        assert {s for _, s, _, _ in fake.requests} == {FREE.isoformat()}
