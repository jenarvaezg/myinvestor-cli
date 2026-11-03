"""`internal-transfers`: both legs of each transfer between the user's own cash accounts."""

from __future__ import annotations

from typing import Annotated

import typer

from myinvestor.cli_accounts import (
    DEFAULT_MOVEMENT_DAYS,
    AccountOpt,
    AllowPartialOpt,
    FreeOnlyOpt,
    _clip_to_free_window,
    _movement_rows,
    _range,
    _report_gaps,
    match_cash_accounts,
)
from myinvestor.cli_common import FormatOpt, app, build_client
from myinvestor.output import OutputFormat, render
from myinvestor.transfers_match import transfer_rows

# The table leaves out the movement ids and the key; JSON and CSV export every field.
TRANSFER_TABLE_COLUMNS = ("fecha", "importe", "desde_cuenta", "hacia_cuenta", "concepto", "match")


@app.command("internal-transfers")
def internal_transfers(
    account: AccountOpt = None,
    date_from: Annotated[str | None, typer.Option("--from", help="Desde (AAAA-MM-DD).")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Hasta (AAAA-MM-DD), por defecto hoy.")] = None,
    days: Annotated[
        int | None, typer.Option(min=1, help=f"Últimos N días (por defecto {DEFAULT_MOVEMENT_DAYS}).")
    ] = None,
    unmatched: Annotated[
        bool,
        typer.Option(
            "--unmatched",
            help="Añadir las transferencias sin pareja en tus cuentas (match vacío): entradas o salidas "
            "externas o de otros clientes de MyInvestor.",
        ),
    ] = False,
    free_only: FreeOnlyOpt = False,
    allow_partial: AllowPartialOpt = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Traspasos entre tus cuentas de efectivo: cada cargo con su abono.

    Empareja por la clave de `operationRef` (match `key`) y, si no se puede leer, por fecha ±1 día,
    importe opuesto y «Transferencia desde MyInvestor» (match `heuristic`). Siempre lee todas las
    cuentas; --account solo filtra los traspasos que tocan esa cuenta. Más allá de ~90 días, como en
    `movements`: si falta algún tramo que pide firma, se muestra lo obtenido y se sale con 13.
    """
    start, end = _range(date_from, date_to, days)
    if free_only:
        start = _clip_to_free_window(start, end)
    with build_client() as client:
        every = client.cash_accounts()
        wanted = {a.account_uuid for a in match_cash_accounts(every, account)}
        movements, gaps = _movement_rows(client, every, start, end)
    pairs, lonely = transfer_rows(movements)
    account_of = {r["id"]: r["cuenta_uuid"] for r in movements}
    rows = [
        r
        for r in [*pairs, *(lonely if unmatched else [])]
        if wanted & {account_of.get(r["desde_id"]), account_of.get(r["hacia_id"])}
    ]
    rows.sort(key=lambda r: (r["fecha"], r["desde_id"] or "", r["hacia_id"] or ""), reverse=True)
    if fmt is OutputFormat.table:
        rows = [{c: r[c] for c in TRANSFER_TABLE_COLUMNS} for r in rows]
    render(rows, fmt, title=f"Traspasos internos {start} → {end}")
    _report_gaps(gaps, allow_partial=allow_partial)
