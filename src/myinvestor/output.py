"""Render rows as a rich table, JSON, CSV or TSV."""

from __future__ import annotations

import csv
import os
import re
import sys
from collections.abc import Callable, Sequence
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
    | {"efectivo"}
    | {"saldo_base", "saldo_promo", "saldo_total", "intereses_base", "intereses_promo", "retencion"}
    | {"coste", "plusvalia", "resultado", "vl_compra"}
)
PRICE_COLUMNS = frozenset({"vl", "coste_medio", "vl_compra"})
AMOUNT_COLUMNS = MONEY_COLUMNS - PRICE_COLUMNS
PERCENT_COLUMNS = frozenset(
    {"rentab", "rentab_ytd", "ter", "1a", "3a", "5a", "ytd"}
    | {"peso", "peso_pct", "objetivo_pct", "desviacion_pct", "real_pct", "renta_fija_pct", "renta_variable_pct"}
    | {"tipo_pct", "tipo_base_pct", "tipo_promo_pct", "tae_pct", "tae_promo_pct"}
    | {"variacion_pct"}
)
QUANTITY_COLUMNS = frozenset({"participaciones", "movimientos"})
NUMERIC_COLUMNS = MONEY_COLUMNS | PERCENT_COLUMNS | QUANTITY_COLUMNS
DATE_COLUMNS = frozenset(
    {"fecha", "valor_fecha", "fecha_vl", "fecha_valor", "proxima", "consultado_en", "periodo"}
    | {"fecha_pago", "fecha_oficial", "fin_promocion", "inicio", "fin"}
    | {"fecha_fiscal", "vencimiento"}
    | {"rentab_desde", "rentab_hasta"}
)
MAX_QUANTITY_DECIMALS = 6
# A row with this key is in that currency: its money and price columns are formatted with it.
CURRENCY_COLUMN = "moneda"
CENTS = Decimal("0.01")

# Long free text: these columns give up width first. Every other text column is a short label
# (account alias, type, status, ISIN, ref…), which only gives up width after them. Text wraps at
# spaces and never in the middle of a word: a word that does not fit ends in "…".
FLEX_COLUMNS = frozenset(
    {"concepto", "descripcion", "detalle", "titulo", "nombre", "name", "producto", "fondo", "gestora"}
    | {"categoria", "beneficiario", "contraparte", "origen", "destino", "regla"}
    | {"message", "mensaje", "error", "last_relogin_error"}
)
# Text shrinks to this width (or its header's) before cutting headers; dates and numbers never shrink.
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


def _word_width(text: str) -> int:
    return max((cell_len(word) for word in text.split()), default=0)


def _soft_floor(column: str, longest_word: int) -> int:
    return max(TEXT_MIN_WIDTH, cell_len(column))


def _word_floor(column: str, longest_word: int) -> int:
    return max(_soft_floor(column, longest_word), longest_word)


def _header_floor(column: str, longest_word: int) -> int:
    return cell_len(column)


def _last_floor(column: str, longest_word: int) -> int:
    return 1


_LONG_TEXT, _LABELS = frozenset({True}), frozenset({False})
# Who gives up width, in order, and down to what. Long text to TEXT_MIN_WIDTH; labels first to their
# longest word (they only wrap at spaces), then to TEXT_MIN_WIDTH (a word that does not fit ends in
# "…"); then headers start being cut, long text first; last, any text column down to one character.
_SHRINK_STAGES = (
    (_LONG_TEXT, _soft_floor),
    (_LABELS, _word_floor),
    (_LABELS, _soft_floor),
    (_LONG_TEXT, _header_floor),
    (_LABELS, _header_floor),
    (_LONG_TEXT | _LABELS, _last_floor),
)


def _natural_widths(
    columns: list[str], cells: list[list[str]], measure: Callable[[str], int] = _text_width
) -> list[int]:
    """Widest line (or, with `measure=_word_width`, widest word) of each column, header included."""
    widths = [measure(name) for name in columns]
    for line in cells:
        widths = [max(width, measure(text)) for width, text in zip(widths, line, strict=True)]
    return widths


def _shrink(widths: list[int], floors: dict[int, int], excess: int) -> tuple[list[int], int]:
    """Take up to `excess` characters from the widest of the `floors` columns, one at a time."""
    widths = list(widths)
    while excess > 0:
        above = [i for i, floor in floors.items() if widths[i] > floor]
        if not above:
            break
        widest = max(above, key=lambda i: widths[i])
        widths[widest] -= 1
        excess -= 1
    return widths, excess


def column_widths(columns: list[str], cells: list[list[str]], max_width: int) -> list[int]:
    """Content width of each column so the table never exceeds `max_width` when it can.

    Every column starts at its natural width and text columns give it up in `_SHRINK_STAGES` order,
    widest first within each stage. Fixed columns (dates, numbers) never shrink: when even they do
    not fit, the text keeps TEXT_MIN_WIDTH (or its header) and the table overflows (#21).
    """
    naturals = _natural_widths(columns, cells)
    words = _natural_widths(columns, cells, _word_width)
    text = [i for i, name in enumerate(columns) if not _is_fixed(name)]
    fixed = sum(width for i, width in enumerate(naturals) if i not in text)
    available = max_width - _table_chrome(len(columns)) - fixed
    if available < len(text):
        return [min(width, _soft_floor(columns[i], 0)) if i in text else width for i, width in enumerate(naturals)]
    widths, excess = naturals, sum(naturals[i] for i in text) - available
    for kinds, floor in _SHRINK_STAGES:
        group = {
            i: min(naturals[i], floor(columns[i], words[i])) for i in text if (columns[i] in FLEX_COLUMNS) in kinds
        }
        widths, excess = _shrink(widths, group, excess)
    return widths


def _console() -> Console:
    return Console(
        width=table_width(wide=_settings.wide, columns=os.environ.get("COLUMNS"), is_tty=sys.stdout.isatty())
    )


def _cells(rows: Sequence[dict[str, Any]], columns: list[str]) -> list[list[str]]:
    """Formatted cells; money and prices in the row's `moneda` when it has one."""
    return [[format_cell(c, row.get(c), row.get(CURRENCY_COLUMN)) for c in columns] for row in rows]


def table_fits(rows: Sequence[dict[str, Any]]) -> bool:
    """Whether `rows` fit the table width without wrapping nor shrinking any column."""
    if not rows:
        return True
    columns = list(rows[0].keys())
    cells = _cells(rows, columns)
    return sum(_natural_widths(columns, cells)) + _table_chrome(len(columns)) <= _console().width


def _table_chrome(count: int) -> int:
    """Characters a rich table adds around `count` columns: padding (1 + 1) and borders."""
    return 3 * count + 1


def _print_table(rows: Sequence[dict[str, Any]], columns: list[str], title: str | None) -> None:
    cells = _cells(rows, columns)
    console = _console()
    widths = column_widths(columns, cells, console.width)
    table = Table(
        *(
            Column(name, justify="right" if name in NUMERIC_COLUMNS else "left", width=width, overflow="ellipsis")
            for name, width in zip(columns, widths, strict=True)
        ),
        title=title,
    )
    for line in cells:
        table.add_row(*map(Text, line))
    # rich crops whatever exceeds the console: rather overflow a too-narrow terminal than cut a date.
    console.width = max(console.width, sum(widths) + _table_chrome(len(columns)))
    console.print(table)


def format_cell(column: str, value: Any, currency: str | None = None) -> str:
    """`currency` (the row's `moneda`) applies to money and price columns; without it, euros."""
    if value is None:
        return ""
    if isinstance(value, Decimal | int | float) and not isinstance(value, bool):
        if column in MONEY_COLUMNS:
            return format_eur(value, currency or "EUR")
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
