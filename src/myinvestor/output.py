"""Render rows as a rich table, JSON or CSV."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

# Column roles for the table view; JSON and CSV keep raw values (Decimal as plain strings).
MONEY_COLUMNS = frozenset(
    {"importe", "saldo", "retenido", "disponible", "valor", "invertido", "ganancia", "vl", "coste_medio", "total"}
)
PERCENT_COLUMNS = frozenset({"rentab", "rentab_ytd", "ter", "1a", "3a", "5a", "ytd"})
QUANTITY_COLUMNS = frozenset({"participaciones"})
MAX_QUANTITY_DECIMALS = 6


class OutputFormat(StrEnum):
    table = "table"
    json = "json"
    csv = "csv"


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


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def render(rows: Sequence[dict[str, Any]], fmt: OutputFormat, *, title: str | None = None) -> None:
    if fmt is OutputFormat.json:
        print(json.dumps(list(rows), indent=2, ensure_ascii=False, default=_json_default))
        return
    if not rows:
        Console(stderr=True).print("[dim]Sin resultados[/dim]")
        return
    columns = list(rows[0].keys())
    if fmt is OutputFormat.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return
    table = Table(title=title, show_lines=False)
    numeric = MONEY_COLUMNS | PERCENT_COLUMNS | QUANTITY_COLUMNS
    for col in columns:
        table.add_column(col, justify="right" if col in numeric else "left")
    for row in rows:
        table.add_row(*(Text(format_cell(c, row.get(c))) for c in columns))
    Console().print(table)


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
    print(json.dumps(data, indent=2, ensure_ascii=False, default=_json_default))
