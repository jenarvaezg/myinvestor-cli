"""#25: `snapshot` (one JSON per day, sections that fail on their own, delta) and `summary`; #33: its `upcoming`."""

from __future__ import annotations

import json
import stat
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli, cli_common
from myinvestor.cli import app
from myinvestor.errors import EXIT_ERROR, EXIT_NETWORK, EXIT_NO_SESSION, EXIT_SIGNATURE, InputError
from myinvestor.http import build_http_client
from myinvestor.session import Session
from myinvestor.snapshot import (
    SCHEMA,
    IncompleteSnapshotError,
    compute_delta,
    latest_before,
    worst_exit_code,
)
from myinvestor.state import StateFile, default_state_path
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CASH_UUID, CPERF, cash_account, flow, ok, order, securities_account, signature_pending

runner = CliRunner()
TODAY = date.today()
DAY = timedelta(days=1)
SECOND_UUID = "aaaaaaaa-0000-0000-0000-000000000002"
FLOWS = r".*/account/api/v3/account/(?P<uuid>[^/]+)/flows"
SIGNATURE = f"{API}/signature/api/v3/signature"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    # no 4 retries with backoff on the 5xx of these tests
    monkeypatch.setattr(cli_common, "build_http_client", partial(build_http_client, retries=0, backoff=0))
    return memory


def on(day: date, i: int, amount: float, kind: str) -> dict[str, Any]:
    stamp = f"{day.isoformat()}T00:00:00.000+0000"
    return flow(i, amount, operationDate=stamp, valueDate=stamp, operationType=kind)


class Flows:
    """Flows endpoint over a list of movements per account, filtering by operation date."""

    def __init__(self, by_account: dict[str, list[dict[str, Any]]], *, always_202: bool = False) -> None:
        self.by_account = by_account
        self.always_202 = always_202
        self.requests: list[tuple[str, str, str]] = []

    def __call__(self, request: httpx.Request, uuid: str) -> httpx.Response:
        start, end = request.url.params["dateFrom"], request.url.params["dateTo"]
        self.requests.append((uuid, start, end))
        if self.always_202:
            return httpx.Response(202, json=signature_pending("sig-1"))
        rows = [f for f in self.by_account.get(uuid, []) if start <= f["operationDate"][:10] <= end]
        return httpx.Response(200, json=ok({"flowList": rows}))


def movements_today() -> list[dict[str, Any]]:
    return [
        on(TODAY - DAY, 1, 500.0, "TRANSFERENCIA SEPA"),  # money from outside
        on(TODAY - DAY, 2, 1.5, "LIQUIDAC. INTERESES"),  # return, not a contribution
        on(TODAY, 3, -100.0, "SUSCRIPCION IIC"),  # cash → fund
    ]


def mock_everything(mock_api: respx.MockRouter, flows: Flows | None = None) -> Flows:
    fake = flows or Flows({CASH_UUID: movements_today()})
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))  # saldo 1000.30
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/holds").respond(
        200,
        json=ok([{"id": "h1", "accountId": CASH_UUID, "type": "INVESTMENT_ORDER", "amount": 25.0, "currency": "EUR"}]),
    )
    mock_api.get(url__regex=FLOWS).mock(side_effect=fake)
    mock_api.get(f"{CPERF}/v2/cash-accounts/101/transfers").respond(200, json=ok([]))
    mock_api.get(f"{CPERF}/v2/automatic-contributions/self").respond(
        200,
        json=ok(
            [
                {
                    "contributionId": "c1",
                    "amount": 60.0,
                    "status": "ACTIVE",
                    "isin": "IE0000000002",
                    "fundName": "Fondo mensual",
                    "fromAccountId": "101",
                    "contributionTimeFrame": {
                        "recurrence": "ONE_MONTH",
                        "nextExecutionDate": (TODAY + 2 * DAY).isoformat(),
                    },
                }
            ]
        ),
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(
        200,
        json=ok(securities_account("201", cashAccountId="101")),  # valor 1100.55, invertido 1000
    )
    mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(
        200,
        json=ok([order("o1", TODAY.isoformat(), status="PENDING", cash=25.0), order("o0", "2026-10-01")]),
    )
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(
        200,
        json=ok(
            [
                {
                    "accountId": "301",
                    "externalAccountCode": "P301",
                    "cashAccountId": "101",
                    "marketValue": 2000.0,
                    "totalInvested": 1800.0,
                    "profit": 200.0,
                    "totalReturn": 11.11,
                }
            ]
        ),
    )
    mock_api.get(f"{CPERF}/v2/pension-accounts/301").respond(
        200,
        json=ok(
            [
                {
                    "dgsCode": "N0001",
                    "name": "Plan genérico",
                    "marketValue": 2000.0,
                    "totalInvested": 1800.0,
                    "liquidationValueDate": "2026-10-30T00:00:00.000Z",
                }
            ]
        ),
    )
    lots = {"plusMinusList": [{"securities": 12.345678, "investment": 920.0, "marketValue": 1100.55}]}
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/investment-summary/funds/IE0000000001/added-values").respond(
        200,
        json=ok(lots),  # the fund came from a transfer: its tax cost is 920, not the 1000 of `invertido`
    )
    point = {"date": "2026-10-30", "totalShares": 100, "marketValue": 2000.0, "investmentAddedValue": 1800.0}
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/positions").respond(200, json=ok({"positions": {"N0001": [point]}}))
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders").respond(
        200,
        json=ok(
            [
                order(
                    "p1",
                    TODAY.isoformat(),
                    securitiesAccountId=None,
                    pensionAccountId="301",
                    isin=None,
                    dgsCode="N0001",
                    fundName="Plan genérico",
                    status="IN_PROGRESS",
                    cash=10.0,
                )
            ]
        ),
    )
    return fake


def run(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def snapshot_of(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout, parse_float=Decimal)


def run_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[Any, str, str]:
    monkeypatch.setattr(sys, "argv", ["myinvestor", *args])
    try:
        cli.main()
        status: Any = 0
    except SystemExit as exc:
        status = exc.code or 0
    out = capsys.readouterr()
    return status, out.out, out.err


def previous_snapshot(day: date, **overrides: Any) -> dict[str, Any]:
    """A saved snapshot of `day` that saw none of `movements_today()`."""
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "as_of": f"{day.isoformat()}T09:00:00+01:00",
        "total": 3539.35,
        "cash": [{"id": "101", "alias": "Cuenta 1", "saldo": 598.80}],
        "securities": [{"producto": "valores", "id": "201", "nombre": "S201", "valor": 950.55, "invertido": 900.0}],
        "pensions": [{"producto": "pensiones", "id": "301", "nombre": "P301", "valor": 1990.0, "invertido": 1800.0}],
        "movements": {"desde": (day - 3 * DAY).isoformat(), "hasta": day.isoformat(), "huecos": [], "movimientos": []},
    }
    return {**base, **overrides}


def write(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


# ---- the whole snapshot --------------------------------------------------------------------------


def test_snapshot_has_every_section_from_one_client(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_everything(mock_api)

    result = run("snapshot")

    assert result.exit_code == 0, result.output
    doc = snapshot_of(result)
    assert doc["schema"] == SCHEMA
    assert datetime.fromisoformat(doc["as_of"]).date() == TODAY
    assert doc["total"] == Decimal("4100.85")  # 1000.30 + 1100.55 + 2000
    [cash] = doc["cash"]
    assert (cash["id"], cash["rol"], cash["saldo"], cash["fecha_valor"]) == (
        "101",
        "OWNER",
        Decimal("1000.30"),
        str(TODAY),
    )
    assert cash["titulares"] == [{"nombre": None, "rol": "OWNER", "yo": True}]
    [sec] = doc["securities"]
    assert (sec["producto"], sec["valor"], sec["invertido"]) == ("valores", Decimal("1100.55"), Decimal("1000.00"))
    assert [(p["isin"], p["peso"]) for p in sec["posiciones"]] == [("IE0000000001", Decimal(100))]
    [pen] = doc["pensions"]
    assert (pen["valor"], pen["fecha_valor"], pen["planes"][0]["codigo"]) == (Decimal("2000.00"), "2026-10-30", "N0001")
    assert [o["ref"] for o in doc["pending"]["orders"]] == ["p1", "o1"]  # newest first, like `orders`
    # the API sends this hold positive: a hold is money going out, negative like in `movements`
    assert [(h["id"], h["importe"]) for h in doc["pending"]["holds"]] == [("h1", Decimal("-25.00"))]
    assert [(r["tipo"], r["producto"], r["estado"]) for r in doc["upcoming"]] == [
        ("orden", "Fondo indexado", "en_curso"),  # also in `pending.orders`
        ("orden", "Plan genérico", "en_curso"),
        ("aportacion", "Fondo mensual", "programada"),
    ]
    movements = doc["movements"]
    assert (movements["desde"], movements["hasta"], movements["huecos"]) == (str(TODAY - 3 * DAY), str(TODAY), [])
    assert [m["ref"] for m in movements["movimientos"]] == ["ref3", "ref2", "ref1"]
    assert [m["ref"] for m in doc["new"]["movements"]] == ["ref3", "ref2", "ref1"]
    assert [o["ref"] for o in doc["new"]["orders"]] == ["o1", "o0"]
    assert [o["ref"] for o in doc["new"]["pension_orders"]] == ["p1"]
    assert doc["delta"] is None
    assert doc["errors"] == []
    assert '"total": 4100.85' in result.stdout  # exact amounts, as numbers
    assert {c.request.method for c in mock_api.calls} == {"GET"}
    assert sum(c.request.url.path.endswith("/cash-accounts/self") for c in mock_api.calls) == 1
    assert len(mock_api.calls) == 4 + 3 * 1 + 2 * 1 + 3 * 1 + 1  # 4 + 3·C + 2·V + 3·P + F, as in the README


def test_new_is_marked_unless_no_mark(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_everything(mock_api)

    first = snapshot_of(run("snapshot", "--no-mark"))["new"]
    second = run("snapshot", "--no-mark")
    assert snapshot_of(second)["new"] == first  # nothing marked: a routine that fails halfway loses nothing
    assert "state mark" in second.stderr

    assert run("state", "mark").exit_code == 0
    assert snapshot_of(run("snapshot"))["new"] == {"movements": [], "orders": [], "pension_orders": []}


def test_no_new_leaves_the_state_alone(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    flows = mock_everything(mock_api)

    doc = snapshot_of(run("snapshot", "--no-new"))

    assert doc["new"] is None
    assert not default_state_path().exists()
    assert {start for _, start, _ in flows.requests} == {str(TODAY - 3 * DAY)}  # only the delta window


# ---- sections that fail on their own -----------------------------------------------------------------


def test_a_5xx_in_pensions_does_not_abort_the_rest(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    mock_everything(mock_api)
    mock_api.get(f"{CPERF}/v2/pension-accounts/301").respond(503, json={"status": {"code": "ERROR"}})

    status, out, err = run_main(monkeypatch, capsys, "snapshot", "--no-new")

    assert status == EXIT_NETWORK
    doc = json.loads(out, parse_float=Decimal)
    assert doc["pensions"] is None and doc["total"] is None
    assert doc["cash"] and doc["securities"] and doc["upcoming"] and doc["pending"]["orders"]
    [error] = doc["errors"]
    assert (error["section"], error["error"], error["exit_code"]) == ("pensions", "api", EXIT_NETWORK)
    assert "503" in error["message"]
    line = json.loads(err.strip().splitlines()[-1])
    assert (line["error"], line["exit_code"]) == ("incomplete_snapshot", EXIT_NETWORK)


@pytest.mark.parametrize(("args", "tty"), [([], False), (["--no-signature"], True)])
def test_movements_asking_for_a_signature_never_start_one(
    mock_api: respx.MockRouter,
    store: MemorySessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    args: list[str],
    tty: bool,
) -> None:
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: tty)
    mock_everything(mock_api, Flows({}, always_202=True))
    started = mock_api.post(url__regex=f"{SIGNATURE}/.*").respond(200, json=ok({"signatureId": "s1"}))

    status, out, _ = run_main(monkeypatch, capsys, *args, "snapshot")

    assert status == EXIT_SIGNATURE
    assert not started.called
    doc = json.loads(out, parse_float=Decimal)
    [error] = doc["errors"]
    assert (error["section"], error["error"], error["exit_code"]) == ("movements", "partial_result", EXIT_SIGNATURE)
    assert doc["movements"]["huecos"][0]["cuenta_id"] == "101"
    assert doc["new"]["movements"] == [] and doc["new"]["orders"]  # the rest of `new` still comes out
    assert doc["total"] == Decimal("4100.85")
    assert CASH_UUID not in StateFile(default_state_path()).load().marked.movements  # a gap is never marked


def test_section_errors_are_reported_from_the_typer_app_too(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_everything(mock_api)
    mock_api.get(f"{CPERF}/v2/automatic-contributions/self").respond(404, json={"status": {"code": "NOT_FOUND"}})

    result = runner.invoke(app, ["snapshot", "--no-new"])

    assert isinstance(result.exception, IncompleteSnapshotError)
    assert result.exception.exit_code == EXIT_ERROR
    doc = snapshot_of(result)
    assert doc["upcoming"] is None and doc["pending"] is not None
    assert [e["section"] for e in doc["errors"]] == ["upcoming"]


def test_worst_exit_code_order() -> None:
    assert worst_exit_code([]) == 0
    assert worst_exit_code([EXIT_NETWORK, EXIT_ERROR]) == EXIT_ERROR
    assert worst_exit_code([EXIT_NETWORK, EXIT_SIGNATURE, EXIT_ERROR]) == EXIT_SIGNATURE
    assert worst_exit_code([EXIT_SIGNATURE, EXIT_NO_SESSION]) == EXIT_NO_SESSION


# ---- delta -----------------------------------------------------------------------------------------------


def test_delta_separates_contributions_from_market(
    mock_api: respx.MockRouter, store: MemorySessionStore, tmp_path: Path
) -> None:
    mock_everything(mock_api)
    before = write(tmp_path / "ayer.json", previous_snapshot(TODAY - DAY))

    delta = snapshot_of(run("snapshot", "--no-new", "--previous", str(before)))["delta"]

    assert delta["desde"] == f"{TODAY - DAY}T09:00:00+01:00"
    assert [(r["producto"], r["id"], r["valor"], r["aportado"], r["mercado"]) for r in delta["por_producto"]] == [
        # +500 transfer and -100 into the fund are contributions; the 1.50 of interest is return
        ("efectivo", "101", Decimal("401.50"), Decimal("400.00"), Decimal("1.50")),
        ("valores", "201", Decimal("150.00"), Decimal("100.00"), Decimal("50.00")),
        ("pensiones", "301", Decimal("10.00"), Decimal("0.00"), Decimal("10.00")),
    ]
    # the 100 moved from cash into the fund cancels out: only the 500 from outside is new money
    assert (delta["total"], delta["aportado"], delta["mercado"]) == (
        Decimal("561.50"),
        Decimal("500.00"),
        Decimal("61.50"),
    )


def test_securities_and_pensions_carry_the_tax_cost(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_everything(mock_api)

    doc = snapshot_of(run("snapshot", "--no-new"))

    [account] = doc["securities"]
    [fund] = account["posiciones"]
    assert (fund["invertido"], fund["coste_fiscal"], fund["ganancia_real"], fund["rentab_real"]) == (
        Decimal("1000.00"),
        Decimal("920.00"),
        Decimal("180.55"),
        Decimal("19.63"),
    )
    assert (account["coste_fiscal"], account["ganancia_real"]) == (Decimal("920.00"), Decimal("180.55"))
    [pension] = doc["pensions"]
    assert (pension["coste_fiscal"], pension["planes"][0]["coste_fiscal"]) == (Decimal("1800.00"), Decimal("1800.00"))
    assert doc["errors"] == []


def test_a_failed_tax_cost_only_empties_those_keys(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    mock_everything(mock_api)
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/investment-summary/funds/IE0000000001/added-values").respond(
        503, json={"status": {"code": "ERROR"}}
    )

    status, out, _ = run_main(monkeypatch, capsys, "snapshot", "--no-new")

    assert status == EXIT_NETWORK
    doc = json.loads(out, parse_float=Decimal)
    [account] = doc["securities"]
    assert account["valor"] == Decimal("1100.55") and account["posiciones"]
    assert account["coste_fiscal"] is None and account["posiciones"][0]["ganancia_real"] is None
    assert doc["pensions"][0]["coste_fiscal"] == Decimal("1800.00")
    assert [e["section"] for e in doc["errors"]] == ["securities.fiscal"]


def test_delta_counts_the_tax_cost_when_both_snapshots_know_it() -> None:
    """A transfer between funds raises `invertido` (to the value moved) but not the tax cost: no money came in."""
    day = date(2026, 11, 4)
    before = {"id": "201", "nombre": "S201", "valor": Decimal(1000), "invertido": Decimal(920)}
    after = {"id": "201", "nombre": "S201", "valor": Decimal(1010), "invertido": Decimal(1000)}
    previous = delta_doc(day - DAY, [], securities=[{**before, "coste_fiscal": Decimal(920)}])
    current = delta_doc(day, [], securities=[{**after, "coste_fiscal": Decimal(920)}])
    old = delta_doc(day - DAY, [], securities=[before])  # saved before `coste_fiscal` existed

    [fiscal] = [r for r in compute_delta(current, previous)["por_producto"] if r["producto"] == "valores"]
    [mixed] = [r for r in compute_delta(current, old)["por_producto"] if r["producto"] == "valores"]

    assert (fiscal["aportado"], fiscal["mercado"]) == (0, 10)
    assert (mixed["aportado"], mixed["mercado"]) == (80, -70)  # `invertido` on both sides, as before


def delta_doc(day: date, movements: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    base = {
        "as_of": f"{day.isoformat()}T09:00:00+01:00",
        "total": Decimal(0),
        "cash": [{"id": "101", "alias": "A", "saldo": Decimal(0)}, {"id": "102", "alias": "B", "saldo": Decimal(0)}],
        "securities": [],
        "pensions": [],
        "movements": {"desde": (day - 3 * DAY).isoformat(), "huecos": [], "movimientos": movements},
    }
    return {**base, **overrides}


def mv(mid: str, account: str, day: date, amount: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "cuenta_id": account,
        "fecha": day.isoformat(),
        "importe": Decimal(amount),
        "tipo": kind,
        **extra,
    }


def test_cash_contributions_count_only_what_the_previous_snapshot_did_not_see() -> None:
    day = date(2026, 11, 4)
    seen = mv("a", "101", day - DAY, "20", "TRANSFERENCIA SEPA")
    previous = delta_doc(day - DAY, [seen])
    current = delta_doc(
        day,
        [
            seen,  # already in the previous balance
            mv("b", "101", day - DAY, "30", "TRANSFERENCIA SEPA"),  # same day, booked after the previous snapshot
            mv("c", "101", day, "-5", "COMISION CUSTODIA"),  # a fee: market, not a contribution
            mv("d", "101", day, "-50", "CARGO POR TRANSFERENCIA", traspaso_interno=True),  # A → B
            mv("e", "102", day, "50", "ABONO POR TRANSFERENCIA", traspaso_interno=True),
            mv("f", "101", day - 9 * DAY, "70", "TRANSFERENCIA SEPA"),  # older than the previous one looked at
        ],
        total=Decimal(25),
        cash=[{"id": "101", "alias": "A", "saldo": Decimal(-25)}, {"id": "102", "alias": "B", "saldo": Decimal(50)}],
    )
    current["movements"]["desde"] = (day - 10 * DAY).isoformat()

    delta = compute_delta(current, previous)

    rows = {r["id"]: (r["valor"], r["aportado"], r["mercado"]) for r in delta["por_producto"]}
    assert rows == {"101": (-25, -20, -5), "102": (50, 50, 0)}
    assert (delta["total"], delta["aportado"], delta["mercado"]) == (25, 30, -5)  # the internal 50 cancels out


def test_delta_is_unknown_where_it_cannot_be_computed() -> None:
    day = date(2026, 11, 4)
    previous = delta_doc(day - DAY, [], pensions=None, total=None)
    current = delta_doc(day, [])
    current["movements"]["huecos"] = [{"cuenta_id": "102"}]

    delta = compute_delta(current, previous)

    assert {r["id"]: r["aportado"] for r in delta["por_producto"]} == {"101": 0, "102": None}
    assert (delta["total"], delta["aportado"], delta["mercado"]) == (None, None, None)
    late = delta_doc(day, [])
    late["movements"]["desde"] = day.isoformat()  # does not reach back to the previous day
    assert {r["aportado"] for r in compute_delta(late, delta_doc(day - 2 * DAY, []))["por_producto"]} == {None}


# ---- --save and the automatic previous ---------------------------------------------------------------


def test_save_writes_the_day_file_and_compares_with_the_last_one_before_today(
    mock_api: respx.MockRouter, store: MemorySessionStore, tmp_path: Path
) -> None:
    mock_everything(mock_api)
    out = tmp_path / "fotos"
    write(out / f"{TODAY - 5 * DAY}.json", previous_snapshot(TODAY - 5 * DAY))
    write(out / f"{TODAY - DAY}.json", previous_snapshot(TODAY - DAY))
    write(out / f"{TODAY}.json", {"schema": SCHEMA, "as_of": "basura de una ejecución anterior"})
    (out / "notas.txt").write_text("no es un snapshot")

    result = run("snapshot", "--save", str(out), "--no-new")

    assert result.exit_code == 0, result.output
    doc = snapshot_of(result)
    assert doc["delta"]["desde"] == f"{TODAY - DAY}T09:00:00+01:00"
    saved = out / f"{TODAY}.json"
    assert json.loads(saved.read_text(), parse_float=Decimal) == doc  # replaced, same as stdout
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert latest_before(out, TODAY + DAY) == saved


def test_save_creates_a_private_directory_and_the_first_run_has_no_delta(
    mock_api: respx.MockRouter, store: MemorySessionStore, tmp_path: Path
) -> None:
    mock_everything(mock_api)
    out = tmp_path / "nuevo" / "fotos"

    doc = snapshot_of(run("snapshot", "--save", str(out), "--no-new"))

    assert doc["delta"] is None
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert (out / f"{TODAY}.json").exists()


def test_a_wrong_previous_fails_before_asking_anything(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["snapshot", "--previous", "/no/existe.json"])

    assert isinstance(result.exception, InputError)
    assert not mock_api.calls


def test_a_corrupt_new_state_only_fails_the_new_sections(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_everything(mock_api)
    default_state_path().parent.mkdir(parents=True)
    default_state_path().write_text("basura")

    result = runner.invoke(app, ["snapshot"])

    doc = snapshot_of(result)
    assert doc["new"] == {"movements": None, "orders": None, "pension_orders": None}
    assert [(e["section"], e["exit_code"]) for e in doc["errors"]] == [
        ("new.movements", EXIT_ERROR),
        ("new.orders", EXIT_ERROR),
        ("new.pension_orders", EXIT_ERROR),
    ]
    assert len(doc["movements"]["movimientos"]) == 3 and doc["total"] is not None
    assert default_state_path().read_text() == "basura"  # left alone


# ---- summary -----------------------------------------------------------------------------------------------


def test_summary_shows_tables_without_touching_the_state(
    mock_api: respx.MockRouter, store: MemorySessionStore, tmp_path: Path
) -> None:
    mock_everything(mock_api)
    before = write(tmp_path / "ayer.json", previous_snapshot(TODAY - DAY))

    result = run("--wide", "summary", "--previous", str(before))

    assert result.exit_code == 0, result.output
    for title in (
        "Posición a",
        "Variación desde",
        "Cartera",
        "Planes de pensiones",
        "Órdenes en curso",
        "Lo que viene",
    ):
        assert title in result.stdout
    assert "4.100,85 €" in result.stdout and "61,50 €" in result.stdout
    assert "Movimientos nuevos" not in result.stdout
    assert not default_state_path().exists()


def test_summary_from_file_needs_no_session(tmp_path: Path, mock_api: respx.MockRouter) -> None:
    saved = write(tmp_path / "hoy.json", previous_snapshot(TODAY, total=12.5, errors=[]))
    before = write(tmp_path / "antes.json", previous_snapshot(TODAY - DAY, total=10))

    result = run("--wide", "summary", "--from-file", str(saved), "--previous", str(before))

    assert result.exit_code == 0, result.output
    assert "TOTAL" in result.stdout and "12,50 €" in result.stdout and "2,50 €" in result.stdout
    assert not mock_api.calls


def test_summary_from_file_shows_an_upcoming_saved_before_estado_existed(
    tmp_path: Path, mock_api: respx.MockRouter
) -> None:
    row = {
        "fecha": str(TODAY),
        "tipo": "aportacion",
        "producto": "Fondo mensual",
        "importe": -60.0,
        "cuenta_cargo": "Mi cuenta",
        "retenido": False,
        "disponible": 100.0,
        "disponible_tras": 40.0,
        "falta": None,
        "tipo_retencion": None,
    }
    saved = write(tmp_path / "vieja.json", previous_snapshot(TODAY, upcoming=[row], errors=[]))

    result = run("--wide", "summary", "--from-file", str(saved))

    assert result.exit_code == 0, result.output
    assert "Lo que viene" in result.stdout and "Fondo mensual" in result.stdout
