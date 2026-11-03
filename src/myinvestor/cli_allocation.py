"""`allocation`: how the portfolio is split by asset type, region, category, product or account."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from myinvestor.allocation import (
    TOTAL,
    Grouping,
    Holding,
    from_cash,
    from_investment,
    from_pension,
    group,
    is_fund,
    parse_target,
    portfolios_by_cash_account,
    rebalance,
    target_total,
    total_row,
    unbalanced_keys,
)
from myinvestor.cli_common import FormatOpt, app, build_client, err, iso_moment, now_local
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError
from myinvestor.models import FundSheet, SecuritiesAccount
from myinvestor.output import OutputFormat, render

TITLES = {
    Grouping.asset: "tipo de activo",
    Grouping.geo: "zona",
    Grouping.category: "categoría",
    Grouping.product: "producto",
    Grouping.account: "cuenta",
}
# Pension-plan sheets only carry an asset type and a category: other groupings skip that request.
PLAN_SHEET_GROUPINGS = frozenset({Grouping.asset, Grouping.category})


def fund_sheets(client: MyInvestorClient, securities: list[SecuritiesAccount]) -> dict[str, FundSheet]:
    """One sheet per distinct fund ISIN (stocks and ETFs have none)."""
    isins = dict.fromkeys(inv.isin for sec in securities for inv in sec.investments if is_fund(inv))
    return {isin: client.fund_sheet(isin) for isin in isins if isin}


def _pension_holdings(client: MyInvestorClient, by: Grouping) -> list[Holding]:
    classify = by in PLAN_SHEET_GROUPINGS
    return [
        from_pension(account, plan, client.pension_plan_sheet(plan.dgs_code) if classify else None)
        for account in client.pension_accounts()
        for plan in client.pension_plans(account.account_id)
    ]


def collect(client: MyInvestorClient, by: Grouping, *, include_cash: bool) -> list[Holding]:
    securities = client.securities_accounts()
    sheets = fund_sheets(client, securities)
    holdings = [from_investment(sec, inv, sheets.get(inv.isin or "")) for sec in securities for inv in sec.investments]
    holdings += _pension_holdings(client, by)
    if include_cash:
        portfolios = portfolios_by_cash_account(securities)
        holdings += [from_cash(acc, portfolios.get(acc.account_id)) for acc in client.cash_accounts()]
    return holdings


def load_target(path: Path) -> dict[str, Decimal]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"No se puede leer el objetivo {path}: {exc}.") from exc
    try:
        data = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise InputError(f"El objetivo {path} no es JSON válido: {exc.msg} (línea {exc.lineno}).") from exc
    return parse_target(data)


def _report_untargeted(keys: list[str]) -> None:
    if keys:
        names = ", ".join(f"«{escape(k)}»" for k in keys)
        err.print(f"[dim]Sin peso objetivo (fuera del rebalanceo, no se venden): {names}.[/dim]")


@app.command()
def allocation(
    by: Annotated[
        Grouping,
        typer.Option("--by", help="Agrupar por tipo de activo, zona, categoría, producto o cuenta."),
    ] = Grouping.asset,
    include_cash: Annotated[
        bool, typer.Option("--include-cash", help="Contar el efectivo de las cuentas como la clase «efectivo».")
    ] = False,
    target: Annotated[
        Path | None,
        typer.Option(
            "--target",
            help='JSON con los pesos objetivo por clave, en %, que sumen 100: {"VARIABLE_INCOME": 80, …}.',
        ),
    ] = None,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Reparto de la cartera (fondos, ETF, acciones y planes) con el peso de cada clave y el TER medio.

    `ter` es la media ponderada por valor de las posiciones con TER conocido (los fondos); la fila
    `total` lleva el de toda la cartera. Lo que no tiene clasificación va a «sin clasificar». Con
    `--target`, `objetivo_pct`, `desviacion_pct` y `aportar`: cuánto aportar a cada clave para llegar
    al objetivo sin vender. En JSON/CSV/TSV cada fila lleva además `consultado_en`.
    """
    targets = load_target(target) if target else None
    consulted = now_local()
    with build_client() as client:
        holdings = collect(client, by, include_cash=include_cash)
    rows = group(holdings, by)
    total = total_row(holdings, by, TOTAL.upper() if fmt is OutputFormat.table else TOTAL)
    if targets is not None:
        _report_untargeted(unbalanced_keys(rows, targets))
        rows = rebalance(rows, targets, by)
        total = target_total(rows, total)
    rows = [*rows, total]
    if fmt is OutputFormat.table:
        render(rows, fmt, title=f"Reparto por {TITLES[by]} a {consulted:%Y-%m-%d %H:%M}")
        return
    render([{**r, "consultado_en": iso_moment(consulted)} for r in rows], fmt)
