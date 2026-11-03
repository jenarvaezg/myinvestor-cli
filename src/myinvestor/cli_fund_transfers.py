"""`fund-transfers`: fund transfers of the securities accounts, or pension-plan transfers (read-only)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

import typer

from myinvestor.cli_common import FormatOpt, app, build_client, parse_day
from myinvestor.cli_invest import _pension_ids, _securities
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError
from myinvestor.models import FundTransfer, PensionTransfer
from myinvestor.output import OutputFormat, render

DEFAULT_FUND_TRANSFER_DAYS = 60  # what the web asks for
# The table joins each code with its name and leaves out `moneda`; JSON and CSV export every field.
FUND_TRANSFER_TABLE_COLUMNS = (
    "fecha",
    "cuenta",
    "origen",
    "destino",
    "participaciones",
    "importe",
    "estado",
    "cancelable",
    "ref",
)

TransferAccountOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--account",
        "-a",
        help="Cuenta de valores (id o código) o, con --pension, de pensiones (id). Repetible; por defecto todas.",
    ),
]


@app.command("fund-transfers")
def fund_transfers(
    account: TransferAccountOpt = None,
    pension: Annotated[
        bool, typer.Option("--pension", help="Traspasos de planes de pensiones en vez de fondos.")
    ] = False,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            "--from",
            help=f"Desde (AAAA-MM-DD); por defecto, {DEFAULT_FUND_TRANSFER_DAYS} días antes de --until.",
        ),
    ] = None,
    until: Annotated[str | None, typer.Option("--until", "--to", help="Hasta (AAAA-MM-DD), por defecto hoy.")] = None,
    pending: Annotated[
        bool,
        typer.Option("--pending", help="Solo cuántos traspasos de fondos hay en curso en cada cuenta de valores."),
    ] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Traspasos de fondos (entre fondos, de y hacia otra entidad) o, con --pension, de planes.

    Por defecto, los últimos 60 días, como la web, de todas las cuentas: una sola petición por
    cuenta con el rango pedido. Los cambios entre fondos dentro de MyInvestor («ALTA IIC SWITCH»
    en los lotes) no salen aquí: son órdenes, en `orders` y `lots`.
    """
    if pending:
        _reject_with_pending(pension=pension, since=since, until=until)
        _pending(account, fmt)
        return
    start, end = _range(since, until)
    with build_client() as client:
        if pension:
            found = [
                _pension_row(i, t)
                for i in _pension_ids(client, account)
                for t in client.pension_transfers(i, start, end)
            ]
        else:
            ids = _securities_ids(client, account)
            found = [_fund_row(i, t) for i in ids for t in client.fund_transfers(i, start, end)]
    found.sort(key=lambda r: (r["fecha"] or "", r["ref"]), reverse=True)
    what = "planes de pensiones" if pension else "fondos"
    if fmt is OutputFormat.table:
        found = [_table_row(r) for r in found]
    render(found, fmt, title=f"Traspasos de {what} {start} → {end}")


def _range(since: str | None, until: str | None) -> tuple[date, date]:
    end = parse_day(until, "--until") or date.today()
    start = parse_day(since, "--since") or end - timedelta(days=DEFAULT_FUND_TRANSFER_DAYS)
    if start > end:
        raise InputError("--since es posterior a --until.")
    return start, end


def _reject_with_pending(*, pension: bool, since: str | None, until: str | None) -> None:
    if pension:
        raise InputError("--pending solo existe para las cuentas de valores: no se combina con --pension.")
    if since or until:
        raise InputError("--pending no usa fechas: quita --since/--until.")


def _securities_ids(client: MyInvestorClient, refs: list[str] | None) -> list[str]:
    """Every active securities account (portfolios included); with `-a`, the ones chosen."""
    return [s.account_id for s in _securities(client, refs)] if refs else client.securities_account_ids()


def _pending(account: list[str] | None, fmt: OutputFormat) -> None:
    with build_client() as client:
        rows = [{"cuenta": i, "pendientes": client.pending_fund_transfers(i)} for i in _securities_ids(client, account)]
    render(rows, fmt, title="Traspasos de fondos en curso")


def _fund_row(account_id: str, t: FundTransfer) -> dict[str, Any]:
    return _row(account_id, t, (t.isin_origin, None), (t.isin_destination, None))


def _pension_row(account_id: str, t: PensionTransfer) -> dict[str, Any]:
    origin = (t.dgs_code_origin, t.fund_name_origin)
    return _row(account_id, t, origin, (t.dgs_code_destination, t.pension_plan_name_destination))


def _row(
    account_id: str,
    t: FundTransfer | PensionTransfer,
    origin: tuple[str | None, str | None],
    destination: tuple[str | None, str | None],
) -> dict[str, Any]:
    return {
        "fecha": t.transfer_date.isoformat() if t.transfer_date else None,
        "cuenta": account_id,
        "origen": origin[0],
        "origen_nombre": origin[1],
        "destino": destination[0],
        "destino_nombre": destination[1],
        "participaciones": t.shares,
        "importe": t.amount,
        "moneda": t.currency,
        "estado": t.status,
        "cancelable": bool(t.is_cancelable),  # like the web: a missing flag is not cancelable
        "ref": t.reference,
    }


def _labelled(code: str | None, name: str | None) -> str | None:
    return " ".join(p for p in (code, name) if p) or None


def _table_row(row: dict[str, Any]) -> dict[str, Any]:
    shown = {
        **row,
        "origen": _labelled(row["origen"], row["origen_nombre"]),
        "destino": _labelled(row["destino"], row["destino_nombre"]),
        "cancelable": "sí" if row["cancelable"] else "no",
    }
    return {c: shown[c] for c in FUND_TRANSFER_TABLE_COLUMNS}
