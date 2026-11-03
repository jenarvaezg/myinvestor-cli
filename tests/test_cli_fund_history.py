"""`fund-history ISIN`: NAV history of any catalog fund (`…/funds/{isin}/graphs/INIT_GRAPH`)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app, main
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import CPERF, ok

runner = CliRunner()
ISIN = "IE0000000001"
GRAPH = f"{CPERF}/v2/funds/{ISIN}/graphs/INIT_GRAPH"
SHEET = f"{CPERF}/v2/funds/{ISIN}"


def nav(day: str, value: float) -> dict[str, Any]:
    # `profit` comes equal to `marketValue` in every point observed: it is not a gain
    return {"positionDate": day, "marketValue": value, "profit": value}


POINTS = [
    nav("2026-09-30", 11.0),
    nav("2026-09-07", 10.0),  # unsorted on purpose
    nav("2026-09-14", 10.5),
    nav("2026-10-01", 11.3),
    nav("2026-10-02", 11.4),
]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


@pytest.fixture
def graph(mock_api: respx.MockRouter) -> respx.Route:
    mock_api.get(SHEET).respond(200, json=ok({"isinCode": ISIN, "currency": {"code": "USD", "symbol": "$"}}))
    return mock_api.get(GRAPH).respond(200, json=ok({"investmentPositionValueList": POINTS}))


def invoke(*args: str) -> Any:
    return runner.invoke(app, ["fund-history", *args], catch_exceptions=False)


def json_rows(*args: str) -> list[dict[str, Any]]:
    result = invoke(*args, "-f", "json")
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout, parse_float=Decimal)
    return rows


def run_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    """Through `main`, so errors become exit codes and messages."""
    monkeypatch.setattr("sys.argv", ["myinvestor", "fund-history", *args])
    with pytest.raises(SystemExit) as exited:
        main()
    return int(exited.value.code or 0), capsys.readouterr().err


@pytest.mark.usefixtures("store")
def test_json_rows_oldest_first_with_date_and_nav(graph: respx.Route) -> None:
    rows = json_rows(ISIN.lower())

    assert graph.call_count == 1
    assert rows == [
        {"fecha": "2026-09-07", "vl": Decimal("10.0"), "moneda": "USD"},
        {"fecha": "2026-09-14", "vl": Decimal("10.5"), "moneda": "USD"},
        {"fecha": "2026-09-30", "vl": Decimal("11.0"), "moneda": "USD"},
        {"fecha": "2026-10-01", "vl": Decimal("11.3"), "moneda": "USD"},
        {"fecha": "2026-10-02", "vl": Decimal("11.4"), "moneda": "USD"},
    ]


@pytest.mark.usefixtures("store", "graph")
def test_since_until_and_every_month() -> None:
    assert [r["fecha"] for r in json_rows(ISIN, "--since", "2026-09-14", "--until", "2026-10-01")] == [
        "2026-09-14",
        "2026-09-30",
        "2026-10-01",
    ]
    assert [r["fecha"] for r in json_rows(ISIN, "--every", "month")] == ["2026-09-30", "2026-10-02"]
    assert [r["fecha"] for r in json_rows(ISIN, "--every", "week")] == [
        "2026-09-07",
        "2026-09-14",
        "2026-10-02",
    ]


@pytest.mark.usefixtures("store", "graph")
def test_table_adds_the_change_against_the_first_point_of_the_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")

    result = invoke(ISIN, "--since", "2026-09-14")

    assert result.exit_code == 0, result.output
    assert "variacion_pct" in result.stdout
    lines = [line for line in result.stdout.splitlines() if "2026-" in line]
    assert "0,00 %" in lines[0] and "10,50 USD" in lines[0]
    assert "8,57 %" in lines[-1]  # 11.4 / 10.5 - 1
    assert "€" not in result.stdout


@pytest.mark.usefixtures("store", "graph")
def test_csv_and_tsv_carry_date_nav_and_currency() -> None:
    csv = invoke(ISIN, "-f", "csv", "--until", "2026-09-07").stdout
    tsv = invoke(ISIN, "-f", "tsv", "--until", "2026-09-07").stdout

    assert csv.splitlines() == ["fecha,vl,moneda", "2026-09-07,10.0,USD"]
    assert tsv.splitlines() == ["fecha\tvl\tmoneda", "2026-09-07\t10.0\tUSD"]


@pytest.mark.usefixtures("store", "graph")
def test_a_range_without_points_is_empty_and_says_what_there_is(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["myinvestor", "fund-history", ISIN, "--since", "2027-01-01", "-f", "json"])
    with pytest.raises(SystemExit) as exited:
        main()
    out, err = capsys.readouterr()

    assert not exited.value.code
    assert json.loads(out) == []
    assert "2026-09-07" in err and "2026-10-02" in err


def test_points_without_nav_are_skipped(store: MemorySessionStore, mock_api: respx.MockRouter) -> None:
    mock_api.get(SHEET).respond(200, json=ok({"currency": "EUR"}))  # a bare code also works
    mock_api.get(GRAPH).respond(
        200, json=ok({"investmentPositionValueList": [nav("2026-09-07", 10.0), {"positionDate": "2026-09-08"}]})
    )

    assert json_rows(ISIN) == [{"fecha": "2026-09-07", "vl": Decimal("10.0"), "moneda": "EUR"}]


@pytest.mark.usefixtures("store")
@pytest.mark.parametrize("sheet", [{"status_code": 404}, {"status_code": 200, "json": ok({"isinCode": ISIN})}])
def test_currency_is_null_when_the_sheet_fails_or_lacks_it(
    sheet: dict[str, Any],
    mock_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_api.get(SHEET).respond(**sheet)
    mock_api.get(GRAPH).respond(200, json=ok({"investmentPositionValueList": [nav("2026-09-07", 10.0)]}))
    monkeypatch.setattr("sys.argv", ["myinvestor", "fund-history", ISIN, "-f", "json"])
    with pytest.raises(SystemExit) as exited:
        main()
    out, err = capsys.readouterr()

    assert not exited.value.code
    assert json.loads(out) == [{"fecha": "2026-09-07", "vl": 10.0, "moneda": None}]
    assert ("moneda" in err) == (sheet["status_code"] == 404)


@pytest.mark.usefixtures("store")
def test_the_sheet_is_not_asked_when_there_is_no_history(mock_api: respx.MockRouter) -> None:
    sheet = mock_api.get(SHEET).respond(200, json=ok({"currency": "EUR"}))
    mock_api.get(GRAPH).respond(200, json=ok({"investmentPositionValueList": []}))

    runner.invoke(app, ["fund-history", ISIN])

    assert sheet.call_count == 0


@pytest.mark.usefixtures("store")
@pytest.mark.parametrize("data", [{"investmentPositionValueList": []}, {}, None])
def test_empty_history_is_a_clear_input_error(
    data: Any, mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_api.get(GRAPH).respond(200, json=ok(data))

    code, err = run_main(monkeypatch, capsys, ISIN)

    assert code == 2
    assert f"no tiene histórico del VL de {ISIN}" in err
    assert "myinvestor funds" in err


@pytest.mark.usefixtures("store")
@pytest.mark.parametrize("status", [400, 404])
def test_unknown_fund_is_a_clear_input_error(
    status: int, mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = {"payload": {"data": None}, "status": {"code": "NOT_FOUND", "message": "Fund not found"}}
    mock_api.get(GRAPH).respond(status, json=body)

    code, err = run_main(monkeypatch, capsys, ISIN, "-f", "json")

    line = json.loads(err)
    assert (code, line["exit_code"], line["error"]) == (2, 2, "input")
    assert f"no tiene histórico del VL de {ISIN}" in line["message"]
    assert f"HTTP {status}" in line["message"]


@pytest.mark.usefixtures("store")
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("IE00321266",), "no parece un ISIN"),
        (("IE003212664X",), "no parece un ISIN"),
        (("N1234",), "history N1234"),
        ((ISIN, "--since", "2026-10-02", "--until", "2026-10-01"), "--since es posterior a --until"),
        ((ISIN, "--since", "02/10/2026"), "fecha no válida"),
    ],
)
def test_bad_input_fails_before_any_request(
    args: tuple[str, ...],
    message: str,
    mock_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, err = run_main(monkeypatch, capsys, *args)

    assert code == 2
    assert message in err
    assert not mock_api.calls


@pytest.mark.usefixtures("store")
def test_every_rejects_unknown_values(graph: respx.Route) -> None:
    result = runner.invoke(app, ["fund-history", ISIN, "--every", "year"])

    assert result.exit_code == 2
    assert graph.call_count == 0
