"""Order executions and capital gains: `order` (one order), `lots` (open FIFO lots) and `gains` (realised)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

import typer

from myinvestor.allocation import is_fund
from myinvestor.cli_common import FormatOpt, app, build_client
from myinvestor.cli_invest import EARLIEST_ORDER_DATE, SecAccountOpt, _securities
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError
from myinvestor.models import FundLot, Investment, Order, OrderDetail, OrderExecution, SecuritiesAccount
from myinvestor.output import OutputFormat, normalize_row, print_json, render

TOTAL = "total"
GAINS, LOSSES = "plusvalias", "minusvalias"

# `operationType` of the orders that take shares out of a fund (labels from the web bundle).
SALE = "INVESTMENT_FUND_REIMBURSEMENT"  # "Reembolso Fondos de Inversión": taxed in the year
TRANSFERS_OUT = frozenset(
    {
        "INTERNAL_TRANSFER_REIMBURSEMENT",  # "Reembolso por Traspaso Interno"
        "EXTERNAL_TRANSFER_REIMBURSEMENT",  # "Reembolso por Traspaso Externo"
        "IIC_SWITCH_DEREGISTRATION",  # "Baja switch"
        "INTERNAL_TRANSFER_IIC",  # "Traspaso interno IIC LP"
    }
)
OUTGOING = TRANSFERS_OUT | {SALE}
DEAD_STATUSES = frozenset({"CANCEL", "CANCELLED", "REJECTED"})  # never executed: no detail requested

IsinArg = Annotated[str | None, typer.Argument(help="ISIN de un fondo. Sin él: todos.")]


def _sum(values: Iterable[Decimal | None]) -> Decimal:
    return sum((v for v in values if v is not None), Decimal(0))


# ---- order REF -----------------------------------------------------------------------------------


@app.command("order")
def order_detail(
    reference: Annotated[str, typer.Argument(help="Referencia de la orden (columna `ref` de `orders`).")],
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="Id de la cuenta de valores (o de pensiones con --pension) de la orden."),
    ] = None,
    pension: Annotated[bool, typer.Option("--pension", help="Es una orden de un plan de pensiones.")] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Una orden con su ejecución: fecha, participaciones, VL, bruto, comisiones, retenciones, neto y resultado fiscal.

    Sin -a busca la referencia en el histórico de órdenes de cada cuenta de valores (una petición por
    cuenta) y, si no aparece (o con --pension), en las de pensiones; después pide el detalle.
    En JSON: `{"orden": {…}, "ejecuciones": […]}`; en CSV/TSV, una fila por ejecución. Nunca sale el DNI del ordenante.
    """
    with build_client() as client:
        account_id, is_pension = (account, pension) if account else _locate(client, reference, pension=pension)
        detail = client.order_detail(account_id, reference, pension=is_pension)
    head = _order_head(account_id, is_pension, detail)
    runs = [_execution_row(run) for run in detail.related_operations]
    if fmt is OutputFormat.json:
        print_json({"orden": normalize_row(head), "ejecuciones": [normalize_row(r) for r in runs]})
    elif fmt is OutputFormat.table:
        render([head], fmt, title=f"Orden {detail.order_reference}")
        render(runs, fmt, title="Ejecuciones")
    else:
        render([{"cuenta": account_id, "ref": detail.order_reference, "isin": detail.isin, **r} for r in runs], fmt)


def _has(orders: Iterable[Order], reference: str) -> bool:
    return any(o.reference == reference for o in orders)


def _locate(client: MyInvestorClient, reference: str, *, pension: bool) -> tuple[str, bool]:
    """(account id, is pension) of the order with `reference`, searching every account's order history."""
    if not pension:
        for account_id in client.securities_account_ids():
            if _has(client.orders(account_id, EARLIEST_ORDER_DATE, date.today()), reference):
                return account_id, False
    for acc in client.pension_accounts():
        if _has(client.pension_orders(acc.account_id, EARLIEST_ORDER_DATE, date.today()), reference):
            return acc.account_id, True
    where = "tus planes de pensiones" if pension else "tus cuentas de valores ni de pensiones"
    raise InputError(f"No hay ninguna orden {reference} en {where} (mira la columna `ref` de `orders --all`).")


def _order_head(account_id: str, pension: bool, detail: OrderDetail) -> dict[str, Any]:
    return {
        "cuenta": account_id,
        "tipo_cuenta": "pensiones" if pension else "valores",
        "ref": detail.order_reference,
        "fecha": detail.order_date.isoformat() if detail.order_date else None,
        "isin": detail.isin,
        "fondo": detail.fund_name,
        "operacion": detail.operation_type,
        "tipo": detail.order_type_name,
        "estado": detail.order_status,
        "importe": detail.gross_amount_eur if detail.gross_amount_eur is not None else detail.cash,
        "participaciones": detail.shares,
        "divisa": detail.currency,
        "vencimiento": detail.expiration_date.isoformat() if detail.expiration_date else None,
    }


def _execution_row(run: OrderExecution) -> dict[str, Any]:
    return {
        "num_operacion": run.operation_number,
        "fecha": run.execution_date.isoformat() if run.execution_date else None,
        "participaciones": run.executed_shares,
        "vl": run.liquidation_value,
        "bruto": run.gross_amount_operation_fund_currency,
        "comisiones": run.commissions,
        "retenciones": run.retentions,
        "neto": run.net_amount_eur,
        "tipo_cambio": run.exchange_rate,
        "resultado": run.tax_result,
    }


# ---- lots ----------------------------------------------------------------------------------------


@app.command()
def lots(isin: IsinArg = None, account: SecAccountOpt = None, fmt: FormatOpt = OutputFormat.table) -> None:
    """Lotes vivos de cada fondo (FIFO): coste, valor y plusvalía latente de cada suscripción que sigue en cartera.

    Es lo que tributaría si reembolsases hoy, no lo ya vendido (eso es `gains`). `fecha_fiscal` es la
    fecha fiscal del lote: tras un traspaso entre fondos es la heredada del fondo de origen, no la del
    traspaso. Solo fondos (no acciones ni ETF). Una petición por fondo, más una por cuenta de valores.
    La última fila es el total.
    """
    with build_client() as client:
        funds = _held_funds(_securities(client, account), isin)
        rows = [
            _lot_row(account_id, inv, fund_name, lot)
            for account_id, inv in funds
            for fund_name, lot in _lots_of(client, account_id, inv)
        ]
    rows.sort(key=lambda r: (r["cuenta"], r["isin"] or "", r["fecha_fiscal"] or ""))
    if rows:
        rows.append(_lots_total(rows))
    render(rows, fmt, title="Lotes vivos (plusvalías latentes)")


def _held_funds(accounts: list[SecuritiesAccount], isin: str | None) -> list[tuple[str, Investment]]:
    held = [(sec.account_id, inv) for sec in accounts for inv in sec.investments if is_fund(inv)]
    if isin is None:
        return held
    chosen = [(a, inv) for a, inv in held if (inv.isin or "").upper() == isin.upper()]
    if not chosen:
        raise InputError(f"No tienes el fondo {isin} en cartera (`lots` solo ve los fondos que sigues teniendo).")
    return chosen


def _lots_of(client: MyInvestorClient, account_id: str, inv: Investment) -> list[tuple[str | None, FundLot]]:
    found = client.fund_lots(account_id, inv.isin or "")
    name = found.fund_name or inv.investment_name
    return [(name, lot) for lot in found.plus_minus_list]


def _lot_row(account_id: str, inv: Investment, fund_name: str | None, lot: FundLot) -> dict[str, Any]:
    return {
        "cuenta": account_id,
        "isin": inv.isin,
        "fondo": fund_name,
        "fecha_fiscal": lot.fiscal_date.isoformat() if lot.fiscal_date else None,
        "operacion": lot.operation_desc or lot.operation_type,
        "participaciones": lot.securities,
        "vl_compra": lot.net_asset_value,
        "coste": lot.investment,
        "valor": lot.market_value,
        "plusvalia": lot.gain,
    }


def _lots_total(rows: list[dict[str, Any]]) -> dict[str, Any]:
    empty = dict.fromkeys(rows[0])
    sums = {key: _sum(r[key] for r in rows) for key in ("coste", "valor", "plusvalia")}
    return {**empty, "cuenta": TOTAL, **sums}


# ---- gains ---------------------------------------------------------------------------------------


@app.command()
def gains(
    isin: IsinArg = None,
    year: Annotated[
        int | None, typer.Option("--year", help="Año fiscal (por fecha de ejecución). Por defecto, el actual.")
    ] = None,
    account: SecAccountOpt = None,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Plusvalías y minusvalías realizadas: reembolsos y traspasos de salida de fondos ejecutados en el año.

    `resultado` es el resultado fiscal de cada ejecución según MyInvestor. Los traspasos entre fondos
    (`traspaso: true`) difieren la tributación: salen en la lista pero no suman en los totales
    (`plusvalias`, `minusvalias` y `total`, las tres últimas filas), que son solo los reembolsos.
    Peticiones: una por cuenta (órdenes desde el 1 de diciembre del año anterior, por si se
    ejecutaron en enero) y una más por cada orden de salida. Solo fondos.
    """
    today = date.today()
    fiscal_year = year or today.year
    if fiscal_year > today.year:
        raise InputError(f"--year {fiscal_year}: ese año todavía no ha empezado.")
    start, end = date(fiscal_year - 1, 12, 1), min(date(fiscal_year, 12, 31), today)
    with build_client() as client:
        ids = [s.account_id for s in _securities(client, account)] if account else client.securities_account_ids()
        outgoing = [(i, o) for i in ids for o in client.orders(i, start, end) if _is_outgoing(o, isin)]
        rows = [
            _gain_row(account_id, o, detail, run)
            for account_id, o in outgoing
            for detail in [client.order_detail(account_id, o.reference)]
            for run in detail.related_operations
            if _execution_year(run, o) == fiscal_year
        ]
    rows.sort(key=lambda r: (r["fecha"], r["ref"]))
    if rows:
        rows.extend(_gains_totals(rows))
    render(rows, fmt, title=f"Reembolsos y traspasos de salida de {fiscal_year}")


def _is_outgoing(order: Order, isin: str | None) -> bool:
    if order.operation_type not in OUTGOING or (order.status or "").upper() in DEAD_STATUSES:
        return False
    return isin is None or (order.isin or "").upper() == isin.upper()


def _execution_day(run: OrderExecution, order: Order) -> date:
    """Execution date; the API may send none (seen on completed pension orders): then the order's."""
    return run.execution_date or order.order_date


def _execution_year(run: OrderExecution, order: Order) -> int:
    return _execution_day(run, order).year


def _gain_row(account_id: str, order: Order, detail: OrderDetail, run: OrderExecution) -> dict[str, Any]:
    """One execution; what the detail lacks comes from the order list."""
    operation = detail.operation_type or order.operation_type
    return {
        "cuenta": account_id,
        "fecha": _execution_day(run, order).isoformat(),
        "ref": detail.order_reference,
        "isin": detail.isin or order.isin,
        "fondo": detail.fund_name or order.fund_name,
        "operacion": operation,
        "traspaso": operation in TRANSFERS_OUT,
        "participaciones": run.executed_shares,
        "vl": run.liquidation_value,
        "bruto": run.gross_amount_operation_fund_currency,
        "comisiones": run.commissions,
        "retenciones": run.retentions,
        "neto": run.net_amount_eur,
        "resultado": run.tax_result,
    }


def _gains_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gains, losses and net of the taxable rows (sales, not transfers)."""
    results = [r["resultado"] for r in rows if not r["traspaso"] and r["resultado"] is not None]
    empty = dict.fromkeys(rows[0])
    return [
        {**empty, "cuenta": label, "resultado": _sum(v for v in results if keep(v))}
        for label, keep in (
            (GAINS, lambda v: v > 0),
            (LOSSES, lambda v: v < 0),
            (TOTAL, lambda v: True),
        )
    ]
