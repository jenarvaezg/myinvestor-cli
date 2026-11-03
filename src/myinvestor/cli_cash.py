"""Cash-account commands: `transfers` (sent and scheduled) and `interest` (the account's remuneration)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

import typer
from rich.markup import escape

from myinvestor.cli_accounts import DEFAULT_MOVEMENT_DAYS, NOT_FOUND, AccountOpt, _range, match_cash_accounts
from myinvestor.cli_common import FormatOpt, app, build_client, err, now_local
from myinvestor.client import MyInvestorClient
from myinvestor.config import FREE_FLOWS_DAYS
from myinvestor.errors import ApiError, InputError, SignatureError
from myinvestor.interest_settlements import Settlement, credited_on, settlements
from myinvestor.models import CashAccount, Remuneration, SentTransfer
from myinvestor.output import OutputFormat, render
from myinvestor.psd2 import free_start
from myinvestor.sent_transfers import is_scheduled, transfer_row

# The tables leave some columns out; JSON and CSV export every field.
TRANSFER_TABLE_COLUMNS = ("fecha", "cuenta", "importe", "beneficiario", "concepto", "ejecucion", "estado", "proxima")
INTEREST_TABLE_COLUMNS = (
    "cuenta",
    "periodo",
    "fecha_pago",
    "saldo_base",
    "tipo_pct",
    "tae_pct",
    "bruto",
    "retencion",
    "neto",
    "liquidacion_fecha",
    "liquidacion_importe",
)


def _fetch_transfers(client: MyInvestorClient, account: list[str] | None) -> list[tuple[CashAccount, SentTransfer]]:
    chosen = match_cash_accounts(client.cash_accounts(), account)
    return [(acc, t) for acc in chosen for t in client.transfers(acc.account_id)]


def _in_range(t: SentTransfer, start: date, end: date) -> bool:
    return t.emission_date is not None and start <= t.emission_date <= end


@app.command()
def transfers(
    account: AccountOpt = None,
    date_from: Annotated[str | None, typer.Option("--from", help="Desde (AAAA-MM-DD), por fecha de emisión.")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Hasta (AAAA-MM-DD), por defecto hoy.")] = None,
    days: Annotated[
        int | None, typer.Option(min=1, help=f"Últimos N días (por defecto {DEFAULT_MOVEMENT_DAYS}).")
    ] = None,
    scheduled: Annotated[
        bool,
        typer.Option(
            "--scheduled", help="Solo las programadas o periódicas que aún se van a ejecutar, con su próxima fecha."
        ),
    ] = False,
    reference: Annotated[
        str | None,
        typer.Option("--ref", help="Solo la transferencia con esta referencia (columna `ref`), de cualquier fecha."),
    ] = None,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Transferencias emitidas desde tus cuentas de efectivo: beneficiario, IBAN de destino, estado…

    La API no filtra por fecha: se piden todas (una petición por cuenta) y el rango se aplica aquí,
    por fecha de emisión. `importe` va en negativo, como el cargo. Nunca se exporta el DNI del ordenante.
    """
    if scheduled and reference:
        raise InputError("Usa --scheduled o --ref, no ambos.")
    if (scheduled or reference) and (date_from or date_to or days):
        raise InputError("--scheduled y --ref no se combinan con --from/--to/--days: miran todas las fechas.")
    start, end = _range(date_from, date_to, days)
    today = now_local().date()
    with build_client() as client:
        found = _fetch_transfers(client, account)
    if scheduled:
        chosen = [(a, t) for a, t in found if is_scheduled(t, today)]
    elif reference:
        chosen = [(a, t) for a, t in found if t.reference == reference.strip()]
        if not chosen:
            raise InputError(f"--ref: no hay ninguna transferencia emitida con referencia {reference!r}.")
    else:
        chosen = [(a, t) for a, t in found if _in_range(t, start, end)]
    rows = sorted((transfer_row(a, t, today) for a, t in chosen), key=_transfer_order, reverse=True)
    if fmt is OutputFormat.table:
        rows = [{c: r[c] for c in TRANSFER_TABLE_COLUMNS} for r in rows]
    title = "Transferencias programadas" if scheduled else f"Transferencias emitidas {start} → {end}"
    render(rows, fmt, title=title)


def _transfer_order(row: dict[str, Any]) -> tuple[str, str, str]:
    return row["fecha"] or row["proxima"] or row["inicio"] or "", row["cuenta_id"], row["ref"]


@app.command()
def interest(
    account: AccountOpt = None,
    movements: Annotated[
        bool,
        typer.Option(
            "--movements/--no-movements",
            help=f"Cruzar con la última «LIQUIDAC. INTERESES» de los movimientos de los últimos {FREE_FLOWS_DAYS} "
            "días (una petición más por cuenta, nunca pide segundo factor).",
        ),
    ] = True,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Remuneración de cada cuenta de efectivo: la ficha de MyInvestor y la última liquidación de intereses.

    Tipo aplicado (base y promocional), TAE, saldo remunerado, intereses brutos, retención y neto, con
    el mes de la ficha (`periodo`) y la fecha de abono. Los tipos van en % (`2.5` es un 2,5 %). Si
    MyInvestor manda la ficha sin desglose (saldos e intereses a 0, lo normal sin promoción), esos
    campos salen vacíos (`null`), no a 0. Las columnas `liquidacion_*` son el abono real
    «LIQUIDAC. INTERESES» de los movimientos, con el periodo que dice su concepto (p. ej. del 18/09 al
    18/10: sin promoción no va por mes natural).
    """
    today = now_local().date()
    with build_client() as client:
        chosen = match_cash_accounts(client.cash_accounts(), account)
        found = [(acc, r) for acc in chosen if (r := _remuneration(client, acc)) is not None]
        settled = {acc.account_id: _settlements(client, acc, today) for acc, _ in found} if movements else {}
    rows = [_interest_row(acc, r, _settlement_for(acc, r, settled.get(acc.account_id), today)) for acc, r in found]
    if fmt is OutputFormat.table:
        rows = [{c: r[c] for c in INTEREST_TABLE_COLUMNS} for r in rows]
    render(rows, fmt, title="Remuneración de las cuentas")


def _remuneration(client: MyInvestorClient, acc: CashAccount) -> Remuneration | None:
    """The account's last settlement; an account without remuneration (404 or empty) is skipped with a notice."""
    try:
        found = client.remuneration(acc.account_id)
    except ApiError as exc:
        if exc.status != NOT_FOUND:
            raise
        found = None
    if found is None:
        err.print(f"[dim]{escape(acc.alias or acc.account_id)}: MyInvestor no da remuneración para esta cuenta.[/dim]")
    return found


def _settlements(client: MyInvestorClient, acc: CashAccount, today: date) -> list[Settlement] | None:
    """Interest credits of the account in the window served without a second factor; None (warned) on error."""
    try:
        statement = client.statement(acc.account_uuid, free_start(today), today)
    except (ApiError, SignatureError) as exc:
        err.print(
            f"[yellow]{escape(acc.alias or acc.account_id)}:[/yellow] sin cruzar con movimientos, "
            f"falló su consulta ({escape(str(exc))})."
        )
        return None
    return settlements(statement.flows)


def _settlement_for(
    acc: CashAccount, r: Remuneration, found: list[Settlement] | None, today: date
) -> Settlement | None:
    """The credit matching the row: the one on its `paymentDate` if the row has a breakdown, else the last one.

    Says so on stderr when they do not match: a breakdown paid inside the window but absent from
    the movements, or no interest credit at all in the window.
    """
    if found is None:
        return None
    name = escape(acc.alias or acc.account_id)
    if not found:
        err.print(
            f"[dim]{name}: sin «LIQUIDAC. INTERESES» en los movimientos de los últimos {FREE_FLOWS_DAYS} días.[/dim]"
        )
        return None
    paid = r.payment_date if r.has_breakdown else None
    same = credited_on(found, paid) if paid else None
    if paid and same is None and paid >= free_start(today):
        err.print(
            f"[yellow]{name}:[/yellow] MyInvestor da un abono de intereses el {paid} que no está en los "
            f"movimientos; `liquidacion_*` es la última liquidación ({found[0].day})."
        )
    return same or found[0]


def _interest_row(acc: CashAccount, r: Remuneration, settled: Settlement | None) -> dict[str, Any]:
    """`retencion` is negative and `neto` = `bruto` + `retencion`, as in `income`.

    A row without breakdown (`has_breakdown`) gives null balances, results and `fecha_pago`, not
    zeros. Rates of 0 that the web hides (`e !== 0 ? e : undefined`) are null too.
    """
    real = r.has_breakdown
    withheld = -abs(r.tax_charge_amount) if real and r.tax_charge_amount is not None else None
    net = r.final_result + (withheld or Decimal(0)) if real and r.final_result is not None else None
    return {
        "cuenta": acc.alias or acc.account_id,
        "cuenta_id": acc.account_id,
        "periodo": f"{r.remuneration_date:%Y-%m}" if r.remuneration_date else None,
        "fecha_pago": _iso(r.payment_date) if real else None,
        "fecha_oficial": _iso(r.official_remuneration_date),
        "saldo_base": _if(real, r.applicable_base_balance),
        "saldo_promo": _if(real, r.balance_promo),
        "saldo_total": _if(real, r.total_balance),
        "tipo_pct": r.remuneration_percentage,
        "tipo_base_pct": r.base_remuneration_percentage or None,
        "tipo_promo_pct": r.promotion_remuneration_percentage or None,
        "tae_pct": r.calculate_tae_average or None,
        "tae_promo_pct": r.tae_promotion or None,
        "intereses_base": _if(real, r.base_results),
        "intereses_promo": _if(real, r.promo_results),
        "bruto": _if(real, r.final_result),
        "retencion": withheld,
        "neto": net,
        "remuneracion": r.remuneration_type,
        "promocion": r.promotion_code,
        "fin_promocion": _iso(r.end_date_remuneration),
        "liquidacion_fecha": _iso(settled.day) if settled else None,
        "liquidacion_importe": settled.amount if settled else None,
        "liquidacion_desde": _iso(settled.start) if settled else None,
        "liquidacion_hasta": _iso(settled.end) if settled else None,
    }


def _if(real: bool, amount: Decimal | None) -> Decimal | None:
    return amount if real else None


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None
