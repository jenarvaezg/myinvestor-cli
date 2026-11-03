"""`income`: interest, dividends, withholding and fees per month, year, type or account."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

import typer
from rich.markup import escape

from myinvestor.classify import INCOME_CATEGORIES, Category, Classification, classify
from myinvestor.cli_accounts import (
    AccountOpt,
    AllowPartialOpt,
    FreeOnlyOpt,
    OwnerOpt,
    _clip_to_free_window,
    _movement_rows,
    _range,
    _report_gaps,
    filter_owner,
    match_cash_accounts,
)
from myinvestor.cli_common import FormatOpt, app, build_client, err
from myinvestor.config import FREE_FLOWS_DAYS
from myinvestor.errors import InputError
from myinvestor.output import OutputFormat, render
from myinvestor.transfers_match import with_transfer_fields

TOTAL = "total"
CATEGORY_ORDER = {c: i for i, c in enumerate(Category)}


class IncomeGrouping(StrEnum):
    month = "month"
    year = "year"
    type = "type"
    account = "account"


Movement = tuple[dict[str, Any], Classification]


def summarize(movements: Iterable[Movement]) -> dict[str, Any]:
    """Income totals: `bruto` = interest + dividends as credited, `neto` = `bruto` + withholding (negative)."""
    sums: defaultdict[Category, Decimal] = defaultdict(Decimal)
    count = 0
    for row, kind in movements:
        if kind.category in INCOME_CATEGORIES:
            sums[kind.category] += row["importe"]
            count += 1
    gross = sums[Category.intereses] + sums[Category.dividendos]
    return {
        "intereses": sums[Category.intereses],
        "dividendos": sums[Category.dividendos],
        "bruto": gross,
        "retenciones": sums[Category.retencion],
        "neto": gross + sums[Category.retencion],
        "comisiones": sums[Category.comision],
        "movimientos": count,
    }


def _months(start: date, end: date) -> list[str]:
    keys: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def by_period(movements: list[Movement], start: date, end: date, *, yearly: bool) -> list[dict[str, Any]]:
    """One row per month (or year) of the range, empty ones included, plus the total."""
    width = 4 if yearly else 7
    keys = sorted({k[:4] for k in _months(start, end)}) if yearly else _months(start, end)
    groups: defaultdict[str, list[Movement]] = defaultdict(list)
    for movement in movements:
        groups[movement[0]["fecha"][:width]].append(movement)
    rows = [{"periodo": key, **summarize(groups[key])} for key in keys]
    return [*rows, {"periodo": TOTAL, **summarize(movements)}]


def by_account(movements: list[Movement]) -> list[dict[str, Any]]:
    groups: dict[str, list[Movement]] = defaultdict(list)
    names: dict[str, str] = {}
    for movement in movements:
        groups[movement[0]["cuenta_id"]].append(movement)
        names[movement[0]["cuenta_id"]] = movement[0]["cuenta"]
    rows = [{"cuenta": names[i], "cuenta_id": i, **summarize(groups[i])} for i in groups]
    return [*rows, {"cuenta": TOTAL, "cuenta_id": None, **summarize(movements)}]


def by_type(movements: list[Movement]) -> list[dict[str, Any]]:
    """One row per category and `operationType`, with the rule that classified it (`null`: unknown)."""
    groups: dict[tuple[Category, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row, kind in movements:
        groups[(kind.category, row["tipo"], kind.rule)].append(row)
    ordered = sorted(groups.items(), key=lambda item: (CATEGORY_ORDER[item[0][0]], item[0][1] or ""))
    rows = [
        {"categoria": category.value, "tipo": kind, "regla": rule, "movimientos": len(rs), "importe": _total(rs)}
        for (category, kind, rule), rs in ordered
    ]
    every = [row for row, _ in movements]
    total = {"categoria": TOTAL, "tipo": None, "regla": None, "movimientos": len(every), "importe": _total(every)}
    return [*rows, total]


def _total(rows: list[dict[str, Any]]) -> Decimal:
    return sum((r["importe"] for r in rows), Decimal(0))


def _year_range(year: int) -> tuple[date, date]:
    today = date.today()
    if year > today.year:
        raise InputError(f"--year {year} es un año futuro.")
    return date(year, 1, 1), min(date(year, 12, 31), today)


def _income_range(year: int | None, date_from: str | None, date_to: str | None, days: int | None) -> tuple[date, date]:
    if year is not None:
        if date_from or date_to or days:
            raise InputError("Usa --year o --from/--to/--days, no ambos.")
        return _year_range(year)
    # by default the free window: the last FREE_FLOWS_DAYS days, today included
    return _range(date_from, date_to, days if days or date_from else FREE_FLOWS_DAYS - 1)


def _report_unknown(movements: list[Movement]) -> None:
    unknown = Counter(row["tipo"] or "(sin tipo)" for row, kind in movements if not kind.known)
    if unknown:
        kinds = ", ".join(f"«{escape(t)}» ({n})" for t, n in unknown.most_common())
        err.print(f"[dim]Sin clasificar (en `otros`): {kinds}. Detalle con --by type.[/dim]")


GROUPINGS: dict[IncomeGrouping, Callable[[list[Movement], date, date], list[dict[str, Any]]]] = {
    IncomeGrouping.month: lambda ms, start, end: by_period(ms, start, end, yearly=False),
    IncomeGrouping.year: lambda ms, start, end: by_period(ms, start, end, yearly=True),
    IncomeGrouping.type: lambda ms, _start, _end: by_type(ms),
    IncomeGrouping.account: lambda ms, _start, _end: by_account(ms),
}


@app.command()
def income(
    year: Annotated[int | None, typer.Option(min=2000, help="Año natural entero (hasta hoy si es el actual).")] = None,
    date_from: Annotated[str | None, typer.Option("--from", help="Desde (AAAA-MM-DD).")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Hasta (AAAA-MM-DD), por defecto hoy.")] = None,
    days: Annotated[
        int | None,
        typer.Option(min=1, help=f"Últimos N días (por defecto, los {FREE_FLOWS_DAYS} que no piden firma)."),
    ] = None,
    by: Annotated[
        IncomeGrouping, typer.Option("--by", help="Agrupar por mes, año, tipo de movimiento o cuenta.")
    ] = IncomeGrouping.month,
    account: AccountOpt = None,
    owner: OwnerOpt = None,
    free_only: FreeOnlyOpt = False,
    allow_partial: AllowPartialOpt = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Intereses, dividendos, retenciones y comisiones de las cuentas de efectivo, a partir de los movimientos.

    `bruto` = intereses + dividendos tal como se abonan; `neto` = bruto + retenciones; `comisiones`
    aparte. Los movimientos se clasifican por `operationType` (ver `--by type`); los desconocidos
    van a `otros`; los dos lados de un traspaso entre tus cuentas, a `traspaso_interno`. Los
    movimientos se piden como en `movements`: lo anterior a los últimos ~90 días hay que aprobarlo
    en la app del móvil, y si falta algún tramo se muestra lo obtenido y se sale con 13 (0 con
    --allow-partial).
    """
    start, end = _income_range(year, date_from, date_to, days)
    if free_only:
        start = _clip_to_free_window(start, end)
    with build_client() as client:
        chosen = filter_owner(match_cash_accounts(client.cash_accounts(), account), owner)
        rows, gaps = _movement_rows(client, chosen, start, end)
    movements = [
        (row, classify(row["tipo"], row["clase"], internal_transfer=row["traspaso_interno"]))
        for row in with_transfer_fields(rows)
    ]
    if by is not IncomeGrouping.type:
        _report_unknown(movements)
    render(GROUPINGS[by](movements, start, end), fmt, title=f"Ingresos {start} → {end}")
    _report_gaps(gaps, allow_partial=allow_partial)
