"""Cash-account commands: `position`, `accounts`, `movements`, `holds`, `balance-history`."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from myinvestor.cli_common import (
    DaysOpt,
    FormatOpt,
    FromOpt,
    TerminalSignatureUI,
    ToOpt,
    app,
    build_client,
    err,
    iso_moment,
    now_local,
    optional_range,
    parse_day,
)
from myinvestor.cli_fiscal import FiscalOpt, account_cost, fund_costs, pension_cost, plan_costs
from myinvestor.cli_state import MarkOpt, NewOpt, StateOpt, record, state_file
from myinvestor.client import MyInvestorClient, Statement
from myinvestor.config import FREE_FLOWS_DAYS
from myinvestor.errors import ApiError, InputError, PartialResultError, SignatureError, SignatureRequired
from myinvestor.fiscal import BLANK, columns, total_columns
from myinvestor.models import (
    CashAccount,
    Flow,
    Hold,
    Ownership,
    PensionAccount,
    PensionPlanPosition,
    SecuritiesAccount,
    stable_ids,
)
from myinvestor.output import OutputFormat, normalize_row, print_json, render, table_fits
from myinvestor.portfolios import is_portfolio, own_cash, portfolio_title
from myinvestor.psd2 import Span, free_start, split_range
from myinvestor.sent_transfers import with_beneficiaries
from myinvestor.signature import INAPP, can_start
from myinvestor.state import AccountCursor, StateKind, movement_marks, unseen_movements
from myinvestor.transfers_match import operation_id, with_transfer_fields

DEFAULT_MOVEMENT_DAYS = 30
NOT_FOUND = 404
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


# Shared by `movements`, `income` and `internal-transfers`: the part of a range that needs a second factor.
FreeOnlyOpt = Annotated[
    bool,
    typer.Option(
        "--free-only",
        "--clip",
        help=f"Solo lo que MyInvestor da sin segundo factor: recorta --from a los últimos {FREE_FLOWS_DAYS} "
        "días (lo avisa en stderr).",
    ),
]
DetailsOpt = Annotated[
    bool,
    typer.Option(
        "--details",
        help="Añadir `beneficiario` e `iban_destino` a los cargos de transferencias emitidas (JSON/CSV): "
        "una petición más por cuenta con algún cargo así.",
    ),
]
AllowPartialOpt = Annotated[
    bool,
    typer.Option(
        "--allow-partial",
        help="Si falta un tramo que pide firma, salir con 0 (con aviso en stderr) en vez de con 13.",
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
            what = "no coincide con ninguna cuenta" if not hits else "coincide con varias cuentas"
            raise InputError(f"--account {ref!r} {what}. Disponibles: {known}.")
        if hits[0] not in chosen:
            chosen.append(hits[0])
    return chosen


def filter_owner(accounts: list[CashAccount], owner: Ownership | None) -> list[CashAccount]:
    return accounts if owner is None else [a for a in accounts if a.ownership is owner]


POSITION_TOTAL = "total"  # `producto` of the total row in JSON/CSV/TSV ("TOTAL" in the table)
# Keys of a managed portfolio's row (`null` in the rest); the table only shows `efectivo`, and only if some has it.
PORTFOLIO_KEYS = ("cartera", "perfil", "efectivo", "efectivo_id")


def _latest(days: Iterable[date | None]) -> str | None:
    known = [d for d in days if d]
    return max(known).isoformat() if known else None


def _cash_position(acc: CashAccount, today: date) -> dict[str, Any]:
    return {
        "producto": "efectivo",
        "id": acc.account_id,
        "nombre": acc.alias,
        "valor": acc.balance,
        "invertido": None,
        "ganancia": None,
        "rentab": None,
        "fecha_valor": today.isoformat(),  # the balance is live
    }


def _securities_position(sec: SecuritiesAccount) -> dict[str, Any]:
    return {
        "producto": "valores",
        "id": sec.account_id,
        "nombre": sec.portfolio_name or sec.cod_account,
        "valor": sec.market_value,
        "invertido": sec.total_invested,
        "ganancia": sec.profit,
        "rentab": sec.total_return,
        "fecha_valor": _latest(inv.liquidation_value_date for inv in sec.investments),
    }


def _portfolio_position(sec: SecuritiesAccount, cash: CashAccount | None, today: date) -> dict[str, Any]:
    """A managed portfolio with its own cash account: `valor` = its funds + that cash (`efectivo`)."""
    row = _securities_position(sec)
    with_cash = (
        {
            "valor": (sec.market_value or Decimal(0)) + cash.balance,
            "fecha_valor": row["fecha_valor"] or today.isoformat(),
        }
        if cash
        else {}
    )
    return {
        **row,
        **with_cash,
        "nombre": portfolio_title(sec),
        "cartera": sec.portfolio_name,
        "perfil": sec.portfolio_profile_type,
        "efectivo": cash.balance if cash else None,
        "efectivo_id": cash.account_id if cash else None,
    }


def _position_rows(cash: list[CashAccount], securities: list[SecuritiesAccount], today: date) -> list[dict[str, Any]]:
    """Cash accounts, then securities accounts; a portfolio's own cash account goes inside the portfolio."""
    owned = own_cash(securities, cash)
    grouped = {a.account_id for a in owned.values()}
    blank = dict.fromkeys(PORTFOLIO_KEYS)
    rows = [{**_cash_position(acc, today), **blank} for acc in cash if acc.account_id not in grouped]
    return rows + [
        _portfolio_position(sec, owned.get(sec.account_id), today)
        if is_portfolio(sec)
        else {**_securities_position(sec), **blank}
        for sec in securities
    ]


def _table_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The table keeps the columns it always had: a portfolio's own cash is already in its `valor`."""
    return [{k: v for k, v in r.items() if k not in PORTFOLIO_KEYS} for r in rows]


def _pension_position(pen: PensionAccount, plans: list[PensionPlanPosition]) -> dict[str, Any]:
    return {
        "producto": "pensiones",
        "id": pen.account_id,
        "nombre": pen.external_account_code,
        "valor": pen.market_value,
        "invertido": pen.total_invested,
        "ganancia": pen.profit,
        "rentab": pen.total_return,
        "fecha_valor": _latest(p.liquidation_value_date for p in plans),
    }


def _total_row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    total = sum((r["valor"] or Decimal(0) for r in rows), Decimal(0))
    return {
        "producto": label,
        "id": "",
        "nombre": "",
        "valor": total,
        "invertido": None,
        "ganancia": None,
        "rentab": None,
        "fecha_valor": None,
    }


def _position_fiscal(
    client: MyInvestorClient,
    securities: list[SecuritiesAccount],
    pensions: list[tuple[PensionAccount, list[PensionPlanPosition]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """`coste_fiscal`, `ganancia_real` and `rentab_real` by (`producto`, `id`); a portfolio's without its cash."""
    funds = fund_costs(client, ((sec.account_id, inv) for sec in securities for inv in sec.investments))
    plans = plan_costs(client, [pen for pen, _ in pensions])
    found = {("valores", s.account_id): columns(s.market_value, account_cost(s, funds)) for s in securities}
    return found | {
        ("pensiones", pen.account_id): columns(pen.market_value, pension_cost(pen.account_id, held, plans))
        for pen, held in pensions
    }


@app.command()
def position(fiscal: FiscalOpt = False, fmt: FormatOpt = OutputFormat.table) -> None:
    """Posición global: efectivo, cuentas de valores y planes de pensiones, con el total.

    `fecha_valor` es el VL más reciente de las inversiones de cada producto (hoy, en el efectivo).
    En JSON/CSV/TSV cada fila lleva además `consultado_en` (fecha y hora de la consulta, con zona)
    y la última es el total, con `producto: "total"`.

    Una cartera del roboadvisor sale como «Cartera METAL (muy agresiva)» y su cuenta de efectivo propia
    va dentro de ella: su `valor` suma ese efectivo (columna `efectivo`) en vez de listarlo aparte.

    `invertido` es el que muestra MyInvestor, que tras un traspaso no es el coste fiscal. Con `--fiscal`,
    `coste_fiscal` (el coste heredado en los traspasos: lotes vivos de los fondos y planes reconstruidos
    con su histórico), `ganancia_real` y `rentab_real`; en el total, la suma de los productos sin el efectivo.
    """
    consulted = now_local()
    blank = dict.fromkeys(PORTFOLIO_KEYS)
    with build_client() as client:
        cash, securities = client.cash_accounts(), client.securities_accounts()
        pensions = [(pen, client.pension_plans(pen.account_id)) for pen in client.pension_accounts()]
        fiscal_rows = _position_fiscal(client, securities, pensions) if fiscal else None
    rows = _position_rows(cash, securities, consulted.date())
    rows += [{**_pension_position(pen, plans), **blank} for pen, plans in pensions]
    if fiscal_rows is not None:
        rows = [{**r, **fiscal_rows.get((r["producto"], r["id"]), BLANK)} for r in rows]
    label = POSITION_TOTAL.upper() if fmt is OutputFormat.table else POSITION_TOTAL
    total = {**_total_row(rows, label), **blank}
    if fiscal_rows is not None:
        total |= total_columns(r for r in rows if (r["producto"], r["id"]) in fiscal_rows)
    rows.append(total)
    if fmt is OutputFormat.table:
        render(_table_columns(rows), fmt, title=f"Posición global a {consulted:%Y-%m-%d %H:%M}")
        return
    render([{**r, "consultado_en": iso_moment(consulted)} for r in rows], fmt)


@app.command()
def accounts(owner: OwnerOpt = None, fmt: FormatOpt = OutputFormat.table) -> None:
    """Cuentas de efectivo con su tipo, tu rol, el titular, el saldo disponible y retenido, y su TAE.

    `tipo_pct` y `tae_pct` (en %) son los de la última liquidación de intereses (como en `interest`):
    una petición más por cuenta, y vacíos si la cuenta no tiene remuneración. Una TAE de 0 sale vacía:
    MyInvestor la manda así cuando no la ha calculado, y la web la oculta.
    En JSON añade `titulares` (nombre, rol y si eres tú; nunca el DNI).
    """
    with build_client() as client:
        chosen = filter_owner(client.cash_accounts(), owner)
        rows = [{**_account_row(a, with_holders=fmt is OutputFormat.json), **_rates(client, a)} for a in chosen]
    render(rows, fmt, title="Cuentas")


def _rates(client: MyInvestorClient, acc: CashAccount) -> dict[str, Any]:
    """`tipo_pct` and `tae_pct` of the last settlement; empty (with a notice) if MyInvestor gives none or fails.

    A TAE of 0 means «not computed» (the web hides it: `e !== 0 ? e : undefined`), so it becomes None.
    """
    try:
        found = client.remuneration(acc.account_id)
    except ApiError as exc:
        if exc.status != NOT_FOUND:
            name = escape(acc.alias or acc.account_id)
            err.print(f"[yellow]{name}:[/yellow] sin TAE, falló la consulta de su remuneración ({escape(str(exc))}).")
        found = None
    tae = found.calculate_tae_average if found else None
    return {
        "tipo_pct": found.remuneration_percentage if found else None,
        "tae_pct": tae or None,
    }


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
    since_date: Annotated[
        str | None,
        typer.Option("--since-date", help="Solo los posteriores a este día (AAAA-MM-DD), sin incluirlo."),
    ] = None,
    since_id: Annotated[
        str | None,
        typer.Option(
            "--since-id",
            help="Solo los posteriores a este movimiento en su cuenta (por fecha y luego `ref`/`id`). Se busca "
            f"en el rango de --from/--days (por defecto, los últimos {DEFAULT_MOVEMENT_DAYS} días).",
        ),
    ] = None,
    new: NewOpt = False,
    state_path: StateOpt = None,
    mark: MarkOpt = True,
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
    free_only: FreeOnlyOpt = False,
    allow_partial: AllowPartialOpt = False,
    details: DetailsOpt = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Movimientos de las cuentas de efectivo.

    Sin segundo factor MyInvestor solo da los últimos ~90 días. Un rango más largo se pide en dos
    tramos: primero los últimos 90 días de todas las cuentas y luego lo anterior, que hay que aprobar
    en la app del móvil (una vez por cuenta, solo en una terminal o con --approve). Si falta algún
    tramo, se muestra lo obtenido y se sale con 13 (0 con --allow-partial).

    Con --new solo salen los que no se mostraron la vez anterior con --new (estado local, ver
    `myinvestor state`); nunca pide nada fuera de los últimos 90 días.
    """
    if new:
        _reject_with_new(
            date_to=date_to, since_date=since_date, since_id=since_id, query=query, movement_id=movement_id
        )
        _new_movements(
            account,
            owner,
            (date_from, days),
            state_path,
            mark=mark,
            oldest_first=oldest_first,
            details=details,
            fmt=fmt,
        )
        return
    if since_date and since_id:
        raise InputError("Usa --since-date o --since-id, no ambos.")
    if since_date and (date_from or days):
        raise InputError("Usa --since-date o --from/--days, no ambos.")
    start, end = _since_range(since_date, date_to) if since_date else _range(date_from, date_to, days)
    if free_only:
        start = _clip_to_free_window(start, end)
    anchor = since_id or movement_id
    with build_client() as client:
        chosen = filter_owner(match_cash_accounts(client.cash_accounts(), account), owner)
        if anchor:
            chosen = [a for a in chosen if anchor.startswith(f"{a.account_uuid}:")] or chosen
        rows, gaps = _movement_rows(client, chosen, start, end) if start <= end else ([], [])
        if details:
            rows = _with_details(client, chosen, rows)
    rows = with_transfer_fields(rows)  # before --since-id/--search: the other leg may not match them
    if since_id:
        rows = _after_movement(rows, since_id, gaps, start, end)
    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in f"{r['concepto'] or ''} {r['tipo'] or ''}".lower()]
    if movement_id:
        if gaps and not any(r["id"] == movement_id for r in rows):
            raise PartialResultError(_gaps_message(gaps))
        _show_movement(rows, movement_id, start, end, fmt)
        return
    _render_movements(rows, f"Movimientos {start} → {end}", oldest_first=oldest_first, fmt=fmt)
    _report_gaps(gaps, allow_partial=allow_partial)


def _movement_order(row: dict[str, Any]) -> tuple[str, str, str]:
    """Chronological order of movements: operation date, then `ref` (a per-account counter), then `id`."""
    return row["fecha"], row["ref"] or "", row["id"]


def _render_movements(rows: list[dict[str, Any]], title: str, *, oldest_first: bool, fmt: OutputFormat) -> None:
    rows = sorted(rows, key=_movement_order, reverse=not oldest_first)
    if fmt is OutputFormat.table:
        rows = _movement_table_rows(rows)
    render(rows, fmt, title=title)


def _movement_table_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Table columns; `valor_fecha` goes when it repeats `fecha` in every row and the table does not fit."""
    table = [{c: r[c] for c in MOVEMENT_TABLE_COLUMNS} for r in rows]
    if all(r["valor_fecha"] == r["fecha"] for r in table) and not table_fits(table):
        return [{c: v for c, v in r.items() if c != "valor_fecha"} for r in table]
    return table


def _since_range(since_date: str, date_to: str | None) -> tuple[date, date]:
    """`--since-date D`: from the day after D to `--to` (if that is after `--to`, nothing is asked)."""
    since = parse_day(since_date, "--since-date") or date.today()
    end = parse_day(date_to, "--to") or date.today()
    return since + timedelta(days=1), end


def _after_movement(
    rows: list[dict[str, Any]], since_id: str, gaps: list[_Gap], start: date, end: date
) -> list[dict[str, Any]]:
    """Movements of the account of `since_id` that come after it in `_movement_order`."""
    anchor = next((r for r in rows if r["id"] == since_id), None)
    if anchor is None:
        if gaps:
            raise PartialResultError(_gaps_message(gaps))
        raise InputError(
            f"--since-id: no hay ningún movimiento con id {since_id!r} entre {start} y {end}. "
            "Si es de otra fecha, indica el rango con --from o --days."
        )
    after = _movement_order(anchor)
    return [r for r in rows if r["cuenta_uuid"] == anchor["cuenta_uuid"] and _movement_order(r) > after]


_NOT_WITH_NEW = {"date_to": "--to", "since_date": "--since-date", "since_id": "--since-id", "query": "--search"}


def _reject_with_new(**given: str | None) -> None:
    clashing = [_NOT_WITH_NEW.get(k, "--id") for k, v in given.items() if v]
    if clashing:
        raise InputError(
            f"--new no se combina con {', '.join(clashing)}: pide hasta hoy desde lo último visto "
            "(--from/--days solo cuentan la primera vez)."
        )


def _new_movements(
    account: list[str] | None,
    owner: Ownership | None,
    first_range: tuple[str | None, int | None],
    state_path: Path | None,
    *,
    mark: bool,
    oldest_first: bool,
    details: bool,
    fmt: OutputFormat,
) -> None:
    """`movements --new`: ask each account from its last day minus the margin and emit the unseen ids."""
    file = state_file(state_path)
    cursors = file.load().marked.movements
    today = date.today()
    date_from, days = first_range
    first_start = _clip_to_free_window(_range(date_from, None, days)[0], today)
    with build_client() as client:
        chosen = filter_owner(match_cash_accounts(client.cash_accounts(), account), owner)
        by_start: dict[date, list[CashAccount]] = {}
        for acc in chosen:
            by_start.setdefault(_new_start(acc, cursors.get(acc.account_uuid), first_start, today), []).append(acc)
        rows: list[dict[str, Any]] = []
        gaps: list[_Gap] = []
        for start, accs in sorted(by_start.items()):
            got, missing = _movement_rows(client, accs, start, today)
            rows += got
            gaps += missing
        if details:
            rows = _with_details(client, chosen, rows)
    rows = with_transfer_fields(rows)  # over everything fetched: the other leg may have been seen already
    _render_movements(
        unseen_movements(rows, cursors), f"Movimientos nuevos (hasta {today})", oldest_first=oldest_first, fmt=fmt
    )
    gapped = {g.account.account_uuid for g in gaps}  # never inside the free window, but never mark a gap as seen
    complete = [a.account_uuid for a in chosen if a.account_uuid not in gapped]
    marks = movement_marks([r for r in rows if r["cuenta_uuid"] not in gapped], complete, today)
    record(file, marks, mark=mark, kind=StateKind.movements)
    _report_gaps(gaps, allow_partial=False)


def _new_start(acc: CashAccount, cursor: AccountCursor | None, first_start: date, today: date) -> date:
    """From the cursor minus the margin, never before the free window (warned) nor after today."""
    if cursor is None:
        return first_start
    wanted = min(cursor.next_start(), today)
    window = free_start(today)
    if wanted >= window:
        return wanted
    err.print(
        f"[yellow]{escape(acc.alias or acc.account_id)}:[/yellow] lo último visto es del {cursor.last_day}, "
        f"antes de la ventana sin firma; se consulta desde el {window}. Lo anterior a esa fecha no se revisa "
        "(si lo necesitas, pídelo con --from)."
    )
    return window


def _with_details(
    client: MyInvestorClient, chosen: list[CashAccount], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """`--details`: the sent transfers of each account with a charge that may be one (one request each)."""
    wanted = {r["cuenta_id"] for r in rows if r["importe"] < 0 and operation_id(r["operacion_ref"])}
    sent = {a.account_id: client.transfers(a.account_id) for a in chosen if a.account_id in wanted}
    return with_beneficiaries(rows, sent)


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
        print_json(normalize_row(row))  # amounts to cents, as in the list
    elif fmt is OutputFormat.csv:
        render([row], fmt)
    else:
        render([{"campo": k, "valor": v} for k, v in row.items()], fmt, title=f"Movimiento {movement_id}")


@dataclass(frozen=True)
class _Fetched:
    """The statement of one span of an account."""

    account: CashAccount
    span: Span
    statement: Statement


def _movement_rows(
    client: MyInvestorClient, chosen: list[CashAccount], start: date, end: date
) -> tuple[list[dict[str, Any]], list[_Gap]]:
    """Rows of every account, free span first for all of them, then the older span (one signature each).

    Without a way to sign, or once a signature fails, older spans are not asked for and become gaps;
    what was already fetched is kept. Ids are numbered over all the spans of an account together.
    """
    fetched, gaps = _fetch_statements(client, chosen, start, end)
    return _rows_of(chosen, fetched), gaps


def _fetch_statements(
    client: MyInvestorClient, chosen: list[CashAccount], start: date, end: date
) -> tuple[list[_Fetched], list[_Gap]]:
    """The statements behind `_movement_rows`, one per account and span (`export` checks their balances)."""
    spans = split_range(start, end, date.today())
    can_approve = can_start(client.signature_ui, INAPP)  # /dev/tty alone types codes, it does not approve
    if any(not s.free for s in spans) and can_approve:
        err.print(
            f"[dim]Más de {FREE_FLOWS_DAYS} días: primero lo posterior al {free_start(date.today())} de cada "
            "cuenta; lo anterior hay que aprobarlo en la app (una vez por cuenta).[/dim]"
        )
    fetched: list[_Fetched] = []
    gaps: list[_Gap] = []
    for span in spans:
        for acc in chosen:
            if not span.free and (not can_approve or gaps):
                gaps.append(_Gap(acc, span))
                continue
            statement = _span_statement(client, acc, span)
            if statement is None:
                gaps.append(_Gap(acc, span))
                continue
            fetched.append(_Fetched(acc, span, statement))
    return fetched, gaps


def _rows_of(chosen: list[CashAccount], fetched: list[_Fetched]) -> list[dict[str, Any]]:
    """Movement rows of the fetched statements; ids are numbered over all the spans of each account."""
    rows: list[dict[str, Any]] = []
    for acc in chosen:
        mine = [f for f in fetched if f.account.account_uuid == acc.account_uuid]
        flows = [flow for f in mine for flow in f.statement.flows]
        iban = acc.iban or next((f.statement.iban for f in mine if f.statement.iban), None)
        ids = stable_ids(acc.account_uuid, flows)
        rows += [_movement_row(acc, iban, f, i) for f, i in zip(flows, ids, strict=True)]
    return rows


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
            hold_row(acc, h)
            for acc in match_cash_accounts(client.cash_accounts(), account)
            for h in client.holds(acc.account_uuid)
        ]
    render(rows, fmt, title="Retenciones")


def hold_row(acc: CashAccount, h: Hold) -> dict[str, Any]:
    return {"cuenta": acc.alias or acc.account_id, "id": h.id, "tipo": h.type, "importe": h.amount}


@app.command("balance-history")
def balance_history(
    date_from: FromOpt = None,
    date_to: ToOpt = None,
    days: DaysOpt = None,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Saldo total diario del efectivo (todas las cuentas). Sin fechas, todo lo que da la API."""
    start, end = optional_range(date_from, date_to, days)
    with build_client() as client:
        points = client.balance_history()
    rows = [
        {"fecha": d.isoformat(), "saldo": b} for d, b in points if (not start or d >= start) and (not end or d <= end)
    ]
    render(rows, fmt, title="Evolución del saldo")
