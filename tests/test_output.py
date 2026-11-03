from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from myinvestor import output
from myinvestor.json_output import dumps
from myinvestor.output import OutputFormat, column_widths, normalize_row, render, table_width

MOVEMENT = {
    "fecha": "2026-10-11",
    "valor_fecha": "2026-10-12",
    "cuenta": "Cuenta 1",
    "tipo": "TRANSFERENCIA SEPA",
    "concepto": "Transferencia recibida con un concepto bastante largo que no cabe",
    "importe": Decimal("-1234.5000"),
    "saldo": Decimal("98765.4300"),
    "ref": "ref1",
}


@pytest.fixture(autouse=True)
def _narrow_mode() -> Iterator[None]:
    output.set_wide(False)
    yield
    output.set_wide(False)


def test_dumps_matches_stdlib_layout_without_decimals() -> None:
    data = {"a": [1, 2.5, None, True, False, 'ñ "x"'], "b": {}, "c": [], "d": {"e": [{"f": "g"}]}}
    assert dumps(data) == json.dumps(data, indent=2, ensure_ascii=False)


def test_dumps_writes_decimal_as_exact_number() -> None:
    text = dumps({"importe": Decimal("-50.00"), "vl": Decimal("12.3456789012345678901"), "big": Decimal("1E+3")})
    assert '"importe": -50.00' in text
    assert '"big": 1000' in text
    assert json.loads(text, parse_float=Decimal)["vl"] == Decimal("12.3456789012345678901")


def test_dumps_stringifies_unknown_types_and_rejects_non_finite() -> None:
    assert dumps([date(2026, 11, 3)]) == '[\n  "2026-11-03"\n]'
    with pytest.raises(ValueError, match="NaN"):
        dumps(Decimal("NaN"))


def test_normalize_row_rounds_amounts_to_cents_only() -> None:
    row = {
        "importe": Decimal("-50.0000"),
        "saldo": Decimal("0.005"),
        "ganancia": Decimal("-0.0000"),
        "vl": Decimal("89.153421"),
        "coste_medio": Decimal("81.0001"),
        "participaciones": Decimal("12.345678"),
        "rentab": Decimal("10.061"),
        "total": 3,
        "concepto": "x",
        "retenido": None,
    }
    assert normalize_row(row) == {
        **row,
        "importe": Decimal("-50.00"),
        "saldo": Decimal("0.01"),
        "ganancia": Decimal("0.00"),
        "total": Decimal("3.00"),
    }
    assert str(normalize_row(row)["ganancia"]) == "0.00"


def test_render_json_numbers(capsys: pytest.CaptureFixture[str]) -> None:
    render([MOVEMENT], OutputFormat.json)
    out = capsys.readouterr().out
    assert '"importe": -1234.50' in out
    row = json.loads(out)[0]
    assert isinstance(row["importe"], float) and isinstance(row["saldo"], float)


def test_render_csv_normalizes_amounts(capsys: pytest.CaptureFixture[str]) -> None:
    render([MOVEMENT], OutputFormat.csv)
    assert capsys.readouterr().out.splitlines()[1].endswith(",-1234.50,98765.43,ref1")


def test_render_tsv_one_line_per_row(capsys: pytest.CaptureFixture[str]) -> None:
    render([{**MOVEMENT, "concepto": "con\ttab\ny salto", "ref": None}], OutputFormat.tsv)
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "fecha\tvalor_fecha\tcuenta\ttipo\tconcepto\timporte\tsaldo\tref",
        "2026-10-11\t2026-10-12\tCuenta 1\tTRANSFERENCIA SEPA\tcon tab y salto\t-1234.50\t98765.43\t",
    ]


@pytest.mark.parametrize(
    ("wide", "columns", "is_tty", "expected"),
    [
        (True, "60", True, output.UNLIMITED_WIDTH),
        (False, "60", True, 60),
        (False, "60", False, 60),
        (False, None, True, None),
        (False, None, False, output.UNLIMITED_WIDTH),
        (False, "", False, output.UNLIMITED_WIDTH),
        (False, "0", True, None),
        (False, "abc", True, None),
    ],
)
def test_table_width(wide: bool, columns: str | None, is_tty: bool, expected: int | None) -> None:
    assert table_width(wide=wide, columns=columns, is_tty=is_tty) == expected


def _table(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], columns: str, **row: Any) -> str:
    monkeypatch.setenv("COLUMNS", columns)
    render([{**MOVEMENT, **row}], OutputFormat.table, title="Movimientos")
    return capsys.readouterr().out


def test_narrow_table_never_cuts_dates_or_amounts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _table(monkeypatch, capsys, "60")
    for value in ("2026-10-11", "2026-10-12", "-1.234,50 €", "98.765,43 €"):
        assert value in out
    assert "…" not in out
    # Headers stay whole and the concept wraps instead of being truncated.
    words = " ".join(out.replace("│", " ").replace("┃", " ").split())
    assert all(word in words for word in ("concepto", "cuenta", "largo", "que", "cabe"))


def test_table_keeps_cells_whole_even_below_their_width(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _table(monkeypatch, capsys, "30")
    assert "2026-10-11" in out and "-1.234,50 €" in out and "…" not in out
    borders = [line for line in out.splitlines() if line[:1] in "┏┃┡│└"]
    assert all(line[-1] in "┓┃┩│┘" for line in borders)  # wider than the terminal, but not cropped


def test_column_widths_share_what_is_left_among_text_columns() -> None:
    columns = ["fecha", "tipo", "concepto", "importe"]
    cells = [["2026-10-11", "SEPA", "x" * 50, "-1,00 €"]]
    # chrome 13 + fixed 10 + 7 -> 30 left: "tipo" keeps its 4, the concept takes the other 26.
    assert column_widths(columns, cells, 60) == [10, 4, 26, 7]
    assert column_widths(columns, cells, 1000) == [10, 4, 50, 7]
    assert column_widths(columns, cells, 20) == [10, 4, 8, 7]


def test_wide_table_one_line_per_row(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    output.set_wide(True)
    out = _table(monkeypatch, capsys, "60")
    assert MOVEMENT["concepto"] in out
    assert len([line for line in out.splitlines() if line.startswith("│")]) == 1  # a single data line


def test_table_without_tty_is_not_limited_to_80(capsys: pytest.CaptureFixture[str]) -> None:
    render([MOVEMENT], OutputFormat.table)
    assert MOVEMENT["concepto"] in capsys.readouterr().out


def test_table_formats_money_and_prices_in_the_row_currency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        {"isin": "US0000000001", "vl": Decimal("29.88"), "valor": Decimal("100"), "moneda": "USD"},
        {"isin": "IE0000000001", "vl": Decimal("10.5"), "valor": Decimal("50"), "moneda": "EUR"},
        {"isin": "IE0000000002", "vl": Decimal("7.25"), "valor": Decimal("20"), "moneda": None},
    ]
    monkeypatch.setenv("COLUMNS", "200")

    render(rows, OutputFormat.table)

    lines = capsys.readouterr().out.splitlines()
    usd = next(line for line in lines if "US0000000001" in line)
    assert "29,88 USD" in usd and "100,00 USD" in usd and "€" not in usd
    assert "10,50 €" in next(line for line in lines if "IE0000000001" in line)
    assert "7,25 €" in next(line for line in lines if "IE0000000002" in line)


def test_table_without_currency_column_is_in_euros(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")

    render([{"vl": Decimal("29.88"), "rentab": Decimal("1.5")}], OutputFormat.table)

    out = capsys.readouterr().out
    assert "29,88 €" in out and "1,50 %" in out
