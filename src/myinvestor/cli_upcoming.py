"""`upcoming`: contributions due, scheduled transfers, orders in progress and holds, against the balance."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

import typer
from rich.markup import escape

from myinvestor.cli_accounts import OwnerOpt, filter_owner
from myinvestor.cli_common import FormatOpt, app, build_client, err, now_local
from myinvestor.client import MyInvestorClient
from myinvestor.models import Order
from myinvestor.output import OutputFormat, format_eur, render
from myinvestor.sent_transfers import transfer_items
from myinvestor.upcoming import Shortfall, scheduled_only, shortfalls, upcoming_rows

DEFAULT_UPCOMING_DAYS = 14
MAX_UPCOMING_DAYS = 366
# The table leaves out the hold `type`; JSON and CSV export every field.
UPCOMING_TABLE_COLUMNS = (
    "fecha",
    "tipo",
    "estado",
    "producto",
    "importe",
    "cuenta_cargo",
    "retenido",
    "disponible",
    "disponible_tras",
    "falta",
)


def _orders(client: MyInvestorClient) -> list[tuple[Order, str | None]]:
    """Recent orders of every securities and pension account, with the cash account each one charges.

    Without dates the securities endpoint returns the recent and in-progress orders only.
    """
    securities = [(o, s.cash_account_id) for s in client.securities_accounts() for o in client.orders(s.account_id)]
    pensions = [(o, p.cash_account_id) for p in client.pension_accounts() for o in client.pension_orders(p.account_id)]
    return securities + pensions


def _table_row(row: dict[str, Any]) -> dict[str, Any]:
    """`get`: a snapshot saved before a key existed (`estado`) still shows."""
    return {c: ("sí" if row.get(c) else "") if c == "retenido" else row.get(c) for c in UPCOMING_TABLE_COLUMNS}


def _warn(shortfall: Shortfall) -> None:
    err.print(
        f"[yellow]Aviso:[/yellow] el disponible de «{escape(shortfall.account)}» "
        f"({format_eur(shortfall.available)}) no cubre lo previsto: desde el {shortfall.since} "
        f"faltan hasta {format_eur(shortfall.missing)}."
    )


@app.command()
def upcoming(
    days: Annotated[
        int,
        typer.Option(
            min=0, max=MAX_UPCOMING_DAYS, help="Días por delante, contando hoy (aportaciones y transferencias)."
        ),
    ] = DEFAULT_UPCOMING_DAYS,
    owner: OwnerOpt = None,
    future_only: Annotated[
        bool,
        typer.Option(
            "--future-only", help="Solo lo programado (aportaciones y transferencias): sin órdenes ni retenciones."
        ),
    ] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Lo que viene: aportaciones y transferencias programadas, órdenes en curso y retenciones, contra el disponible.

    `disponible_tras` resta solo lo que aún no se ha cargado (aportaciones y transferencias programadas): las
    retenciones y las órdenes
    en curso ya están descontadas del disponible. Una orden con su retención sale en una sola fila
    (`retenido`). `estado` dice si aún no ha empezado (`programada`) o ya está en curso (`en_curso`);
    --future-only deja solo lo programado. `falta` dice cuánto no cubre la cuenta; también se avisa
    en stderr.
    """
    today = now_local().date()
    with build_client() as client:
        accounts = client.cash_accounts()
        mine = filter_owner(accounts, owner)
        holds = {a.account_id: client.holds(a.account_uuid) for a in mine}
        sent = {a.account_id: client.transfers(a.account_id) for a in mine}
        contributions = client.contributions()
        orders = _orders(client)
    rows = upcoming_rows(
        today=today,
        days=days,
        accounts=accounts,
        contributions=contributions,
        orders=orders,
        holds=holds,
        transfers=transfer_items(sent, today, today + timedelta(days=days)),
        owner=owner,
    )
    listed = scheduled_only(rows) if future_only else rows
    shown = [_table_row(r) for r in listed] if fmt is OutputFormat.table else listed
    render(shown, fmt, title=f"Próximos {days} días")
    for shortfall in shortfalls(rows):
        _warn(shortfall)
