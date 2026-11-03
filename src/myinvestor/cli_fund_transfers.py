"""`fund-transfers`: fund transfers of the securities accounts, or pension-plan transfers (read-only).

Each row is `externo` (what `…/transfers` lists) or `interno`: a switch between two funds of the
same account rebuilt from its orders (`internal_transfers`) or, with `--pension`, a mobilisation
between two plans rebuilt from the pension orders or deduced from the position history (`plan_moves`).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Annotated, Any

import typer

from myinvestor.cli_common import FormatOpt, app, build_client, parse_day
from myinvestor.cli_gains import DEAD_STATUSES
from myinvestor.cli_invest import _pension_ids, _securities
from myinvestor.client import MyInvestorClient, Period
from myinvestor.errors import InputError
from myinvestor.internal_transfers import MAX_GAP_DAYS, InternalTransfer, pair_orders
from myinvestor.models import FundTransfer, Order, PensionTransfer
from myinvestor.output import OutputFormat, render
from myinvestor.plan_moves import PlanMove, deduce_plan_moves

DEFAULT_FUND_TRANSFER_DAYS = 60  # what the web asks for
EXTERNAL, INTERNAL = "externo", "interno"
FROM_API, FROM_ORDERS, FROM_HISTORY = "api", "orders", "history"
SAME_TRANSFER_DAYS = MAX_GAP_DAYS  # rows of two sources this close, same account, origin and destination: one
# Shortest history period that reaches back N days (the API gives no more than a year).
HISTORY_PERIODS = ((28, Period.month), (89, Period.three_months), (180, Period.six_months))
# The table joins each code with its name and leaves out `moneda`, `fuente` and `ref_destino`;
# JSON and CSV export every field.
FUND_TRANSFER_TABLE_COLUMNS = (
    "fecha",
    "tipo",
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

    Por defecto, los últimos 60 días, como la web, de todas las cuentas. `tipo` dice si es
    `externo` (lo que lista MyInvestor como traspaso: la web solo enseña los que están en curso)
    o `interno`: un cambio entre dos fondos de la misma cuenta («Baja/Alta switch», «Reembolso/
    Suscripción por Traspaso Interno»), reconstruido emparejando sus dos órdenes (`fuente: orders`;
    si una pata no casa, sale sola). Con --pension, las movilizaciones entre dos planes de la misma
    cuenta salen de las órdenes del plan o, si no están, se deducen de `history` (`fuente: history`,
    como mucho un año atrás): un plan pierde participaciones y otro nace o sube por un valor parecido
    (±5 %) en los 5 días siguientes; fecha y valor son aproximados. Peticiones por cuenta: traspasos
    y órdenes (con --pension, también la evolución).
    """
    if pending:
        _reject_with_pending(pension=pension, since=since, until=until)
        _pending(account, fmt)
        return
    start, end = _range(since, until)
    with build_client() as client:
        found = _pension_rows(client, account, start, end) if pension else _fund_rows(client, account, start, end)
    found = [r for r in found if r["fecha"] is None or start.isoformat() <= r["fecha"] <= end.isoformat()]
    found.sort(key=lambda r: (r["fecha"] or "", r["ref"]), reverse=True)
    what = "planes de pensiones" if pension else "fondos"
    if fmt is OutputFormat.table:
        found = [_table_row(r) for r in found]
    render(found, fmt, title=f"Traspasos de {what} {start} → {end}")


def _fund_rows(client: MyInvestorClient, refs: list[str] | None, start: date, end: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in _securities_ids(client, refs):
        external = [_fund_row(i, t) for t in client.fund_transfers(i, start, end)]
        orders = client.orders(i, start - timedelta(days=MAX_GAP_DAYS), _orders_until(end))
        rows += _merged(external, [_internal_row(t) for t in pair_orders(_alive(orders))])
    return rows


def _pension_rows(client: MyInvestorClient, refs: list[str] | None, start: date, end: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in _pension_ids(client, refs):
        external = [_pension_row(i, t) for t in client.pension_transfers(i, start, end)]
        orders = client.pension_orders(i, start - timedelta(days=MAX_GAP_DAYS), _orders_until(end))
        merged = _merged(external, [_internal_row(t) for t in pair_orders(_alive(orders))])
        series = client.position_history(i, _history_period(start), pension=True)
        rows += _merged(merged, [_move_row(i, m) for m in deduce_plan_moves(series)])
    return rows


def _orders_until(end: date) -> date:
    """`end` plus room for the incoming leg of a transfer started on `end`, never past today."""
    return max(end, min(end + timedelta(days=MAX_GAP_DAYS), date.today()))


def _alive(orders: Iterable[Order]) -> list[Order]:
    return [o for o in orders if (o.status or "").upper() not in DEAD_STATUSES]


def _history_period(start: date) -> Period:
    days = (date.today() - start).days
    return next((period for reach, period in HISTORY_PERIODS if days <= reach), Period.year)


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two rows of different sources describe the same transfer.

    Same account, a few days apart, and the same origin and destination where both rows know
    them (a lone leg knows only one side), with at least one side in common.
    """
    if a["cuenta"] != b["cuenta"] or a["fecha"] is None or b["fecha"] is None:
        return False
    sides = [(a[k], b[k]) for k in ("origen", "destino") if a[k] is not None and b[k] is not None]
    if not sides or any(x != y for x, y in sides):
        return False
    return abs((date.fromisoformat(a["fecha"]) - date.fromisoformat(b["fecha"])).days) <= SAME_TRANSFER_DAYS


def _merged(kept: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`kept` plus the `extra` rows that are not already there; a kept row seen in `extra` is internal."""
    kept = [{**r, "tipo": INTERNAL} if any(_same(r, e) for e in extra) else r for r in kept]
    return kept + [e for e in extra if not any(_same(e, r) for r in kept)]


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


def _internal_row(t: InternalTransfer) -> dict[str, Any]:
    out, into, first = t.out, t.into, t.first
    return {
        "fecha": t.day.isoformat(),
        "tipo": INTERNAL,
        "cuenta": t.account_id,
        "origen": out.product if out else None,
        "origen_nombre": out.fund_name if out else None,
        "destino": into.product if into else None,
        "destino_nombre": into.fund_name if into else None,
        "participaciones": first.shares,
        "importe": t.amount,
        "moneda": first.currency,
        "estado": t.status,
        "cancelable": any(leg.is_cancelable for leg in (out, into) if leg is not None),
        "ref": first.reference,
        "fuente": FROM_ORDERS,
        "ref_destino": into.reference if into else None,
    }


def _move_row(account_id: str, m: PlanMove) -> dict[str, Any]:
    return {
        "fecha": m.day.isoformat(),
        "tipo": INTERNAL,
        "cuenta": account_id,
        "origen": m.origin,
        "origen_nombre": None,
        "destino": m.destination,
        "destino_nombre": None,
        "participaciones": m.shares,
        "importe": m.value,
        "moneda": "EUR",
        "estado": None,
        "cancelable": False,
        "ref": f"{FROM_HISTORY}:{account_id}:{m.day.isoformat()}:{m.origin}>{m.destination}",
        "fuente": FROM_HISTORY,
        "ref_destino": None,
    }


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
        "tipo": EXTERNAL,
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
        "fuente": FROM_API,
        "ref_destino": None,
    }


def _labelled(code: str | None, name: str | None) -> str | None:
    return " ".join(p for p in (code, name) if p) or None


def _table_row(row: dict[str, Any]) -> dict[str, Any]:
    shown = {
        **row,
        "origen": _labelled(row["origen"], row["origen_nombre"]),
        "destino": _labelled(row["destino"], row["destino_nombre"]),
        "tipo": f"{row['tipo']} (deducido)" if row["fuente"] == FROM_HISTORY else row["tipo"],
        "cancelable": "sí" if row["cancelable"] else "no",
    }
    return {c: shown[c] for c in FUND_TRANSFER_TABLE_COLUMNS}
