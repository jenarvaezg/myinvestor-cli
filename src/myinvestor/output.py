"""Render rows as a rich table, JSON, CSV or TSV."""

from __future__ import annotations

import csv
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from rich.cells import cell_len
from rich.console import Console
from rich.table import Column, Table
from rich.text import Text

from myinvestor.json_envelope import wrap
from myinvestor.json_output import decimal_literal, dumps

# Column roles. The table formats them the Spanish way; JSON/CSV/TSV keep raw numbers, with
# euro amounts rounded to cents and per-unit prices, participations and percentages untouched.
MONEY_COLUMNS = frozenset(
    {"importe", "saldo", "retenido", "disponible", "valor", "invertido", "ganancia", "vl", "coste_medio", "total"}
    | {"intereses", "dividendos", "bruto", "retenciones", "neto", "comisiones"}
    | {"disponible_tras", "falta"}
    | {"aportar"}
    | {"aportado", "mercado"}
    | {"saldo_base", "saldo_promo", "saldo_total", "intereses_base", "intereses_promo", "retencion"}
    | {"coste", "plusvalia", "resultado", "vl_compra"}
)
PRICE_COLUMNS = frozenset({"vl", "coste_medio", "vl_compra"})
AMOUNT_COLUMNS = MONEY_COLUMNS - PRICE_COLUMNS
PERCENT_COLUMNS = frozenset(
    {"rentab", "rentab_ytd", "ter", "1a", "3a", "5a", "ytd"}
    | {"peso", "peso_pct", "objetivo_pct", "desviacion_pct"}
    | {"tipo_pct", "tipo_base_pct", "tipo_promo_pct", "tae_pct", "tae_promo_pct"}
    | {"tae", "tipo_interes"}
)
QUANTITY_COLUMNS = frozenset({"participaciones", "movimientos"})
NUMERIC_COLUMNS = MONEY_COLUMNS | PERCENT_COLUMNS | QUANTITY_COLUMNS
DATE_COLUMNS = frozenset(
    {"fecha", "valor_fecha", "fecha_vl", "fecha_valor", "proxima", "consultado_en", "periodo"}
    | {"fecha_pago", "fecha_oficial", "fin_promocion", "inicio", "fin"}
    | {"fecha_fiscal", "vencimiento"}
)
MAX_QUANTITY_DECIMALS = 6
CENTS = Decimal("0.01")

# Text columns may wrap down to this width (or their header's); dates and numbers never wrap nor shrink.
TEXT_MIN_WIDTH = 6
# "No limit" for --wide and non-TTY output: tables only take the width their content needs.
UNLIMITED_WIDTH = 10_000
_LINE_BREAKS = re.compile(r"[\t\r\n]+")

# Informative messages on stderr (never stdout); `--quiet` silences them.
notices = Console(stderr=True)


class OutputFormat(StrEnum):
    table = "table"
    json = "json"
    csv = "csv"
    tsv = "tsv"


@dataclass(frozen=True)
class TableSettings:
    wide: bool = False


_settings = TableSettings()


def set_wide(wide: bool) -> None:
    """`--wide`: tables never wrap nor truncate, whatever the terminal width."""
    global _settings
    _settings = TableSettings(wide=wide)


def format_number(value: Decimal | int | float, decimals: int = 2) -> str:
    """Spanish formatting: -1.128,04"""
    text = f"{abs(value):,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{'-' if value < 0 else ''}{text}"


def format_eur(value: Decimal | int | float | None, currency: str = "EUR") -> str:
    """Spanish formatting: -1.128,04 € (other currencies keep their code)."""
    if value is None:
        return ""
    symbol = "€" if currency in ("EUR", "", None) else currency
    return f"{format_number(value)} {symbol}"


def format_pct(value: Decimal | int | float | None) -> str:
    return "" if value is None else f"{format_number(value)} %"


def normalize_amount(value: Decimal | int) -> Decimal:
    """Euros to cents (`-50.0000` → `-50.00`), without a `-0.00`."""
    cents = Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)
    return cents.copy_abs() if cents.is_zero() else cents


def _is_amount(column: str, value: Any) -> bool:
    return column in AMOUNT_COLUMNS and isinstance(value, Decimal | int) and not isinstance(value, bool)


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Copy of `row` with amount columns rounded to cents; everything else as is."""
    return {key: normalize_amount(value) if _is_amount(key, value) else value for key, value in row.items()}


def render(rows: Sequence[dict[str, Any]], fmt: OutputFormat, *, title: str | None = None) -> None:
    if fmt is OutputFormat.json:
        print_json([normalize_row(r) for r in rows])
        return
    if not rows:
        if fmt is OutputFormat.table:  # JSON already printed `[]`; empty CSV/TSV is the result
            notices.print("[dim]Sin resultados[/dim]")
        return
    columns = list(rows[0].keys())
    if fmt is OutputFormat.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: _plain(v) for k, v in normalize_row(r).items()} for r in rows)
        return
    if fmt is OutputFormat.tsv:
        _print_tsv(rows, columns)
        return
    _print_table(rows, columns, title)


def _plain(value: Any) -> Any:
    """Decimals in fixed-point notation (`str` would give `1E-7`)."""
    return decimal_literal(value) if isinstance(value, Decimal) else value


def _tsv_cell(value: Any) -> str:
    return "" if value is None else _LINE_BREAKS.sub(" ", str(_plain(value)))


def _print_tsv(rows: Sequence[dict[str, Any]], columns: list[str]) -> None:
    """One line per row, tab separated, no quoting: tabs and line breaks inside values become spaces."""
    print("\t".join(columns))
    for row in map(normalize_row, rows):
        print("\t".join(_tsv_cell(row.get(c)) for c in columns))


def table_width(*, wide: bool, columns: str | None, is_tty: bool) -> int | None:
    """Console width for tables; None lets rich measure the terminal.

    `--wide` wins, then `COLUMNS`; without a TTY (cron, pipes, agents) there is no terminal to fit,
    so the table is as wide as its content instead of rich's 80-column fallback.
    """
    if wide:
        return UNLIMITED_WIDTH
    if columns and columns.isdigit() and int(columns) > 0:
        return int(columns)
    return None if is_tty else UNLIMITED_WIDTH


def _text_width(text: str) -> int:
    return max((cell_len(line) for line in text.splitlines()), default=0)


def _is_fixed(column: str) -> bool:
    """Dates and numbers: never wrapped nor shrunk, a cut `2026-1…` is useless."""
    return column in NUMERIC_COLUMNS or column in DATE_COLUMNS


def column_widths(columns: list[str], cells: list[list[str]], max_width: int) -> list[int]:
    """Content width of each column so the table fits `max_width` when it can.

    Fixed columns get their full width. Text columns share what is left: narrow ones keep their
    natural width and the widest wrap, never below TEXT_MIN_WIDTH (or their header's width).
    """
    naturals = [_text_width(name) for name in columns]
    for line in cells:
        naturals = [max(width, _text_width(text)) for width, text in zip(naturals, line, strict=True)]
    widths = [
        natural if _is_fixed(name) else min(natural, max(TEXT_MIN_WIDTH, cell_len(name)))
        for name, natural in zip(columns, naturals, strict=True)
    ]
    text = sorted((i for i, name in enumerate(columns) if not _is_fixed(name)), key=lambda i: naturals[i])
    remaining = max_width - _table_chrome(len(columns)) - sum(widths)
    for position, i in enumerate(text):
        grow = max(0, min(naturals[i] - widths[i], remaining // (len(text) - position)))
        widths[i] += grow
        remaining -= grow
    return widths


def _table_chrome(count: int) -> int:
    """Characters a rich table adds around `count` columns: padding (1 + 1) and borders."""
    return 3 * count + 1


def _print_table(rows: Sequence[dict[str, Any]], columns: list[str], title: str | None) -> None:
    cells = [[format_cell(c, row.get(c)) for c in columns] for row in rows]
    console = Console(
        width=table_width(wide=_settings.wide, columns=os.environ.get("COLUMNS"), is_tty=sys.stdout.isatty())
    )
    widths = column_widths(columns, cells, console.width)
    table = Table(
        *(
            Column(name, justify="right" if name in NUMERIC_COLUMNS else "left", width=width, overflow="fold")
            for name, width in zip(columns, widths, strict=True)
        ),
        title=title,
    )
    for line in cells:
        table.add_row(*map(Text, line))
    # rich crops whatever exceeds the console: rather overflow a too-narrow terminal than cut a date.
    console.width = max(console.width, sum(widths) + _table_chrome(len(columns)))
    console.print(table)


def format_cell(column: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal | int | float) and not isinstance(value, bool):
        if column in MONEY_COLUMNS:
            return format_eur(value)
        if column in PERCENT_COLUMNS:
            return format_pct(value)
        if column in QUANTITY_COLUMNS:
            exponent = Decimal(str(value)).normalize().as_tuple().exponent
            decimals = min(max(-exponent, 0), MAX_QUANTITY_DECIMALS) if isinstance(exponent, int) else 2
            return format_number(value, decimals)
    return str(value)


def print_json(data: Any) -> None:
    """JSON on stdout, inside the `--json-envelope` envelope when it is on."""
    print(dumps(wrap(data)))
