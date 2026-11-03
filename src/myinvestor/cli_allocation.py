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
    unlocated,
    untranslated_labels,
)
from myinvestor.cli_common import FormatOpt, app, build_client, err, iso_moment, now_local
from myinvestor.cli_fiscal import FiscalOpt, fund_costs, investment_cost, plan_cost, plan_costs
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError
from myinvestor.fiscal import FISCAL_KEYS
from myinvestor.models import FundSheet, SecuritiesAccount
from myinvestor.output import OutputFormat, render

TITLES = {
    Grouping.asset: "tipo de activo",
    Grouping.geo: "zona",
    Grouping.category: "categoría",
    Grouping.product: "producto",
    Grouping.account: "cuenta",
}
# Pension-plan sheets carry an asset type, a category and a name (the region comes from the last two):
# other groupings skip that request.
PLAN_SHEET_GROUPINGS = frozenset({Grouping.asset, Grouping.geo, Grouping.category})


def fund_sheets(client: MyInvestorClient, securities: list[SecuritiesAccount]) -> dict[str, FundSheet]:
    """One sheet per distinct fund ISIN (stocks and ETFs have none)."""
    isins = dict.fromkeys(inv.isin for sec in securities for inv in sec.investments if is_fund(inv))
    return {isin: client.fund_sheet(isin) for isin in isins if isin}


def _pension_holdings(client: MyInvestorClient, by: Grouping, *, fiscal: bool) -> list[Holding]:
    classify = by in PLAN_SHEET_GROUPINGS
    accounts = client.pension_accounts()
    plans = [(account, plan) for account in accounts for plan in client.pension_plans(account.account_id)]
    sheets = [client.pension_plan_sheet(plan.dgs_code) if classify else None for _, plan in plans]
    costs = plan_costs(client, accounts) if fiscal else {}
    return [
        from_pension(account, plan, sheet, plan_cost(account.account_id, plan, costs) if fiscal else None)
        for (account, plan), sheet in zip(plans, sheets, strict=True)
    ]


def collect(client: MyInvestorClient, by: Grouping, *, include_cash: bool, fiscal: bool = False) -> list[Holding]:
    """Every position, classified; with `fiscal`, with its tax cost (each fund's lots, the plans' history)."""
    securities = client.securities_accounts()
    sheets = fund_sheets(client, securities)
    positions = [(sec, inv) for sec in securities for inv in sec.investments]
    costs = fund_costs(client, ((sec.account_id, inv) for sec, inv in positions)) if fiscal else {}
    holdings = [
        from_investment(
            sec, inv, sheets.get(inv.isin or ""), investment_cost(sec.account_id, inv, costs) if fiscal else None
        )
        for sec, inv in positions
    ]
    holdings += _pension_holdings(client, by, fiscal=fiscal)
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


def _report_untranslated(labels: list[str]) -> None:
    if labels:
        names = ", ".join(f"«{escape(label)}»" for label in labels)
        err.print(f"[dim]Tipos de activo sin traducir (van a sin_clasificar): {names}.[/dim]")


def _report_unlocated(names: list[str]) -> None:
    if names:
        listed = ", ".join(f"«{escape(name)}»" for name in names)
        err.print(f"[dim]Sin zona conocida (van a sin_clasificar): {listed}.[/dim]")


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
            help='JSON con los pesos objetivo por clave, en %, que sumen 100: {"renta_variable": 80, …}.',
        ),
    ] = None,
    fiscal: FiscalOpt = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Reparto de la cartera (fondos, ETF, acciones y planes) con el peso de cada clave y el TER medio.

    `ter` es la media ponderada por valor de las posiciones con TER conocido (los fondos); la fila
    `total` lleva el de toda la cartera. Con `--by asset` la clave es una clase canónica (`renta_variable`,
    `renta_fija`, `monetario`, `mixto`, `materias_primas`…) y `origen`, las etiquetas de la API de las
    que sale. Con `--by geo`, `origen` dice qué decidió la zona (`geoZone`, `nombre: S&P 500`,
    `categoría: GLOBAL`, `ISIN: US`); las materias primas y el efectivo van a `no_aplica`. Lo que no
    tiene clasificación va a `sin_clasificar`. Con
    `--target`, `objetivo_pct`, `desviacion_pct` y `aportar`: cuánto aportar a cada clave para llegar
    al objetivo sin vender. En JSON/CSV/TSV cada fila lleva además `consultado_en`.
    Con `--fiscal`, `coste_fiscal` (el coste heredado en los traspasos; en el efectivo, su saldo),
    `ganancia_real` y `rentab_real` de cada clave; `null` si falta el de alguna de sus posiciones.
    """
    targets = load_target(target) if target else None
    consulted = now_local()
    with build_client() as client:
        holdings = collect(client, by, include_cash=include_cash, fiscal=fiscal)
    if by is Grouping.asset:
        _report_untranslated(untranslated_labels(holdings))
    if by is Grouping.geo:
        _report_unlocated(unlocated(holdings))
    rows = group(holdings, by)
    total = total_row(holdings, by, TOTAL.upper() if fmt is OutputFormat.table else TOTAL)
    if targets is not None:
        _report_untargeted(unbalanced_keys(rows, targets))
        rows = rebalance(rows, targets, by)
        total = target_total(rows, total)
    rows = [*rows, total]
    if not fiscal:
        rows = [{k: v for k, v in r.items() if k not in FISCAL_KEYS} for r in rows]
    if fmt is OutputFormat.table:
        render(rows, fmt, title=f"Reparto por {TITLES[by]} a {consulted:%Y-%m-%d %H:%M}")
        return
    render([{**r, "consultado_en": iso_moment(consulted)} for r in rows], fmt)
