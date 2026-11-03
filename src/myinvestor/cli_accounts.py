"""Cash-account commands: `position`, `accounts`, `movements`, `holds`, `balance-history`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

import typer
from rich.markup import escape

from myinvestor.cli_common import FormatOpt, TerminalSignatureUI, app, build_client, err, parse_day
from myinvestor.client import MyInvestorClient, Statement
from myinvestor.config import FREE_FLOWS_DAYS
from myinvestor.errors import InputError, PartialResultError, SignatureError, SignatureRequired
from myinvestor.models import CashAccount, Flow, Ownership, stable_ids
from myinvestor.output import OutputFormat, print_json, render
from myinvestor.psd2 import Span, free_start, split_range

DEFAULT_MOVEMENT_DAYS = 30
# `movements` in a table keeps these columns; JSON and CSV export every field of the row.
MOVEMENT_TABLE_COLUMNS = ("fecha", "valor_fecha", "cuenta", "tipo", "concepto", "importe", "saldo", "ref")

AccountOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--account",
        "-a",
        help="Cuenta: id, uuid, alias o últimos dígitos del IBAN. Repetible; por defecto todas.",
    ),
]


OwnerOpt = Annotated[
    Ownership | None,
    typer.Option(
        "--owner",
        help="Filtrar cuentas: me (soy titular), junior (cuenta de un menor) u other (el resto).",
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


def filter_owner(accounts: list[CashAccount], owner: Ownership | None) -> list[CashAccount]:
    return accounts if owner is None else [a for a in accounts if a.ownership is owner]


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
def accounts(owner: OwnerOpt = None, fmt: FormatOpt = OutputFormat.table) -> None:
    """Cuentas de efectivo con su tipo, tu rol, el titular y el saldo disponible y retenido.

    En JSON añade `titulares` (nombre, rol y si eres tú; nunca el DNI).
    """
    with build_client() as client:
        chosen = filter_owner(client.cash_accounts(), owner)
    rows = [_account_row(a, with_holders=fmt is OutputFormat.json) for a in chosen]
    render(rows, fmt, title="Cuentas")


def _account_row(a: CashAccount, *, with_holders: bool) -> dict[str, Any]:
    main = a.main_holder
    row: dict[str, Any] = {
        "id": a.account_id,
        "uuid": a.account_uuid,
        "alias": a.alias,
        "iban": a.iban,
        "tipo": a.account_type,
        "rol": a.my_role,
        "titular": main.full_name if main else None,
        "estado": a.status,
        "disponible": a.enabled_balance,
        "retenido": a.withheld_balance,
        "saldo": a.balance,
    }
    if with_holders:
        row["titulares"] = [{"nombre": h.full_name, "rol": h.participant_type, "yo": h.me} for h in a.holders]
    return row


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
    owner: OwnerOpt = None,
    movement_id: Annotated[
        str | None,
        typer.Option(
            "--id",
            help="Mostrar solo el movimiento con este id (columna `id`). Se busca en el rango de "
            f"--from/--to/--days (por defecto, los últimos {DEFAULT_MOVEMENT_DAYS} días).",
        ),
    ] = None,
    free_only: Annotated[
        bool,
        typer.Option(
            "--free-only",
            "--clip",
            help=f"Solo lo que MyInvestor da sin segundo factor: recorta --from a los últimos {FREE_FLOWS_DAYS} "
            "días (lo avisa en stderr).",
        ),
    ] = False,
    allow_partial: Annotated[
        bool,
        typer.Option(
            "--allow-partial",
            help="Si falta un tramo que pide firma, salir con 0 (con aviso en stderr) en vez de con 13.",
        ),
    ] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Movimientos de las cuentas de efectivo.

    Sin segundo factor MyInvestor solo da los últimos ~90 días. Un rango más largo se pide en dos
    tramos: primero los últimos 90 días de todas las cuentas y luego lo anterior, que hay que aprobar
    en la app del móvil (una vez por cuenta, solo en una terminal o con --approve). Si falta algún
    tramo, se muestra lo obtenido y se sale con 13 (0 con --allow-partial).
    """
    start, end = _range(date_from, date_to, days)
    if free_only:
        start = _clip_to_free_window(start, end)
    with build_client() as client:
        chosen = filter_owner(match_cash_accounts(client.cash_accounts(), account), owner)
        if movement_id:
            chosen = [a for a in chosen if movement_id.startswith(f"{a.account_uuid}:")] or chosen
        rows, gaps = _movement_rows(client, chosen, start, end)
    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in f"{r['concepto'] or ''} {r['tipo'] or ''}".lower()]
    if movement_id:
        if gaps and not any(r["id"] == movement_id for r in rows):
            raise PartialResultError(_gaps_message(gaps))
        _show_movement(rows, movement_id, start, end, fmt)
        return
    rows.sort(key=lambda r: (r["fecha"], r["ref"] or "", r["id"]), reverse=not oldest_first)
    if fmt is OutputFormat.table:
        rows = [{c: r[c] for c in MOVEMENT_TABLE_COLUMNS} for r in rows]
    render(rows, fmt, title=f"Movimientos {start} → {end}")
    _report_gaps(gaps, allow_partial=allow_partial)


@dataclass(frozen=True)
class _Gap:
    """A span of an account that was not fetched because it needs a second factor."""

    account: CashAccount
    span: Span

    def __str__(self) -> str:
        return f"{self.account.alias or self.account.account_id} del {self.span.start} al {self.span.end}"


def _clip_to_free_window(start: date, end: date) -> date:
    first = free_start(date.today())
    if end < first:
        raise InputError(
            f"--free-only: todo el rango es anterior al {first} y sin segundo factor MyInvestor solo da "
            f"los últimos {FREE_FLOWS_DAYS} días."
        )
    if start < first:
        err.print(
            f"[yellow]Recortado a --from {first}:[/yellow] sin segundo factor MyInvestor solo da "
            f"los últimos {FREE_FLOWS_DAYS} días."
        )
    return max(start, first)


def _gaps_message(gaps: list[_Gap]) -> str:
    return (
        f"Faltan movimientos que MyInvestor solo da con un segundo factor: {'; '.join(map(str, gaps))}. "
        "Para obtenerlos, repite en una terminal interactiva o con `myinvestor --approve …` (sin --no-signature); "
        "para quedarte con lo que sale sin firma, usa --free-only o --allow-partial."
    )


def _report_gaps(gaps: list[_Gap], *, allow_partial: bool) -> None:
    if not gaps:
        return
    if not allow_partial:
        raise PartialResultError(_gaps_message(gaps))
    missing = "; ".join(map(str, gaps))
    err.print(f"[yellow]Resultado parcial:[/yellow] faltan los movimientos que piden firma ({escape(missing)}).")


def _show_movement(rows: list[dict[str, Any]], movement_id: str, start: date, end: date, fmt: OutputFormat) -> None:
    row = next((r for r in rows if r["id"] == movement_id), None)
    if row is None:
        raise InputError(
            f"No hay ningún movimiento con id {movement_id!r} entre {start} y {end}. "
            "Si es de otra fecha, indica el rango con --from/--to o --days."
        )
    if fmt is OutputFormat.json:
        print_json(row)
    elif fmt is OutputFormat.csv:
        render([row], fmt)
    else:
        render([{"campo": k, "valor": v} for k, v in row.items()], fmt, title=f"Movimiento {movement_id}")


def _movement_rows(
    client: MyInvestorClient, chosen: list[CashAccount], start: date, end: date
) -> tuple[list[dict[str, Any]], list[_Gap]]:
    """Rows of every account, free span first for all of them, then the older span (one signature each).

    Without a way to sign, or once a signature fails, older spans are not asked for and become gaps;
    what was already fetched is kept. Ids are numbered over all the spans of an account together.
    """
    spans = split_range(start, end, date.today())
    if any(not s.free for s in spans) and client.signature_ui is not None:
        err.print(
            f"[dim]Más de {FREE_FLOWS_DAYS} días: primero lo posterior al {free_start(date.today())} de cada "
            "cuenta; lo anterior hay que aprobarlo en la app (una vez por cuenta).[/dim]"
        )
    flows: dict[str, list[Flow]] = {a.account_uuid: [] for a in chosen}
    ibans: dict[str, str | None] = {a.account_uuid: a.iban for a in chosen}
    gaps: list[_Gap] = []
    for span in spans:
        for acc in chosen:
            if not span.free and (client.signature_ui is None or gaps):
                gaps.append(_Gap(acc, span))
                continue
            statement = _span_statement(client, acc, span)
            if statement is None:
                gaps.append(_Gap(acc, span))
                continue
            flows[acc.account_uuid].extend(statement.flows)
            ibans[acc.account_uuid] = ibans[acc.account_uuid] or statement.iban
    rows = [
        _movement_row(acc, ibans[acc.account_uuid], f, i)
        for acc in chosen
        for f, i in zip(flows[acc.account_uuid], stable_ids(acc.account_uuid, flows[acc.account_uuid]), strict=True)
    ]
    return rows, gaps


def _span_statement(client: MyInvestorClient, acc: CashAccount, span: Span) -> Statement | None:
    """The statement of one span, or None if MyInvestor asked for a signature that was not completed."""
    ui = client.signature_ui
    if isinstance(ui, TerminalSignatureUI):
        ui.context = f"movimientos de {acc.alias or acc.account_id} desde {span.start}"
    try:
        return client.statement(acc.account_uuid, span.start, span.end)
    except SignatureError as exc:
        if not isinstance(exc, SignatureRequired):  # rejected, timed out…: say why
            err.print(f"[yellow]{escape(acc.alias or acc.account_id)}:[/yellow] {escape(str(exc))}")
        return None


def _movement_row(acc: CashAccount, iban: str | None, f: Flow, movement_id: str) -> dict[str, Any]:
    return {
        "id": movement_id,
        "fecha": f.operation_date.isoformat(),
        "valor_fecha": f.value_date.isoformat() if f.value_date else None,
        "cuenta": acc.alias or acc.account_id,
        "cuenta_id": acc.account_id,
        "cuenta_uuid": acc.account_uuid,
        "iban": iban,
        "sentido": f.flow_type,
        "tipo": f.operation_type,
        "clase": f.operation_class,
        "concepto": f.concept,
        "importe": f.amount,
        "saldo": f.total_balance,
        "origen": f.operation_origin_label,
        "destino": f.operation_target_label,
        "contraparte": f.counterparty,
        "ref": f.reference,
        "operacion_ref": f.operation_ref,
        "fichero_ref": f.file_ref,
    }


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
