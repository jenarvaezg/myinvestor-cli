"""#28: `export DIR`: full first run, incremental runs that only append, manifest with coverage and holes."""

from __future__ import annotations

import csv
import io
import json
import stat
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.cli_common import package_version
from myinvestor.errors import InputError, PartialResultError
from myinvestor.export import (
    DATASET,
    Coverage,
    ExportDir,
    ExportFormat,
    Hole,
    HoleReason,
    Interval,
    check_span,
    merge_intervals,
    next_start,
    subtract,
    update_coverage,
)
from myinvestor.psd2 import free_start
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import CASH_UUID, CPERF, ok, order, securities_account
from tests.test_psd2_window import SECOND_UUID, FakeFlows, mock_api_with, on

runner = CliRunner()
TODAY = date.today()
FREE = free_start(TODAY)
DAY = timedelta(days=1)
EARLIEST = date(2010, 1, 1)
ORDERS = f"{CPERF}/v3/securities-accounts/201/orders"
CONTRIBUTIONS = f"{CPERF}/v2/automatic-contributions/self"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    monkeypatch.setattr("myinvestor.signature.time.sleep", lambda _s: None)
    return memory


class Ledger(FakeFlows):
    """`FakeFlows` plus `startBalance`/`endBalance` that match its movements (or are off by `skew`)."""

    def __init__(self, by_account: dict[str, list[dict[str, Any]]], skew: float = 0.0) -> None:
        super().__init__(by_account)
        self.skew = skew

    def __call__(self, request: httpx.Request, uuid: str) -> httpx.Response:
        response = super().__call__(request, uuid)
        if response.status_code != 200:
            return response
        start, end = request.url.params["dateFrom"], request.url.params["dateTo"]
        days = [(f["operationDate"][:10], f["amount"]) for f in self.by_account.get(uuid, [])]
        opening = 1000.0 + sum(a for d, a in days if d < start)
        body = response.json()
        body["payload"]["data"]["startBalance"] = opening
        body["payload"]["data"]["endBalance"] = opening + sum(a for d, a in days if start <= d <= end) + self.skew
        return httpx.Response(200, json=body)


def ledger(skew: float = 0.0) -> Ledger:
    return Ledger(
        {
            CASH_UUID: [on(FREE - 30 * DAY, 1, -10.0), on(TODAY - DAY, 2, 20.0)],
            SECOND_UUID: [on(FREE - 5 * DAY, 3, -30.0), on(FREE, 4, 40.0)],
        },
        skew=skew,
    )


def contribution(cid: str = "c1") -> dict[str, Any]:
    frame = {"recurrence": "MONTHLY", "nextExecutionDate": "2026-12-01T00:00:00.000Z"}
    return {"contributionId": cid, "amount": 100.0, "status": "ACTIVE", "contributionTimeFrame": frame}


@pytest.fixture
def api(mock_api: respx.MockRouter, store: MemorySessionStore) -> tuple[Ledger, respx.Route, respx.Route]:
    """Two cash accounts, a securities account with one order, a pension account and a contribution."""
    fake = ledger()
    signatures = mock_api_with(mock_api, fake)
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(securities_account()))
    orders = mock_api.get(ORDERS).respond(200, json=ok([order("a", "2026-10-01", status="IN_PROGRESS")]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([{"accountId": "301"}]))
    pension = order("p1", "2026-09-01", securitiesAccountId=None, pensionAccountId="301", dgsCode="N0001", isin=None)
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders").respond(200, json=ok([pension]))
    mock_api.get(CONTRIBUTIONS).respond(200, json=ok([contribution()]))
    return fake, signatures, orders


def export(directory: Path, *args: str, approve: bool = False) -> Any:
    return runner.invoke(app, [*(["--approve"] if approve else []), "export", str(directory), *args])


def csv_rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text())))


def manifest(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "manifest.json").read_text())


# ---- first export ---------------------------------------------------------------------------------


def test_first_export_with_approval_gets_everything_with_one_signature_per_account(
    api: tuple[Ledger, respx.Route, respx.Route], tmp_path: Path
) -> None:
    fake, signatures, _ = api
    out = tmp_path / "hist"

    result = export(out, approve=True)

    assert result.exit_code == 0, result.output
    rows = csv_rows(out / "movements.csv")
    oldest_first = [f"{CASH_UUID}:ref1", f"{SECOND_UUID}:ref3", f"{SECOND_UUID}:ref4", f"{CASH_UUID}:ref2"]
    assert [r["id"] for r in rows] == oldest_first
    assert rows[0]["importe"] == "-10.00" and rows[0]["cuenta_uuid"] == CASH_UUID
    assert (rows[0]["pareja_id"], rows[0]["traspaso_interno"]) == ("", "False")  # columns of `movements`
    assert signatures.call_count == 2
    assert {(u, s) for u, s, _, signed in fake.requests if signed} == {
        (CASH_UUID, EARLIEST.isoformat()),
        (SECOND_UUID, EARLIEST.isoformat()),
    }
    for name in ("movements", "orders", "pension_orders", "contributions", "holdings", "accounts"):
        assert stat.S_IMODE((out / f"{name}.csv").stat().st_mode) == 0o600, name
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert stat.S_IMODE((out / "manifest.json").stat().st_mode) == 0o600
    assert [r["ref"] for r in csv_rows(out / "pension_orders.csv")] == ["p1"]
    holding = csv_rows(out / "holdings.csv")[0]
    assert (holding["isin"], holding["peso"]) == ("IE0000000001", "100.00")
    assert {r["id"] for r in csv_rows(out / "accounts.csv")} == {"101", "102"}

    data = manifest(out)
    assert data["cli_version"] == package_version() and data["format"] == "csv" and data["as_of"]
    assert data["files"]["movements"] == {"fichero": "movements.csv", "filas": 4}
    account = data["accounts"][CASH_UUID]
    assert account["cuenta"] == "Cuenta 1"
    assert account["cubierto"] == [{"desde": EARLIEST.isoformat(), "hasta": TODAY.isoformat()}]
    assert account["huecos"] == []
    assert [t["cuadra"] for t in account["ultimos_tramos"]] == [True, True]


def test_first_export_without_approval_exports_the_free_part_and_records_the_hole(
    api: tuple[Ledger, respx.Route, respx.Route], tmp_path: Path
) -> None:
    fake, signatures, _ = api
    out = tmp_path / "hist"

    result = export(out)

    assert isinstance(result.exception, PartialResultError)
    assert "manifest.json" in str(result.exception)
    assert not signatures.called
    assert sorted(r["id"] for r in csv_rows(out / "movements.csv")) == [f"{CASH_UUID}:ref2", f"{SECOND_UUID}:ref4"]
    assert csv_rows(out / "orders.csv")  # the rest is exported anyway
    account = manifest(out)["accounts"][CASH_UUID]
    assert account["cubierto"] == [{"desde": FREE.isoformat(), "hasta": TODAY.isoformat()}]
    old = {"desde": EARLIEST.isoformat(), "hasta": (FREE - DAY).isoformat(), "motivo": "firma"}
    assert account["huecos"] == [old]

    # Later, with approval: the hole is asked again and filled, without duplicating what was there.
    fake.requests.clear()
    assert export(out, approve=True).exit_code == 0
    assert len(csv_rows(out / "movements.csv")) == 4
    account = manifest(out)["accounts"][CASH_UUID]
    assert account["huecos"] == []
    assert account["cubierto"] == [{"desde": EARLIEST.isoformat(), "hasta": TODAY.isoformat()}]


def test_allow_partial_exits_0(api: tuple[Ledger, respx.Route, respx.Route], tmp_path: Path) -> None:
    result = export(tmp_path / "hist", "--allow-partial")

    assert result.exit_code == 0, result.output
    assert "parcial" in result.stderr.lower()


def test_since_limits_the_first_export(api: tuple[Ledger, respx.Route, respx.Route], tmp_path: Path) -> None:
    fake, signatures, _ = api

    assert export(tmp_path / "hist", "--since", FREE.isoformat()).exit_code == 0
    assert {s for _, s, _, _ in fake.requests} == {FREE.isoformat()}
    assert not signatures.called


# ---- incremental ------------------------------------------------------------------------------------


def test_incremental_export_only_appends_new_rows(api: tuple[Ledger, respx.Route, respx.Route], tmp_path: Path) -> None:
    fake, _, orders = api
    out = tmp_path / "hist"
    assert export(out, approve=True).exit_code == 0
    before = {name: (out / f"{name}.csv").read_text() for name in ("movements", "orders", "holdings", "accounts")}
    fake.requests.clear()
    fake.by_account[CASH_UUID].append(on(TODAY, 5, 1.5))
    orders.respond(200, json=ok([order("a", "2026-10-01"), order("b", "2026-10-02")]))

    result = export(out)

    assert result.exit_code == 0, result.output
    since = (TODAY - 3 * DAY).isoformat()  # last covered day minus the margin: inside the free window
    assert {(u, s) for u, s, _, _ in fake.requests} == {(CASH_UUID, since), (SECOND_UUID, since)}
    movements = (out / "movements.csv").read_text()
    assert movements.startswith(before["movements"])  # old rows untouched
    assert [r["id"] for r in csv_rows(out / "movements.csv")][-1] == f"{CASH_UUID}:ref5"
    assert len(csv_rows(out / "movements.csv")) == 5
    assert [(r["ref"], r["estado"]) for r in csv_rows(out / "orders.csv")] == [
        ("a", "IN_PROGRESS"),
        ("a", "EXECUTED"),
        ("b", "EXECUTED"),
    ]
    assert (out / "holdings.csv").read_text() == before["holdings"]  # same day: same snapshot
    assert (out / "accounts.csv").read_text() == before["accounts"]
    assert "movements.csv: 1 filas nuevas (5 en total)" in result.stderr


def test_json_lines_export_is_incremental_too(api: tuple[Ledger, respx.Route, respx.Route], tmp_path: Path) -> None:
    out = tmp_path / "hist"
    assert export(out, "-f", "json", approve=True).exit_code == 0
    assert export(out, "-f", "json").exit_code == 0

    lines = (out / "movements.jsonl").read_text().splitlines()
    rows = [json.loads(line, parse_float=Decimal) for line in lines]
    assert len(rows) == len({r["id"] for r in rows}) == 4
    assert rows[0]["importe"] == Decimal("-10.00")
    assert len((out / "contributions.jsonl").read_text().splitlines()) == 1
    assert manifest(out)["format"] == "json"

    clash = export(out, "-f", "csv")
    assert isinstance(clash.exception, InputError)


def test_a_span_that_does_not_balance_is_recorded_as_a_hole(
    mock_api: respx.MockRouter, api: tuple[Ledger, respx.Route, respx.Route], tmp_path: Path
) -> None:
    fake = ledger(skew=1.0)
    mock_api.get(url__regex=r".*/account/api/v3/account/(?P<uuid>[^/]+)/flows").mock(side_effect=fake)
    out = tmp_path / "hist"

    result = export(out, "--since", FREE.isoformat())

    assert result.exit_code == 0, result.output
    assert "Descuadre en Cuenta 1" in " ".join(result.stderr.split())
    account = manifest(out)["accounts"][CASH_UUID]
    assert account["huecos"] == [{"desde": FREE.isoformat(), "hasta": TODAY.isoformat(), "motivo": "descuadre"}]
    assert account["cubierto"] == []
    assert account["ultimos_tramos"][0]["cuadra"] is False


# ---- pure parts ------------------------------------------------------------------------------------------


def iv(a: int, b: int) -> Interval:
    return Interval(date(2026, 1, a), date(2026, 1, b))


def test_merge_intervals_joins_overlapping_and_adjacent_days() -> None:
    assert merge_intervals([iv(5, 6), iv(1, 2), iv(3, 4), iv(10, 12), iv(11, 11)]) == (iv(1, 6), iv(10, 12))


def test_subtract_cuts_holes_by_whole_days() -> None:
    hole = Hole(iv(1, 10), HoleReason.signature)
    assert subtract([hole], [iv(3, 4), iv(9, 20)]) == [
        Hole(iv(1, 2), HoleReason.signature),
        Hole(iv(5, 8), HoleReason.signature),
    ]


def test_coverage_and_next_start() -> None:
    first = update_coverage(None, [check_span(iv(20, 31), None, None, [])], [iv(1, 19)])
    assert first == Coverage((iv(20, 31),), (Hole(iv(1, 19), HoleReason.signature),))
    assert first.last_checks[0].ok is None  # no balances: covered, but unchecked
    assert next_start(None, None, EARLIEST) == EARLIEST
    assert next_start(first, None, EARLIEST) == date(2026, 1, 1)  # retry the hole
    healed = update_coverage(first, [check_span(iv(1, 31), Decimal(1), Decimal(3), [Decimal(2)])], [])
    assert healed == Coverage((iv(1, 31),), ())
    assert next_start(healed, None, EARLIEST) == date(2026, 1, 28)
    assert next_start(healed, date(2025, 6, 1), EARLIEST) == date(2025, 6, 1)


def test_appending_to_a_csv_keeps_its_header_and_says_which_columns_it_drops(tmp_path: Path) -> None:
    out = ExportDir(tmp_path, ExportFormat.csv)
    (tmp_path / "movements.csv").write_text("id,importe\nx:1,1.00")  # no trailing newline
    rows = [{"id": "x:1", "importe": Decimal(1)}, {"id": "x:2", "importe": Decimal("2.5"), "pareja_id": "p"}]

    result = out.append(DATASET["movements"], rows)

    assert (result.new, result.total, result.dropped_columns) == (1, 2, ("pareja_id",))
    assert (tmp_path / "movements.csv").read_text() == "id,importe\nx:1,1.00\nx:2,2.50\n"
