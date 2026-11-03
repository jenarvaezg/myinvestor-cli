"""Cash-account commands: `position`, `accounts`, `movements`, `holds`, `balance-history`."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

import typer

from myinvestor.cli_common import FormatOpt, TerminalSignatureUI, app, build_client, err, parse_day
from myinvestor.client import MyInvestorClient
from myinvestor.config import FREE_FLOWS_DAYS
from myinvestor.errors import InputError
from myinvestor.models import CashAccount
from myinvestor.output import OutputFormat, render

DEFAULT_MOVEMENT_DAYS = 30

AccountOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--account",
        "-a",
        help="Cuenta: id, uuid, alias o últimos dígitos del IBAN. Repetible; por defecto todas.",
    ),
]


def match_cash_accounts(accounts: list[CashAccount], refs: list[str] | None) -> list[CashAccount]:
    if not refs:
        return accounts
    chosen: list[CashAccount] = []
    for ref in refs:
        needle = ref.strip().lower().replace(" ", "")
        hits = [
            a
            for a in accounts
            if needle in (a.account_id.lower(), a.account_uuid.lower(), (a.alias or "").lower().replace(" ", ""))
            or (len(needle) >= 4 and (a.iban or "").lower().endswith(needle))
        ]
        if len(hits) != 1:
            known = ", ".join(f"{a.account_id} ({a.alias})" for a in accounts)
            what = "ninguna" if not hits else "varias"
            raise InputError(f"--account {ref!r} coincide con {what} cuentas. Disponibles: {known}.")
        if hits[0] not in chosen:
            chosen.append(hits[0])
    return chosen


def _total_row(label: str, **values: Any) -> dict[str, Any]:
    return {"producto": label, **values}


@app.command()
def position(fmt: FormatOpt = OutputFormat.table) -> None:
    """Posición global: efectivo, cuentas de valores y planes de pensiones."""
    rows: list[dict[str, Any]] = []
    with build_client() as client:
        for acc in client.cash_accounts():
            rows.append(
                {
                    "producto": "efectivo",
                    "id": acc.account_id,
                    "nombre": acc.alias,
                    "valor": acc.balance,
                    "invertido": None,
                    "ganancia": None,
                    "rentab": None,
                }
            )
        for sec in client.securities_accounts():
            rows.append(
                {
                    "producto": "valores",
                    "id": sec.account_id,
                    "nombre": sec.portfolio_name or sec.cod_account,
                    "valor": sec.market_value,
                    "invertido": sec.total_invested,
                    "ganancia": sec.profit,
                    "rentab": sec.total_return,
                }
            )
        for pen in client.pension_accounts():
            rows.append(
                {
                    "producto": "pensiones",
                    "id": pen.account_id,
                    "nombre": pen.external_account_code,
                    "valor": pen.market_value,
                    "invertido": pen.total_invested,
                    "ganancia": pen.profit,
                    "rentab": pen.total_return,
                }
            )
    total = sum((r["valor"] or Decimal(0) for r in rows), Decimal(0))
    if fmt is OutputFormat.table:
        rows.append(
            {
                "producto": "TOTAL",
                "id": "",
                "nombre": "",
                "valor": total,
                "invertido": None,
                "ganancia": None,
                "rentab": None,
            }
        )
    render(rows, fmt, title="Posición global")


@app.command()
def accounts(fmt: FormatOpt = OutputFormat.table) -> None:
    """Cuentas de efectivo con su saldo disponible y retenido."""
    with build_client() as client:
        rows = [
            {
                "id": a.account_id,
                "uuid": a.account_uuid,
                "alias": a.alias,
                "iban": a.iban,
                "estado": a.status,
                "disponible": a.enabled_balance,
                "retenido": a.withheld_balance,
                "saldo": a.balance,
            }
            for a in client.cash_accounts()
        ]
    render(rows, fmt, title="Cuentas")


def _range(date_from: str | None, date_to: str | None, days: int | None) -> tuple[date, date]:
    end = parse_day(date_to, "--to") or date.today()
    if date_from and days:
        raise InputError("Usa --from o --days, no ambos.")
    start = parse_day(date_from, "--from") or end - timedelta(days=DEFAULT_MOVEMENT_DAYS if days is None else days)
    if start > end:
        raise InputError("--from es posterior a --to.")
    return start, end


@app.command()
def movements(
    account: AccountOpt = None,
    date_from: Annotated[str | None, typer.Option("--from", help="Desde (AAAA-MM-DD).")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Hasta (AAAA-MM-DD), por defecto hoy.")] = None,
    days: Annotated[
        int | None, typer.Option(min=1, help=f"Últimos N días (por defecto {DEFAULT_MOVEMENT_DAYS}).")
    ] = None,
    query: Annotated[str | None, typer.Option("--search", "-s", help="Filtrar por texto en concepto/tipo.")] = None,
    oldest_first: Annotated[
        bool, typer.Option(help="Orden cronológico (por defecto, lo más reciente primero).")
    ] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Movimientos de las cuentas de efectivo.

    Más allá de ~90 días MyInvestor pide aprobar la consulta en la app del móvil: en una terminal
    interactiva el CLI espera a que la apruebes; sin terminal, falla con un aviso.
    """
    start, end = _range(date_from, date_to, days)
    if (date.today() - start).days > FREE_FLOWS_DAYS:
        err.print(f"[dim]Rango de más de {FREE_FLOWS_DAYS} días: puede que haya que aprobarlo en la app.[/dim]")
    with build_client() as client:
        rows = _movement_rows(client, match_cash_accounts(client.cash_accounts(), account), start, end)
    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in f"{r['concepto'] or ''} {r['tipo'] or ''}".lower()]
    rows.sort(key=lambda r: (r["fecha"], r["ref"] or ""), reverse=not oldest_first)
    render(rows, fmt, title=f"Movimientos {start} → {end}")


def _movement_rows(client: MyInvestorClient, chosen: list[CashAccount], start: date, end: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ui = client.signature_ui
    for acc in chosen:
        if isinstance(ui, TerminalSignatureUI):
            ui.context = f"movimientos de {acc.alias or acc.account_id} desde {start}"
        statement = client.statement(acc.account_uuid, start, end)
        for f in statement.flows:
            rows.append(
                {
                    "fecha": f.operation_date.isoformat(),
                    "valor_fecha": f.value_date.isoformat() if f.value_date else None,
                    "cuenta": acc.alias or acc.account_id,
                    "tipo": f.operation_type,
                    "concepto": f.concept,
                    "importe": f.amount,
                    "saldo": f.total_balance,
                    "ref": f.reference,
                }
            )
    return rows


@app.command()
def holds(account: AccountOpt = None, fmt: FormatOpt = OutputFormat.table) -> None:
    """Retenciones de saldo (p. ej. dinero bloqueado por órdenes de fondos en curso)."""
    with build_client() as client:
        rows = [
            {"cuenta": acc.alias or acc.account_id, "id": h.id, "tipo": h.type, "importe": h.amount}
            for acc in match_cash_accounts(client.cash_accounts(), account)
            for h in client.holds(acc.account_uuid)
        ]
    render(rows, fmt, title="Retenciones")


@app.command("balance-history")
def balance_history(
    date_from: Annotated[str | None, typer.Option("--from", help="Desde (AAAA-MM-DD).")] = None,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Saldo total diario del efectivo (todas las cuentas)."""
    start = parse_day(date_from, "--from")
    with build_client() as client:
        points = client.balance_history()
    rows = [{"fecha": d.isoformat(), "saldo": b} for d, b in points if start is None or d >= start]
    render(rows, fmt, title="Evolución del saldo")
