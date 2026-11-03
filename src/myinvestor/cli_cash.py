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
from myinvestor.errors import ApiError, InputError
from myinvestor.models import CashAccount, Remuneration, SentTransfer
from myinvestor.output import OutputFormat, render
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
def interest(account: AccountOpt = None, fmt: FormatOpt = OutputFormat.table) -> None:
    """Remuneración de cada cuenta de efectivo: la última liquidación de intereses.

    Tipo aplicado (base y promocional), TAE, saldo remunerado, intereses brutos, retención y neto, con
    el periodo liquidado y la fecha de abono. Los tipos van en % (`2.5` es un 2,5 %).
    """
    with build_client() as client:
        found = [(acc, _remuneration(client, acc)) for acc in match_cash_accounts(client.cash_accounts(), account)]
    rows = [_interest_row(acc, r) for acc, r in found if r is not None]
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


def _interest_row(acc: CashAccount, r: Remuneration) -> dict[str, Any]:
    """`retencion` is negative and `neto` = `bruto` + `retencion`, as in `income`."""
    withheld = -abs(r.tax_charge_amount) if r.tax_charge_amount is not None else None
    net = r.final_result + (withheld or Decimal(0)) if r.final_result is not None else None
    return {
        "cuenta": acc.alias or acc.account_id,
        "cuenta_id": acc.account_id,
        "periodo": f"{r.remuneration_date:%Y-%m}" if r.remuneration_date else None,
        "fecha_pago": _iso(r.payment_date),
        "fecha_oficial": _iso(r.official_remuneration_date),
        "saldo_base": r.applicable_base_balance,
        "saldo_promo": r.balance_promo,
        "saldo_total": r.total_balance,
        "tipo_pct": r.remuneration_percentage,
        "tipo_base_pct": r.base_remuneration_percentage,
        "tipo_promo_pct": r.promotion_remuneration_percentage,
        "tae_pct": r.calculate_tae_average,
        "tae_promo_pct": r.tae_promotion,
        "intereses_base": r.base_results,
        "intereses_promo": r.promo_results,
        "bruto": r.final_result,
        "retencion": withheld,
        "neto": net,
        "remuneracion": r.remuneration_type,
        "promocion": r.promotion_code,
        "fin_promocion": _iso(r.end_date_remuneration),
    }


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None
