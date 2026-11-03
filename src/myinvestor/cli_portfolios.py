"""`portfolios`: the robo-advisor catalog, one portfolio's target composition, and how yours drift from it."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Any

import typer
from rich.markup import escape

from myinvestor.cli_common import FormatOpt, app, build_client, err
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError
from myinvestor.models import CashAccount, SecuritiesAccount
from myinvestor.output import OutputFormat, format_eur, normalize_row, notices, print_json, render
from myinvestor.portfolios import (
    AnnualReturn,
    Drift,
    Portfolio,
    PortfolioFund,
    catalog_entry,
    drift,
    find_portfolio,
    is_portfolio,
    own_cash,
    portfolio_title,
    profile_label,
    with_profile,
)

CATALOG_TITLE = "Carteras del roboadvisor (rentab: % acumulado del periodo, no anual)"


@app.command()
def portfolios(
    portfolio: Annotated[
        str | None,
        typer.Argument(help="Nombre (`metal`, sin distinguir mayúsculas) o id de una cartera: su composición."),
    ] = None,
    mine: Annotated[
        bool,
        typer.Option(
            "--mine",
            help="Tus carteras contratadas: desviación de cada fondo frente al peso objetivo y rentabilidad anual "
            "(dos peticiones más por cartera).",
        ),
    ] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Carteras del roboadvisor: catálogo con perfil, % RF/RV, rentabilidad y horizonte recomendado.

    Con CARTERA, su composición objetivo (fondos y peso). Con --mine, cada cartera que tienes
    contratada frente a la del catálogo (y, con CARTERA, solo esa). En JSON el detalle es
    `{"cartera": {…}, "fondos": […]}` y --mine, una fila por cartera con `posiciones` y
    `rentabilidad_anual`; en CSV/TSV, una fila por fondo.
    """
    with build_client() as client:
        catalog = client.portfolios()
        if mine:
            _mine(client, catalog, portfolio, fmt)
            return
    if portfolio:
        _detail(_chosen(catalog, portfolio), fmt)
        return
    rows = [_catalog_row(p) for p in catalog]
    render(_with_profile_labels(rows) if fmt is OutputFormat.table else rows, fmt, title=CATALOG_TITLE)


def _chosen(catalog: Sequence[Portfolio], ref: str) -> Portfolio:
    hit = find_portfolio(catalog, ref)
    if hit is None:
        known = ", ".join(f"{p.name} ({p.id})" for p in catalog)
        raise InputError(f"No hay ninguna cartera {ref!r} en el catálogo. Disponibles: {known}.")
    return hit


def _catalog_row(p: Portfolio) -> dict[str, Any]:
    return {
        "id": p.id,
        "nombre": p.name,
        "perfil": p.profile,
        "renta_fija_pct": p.fixed_income,
        "renta_variable_pct": p.variable_income,
        "rentab": p.profitability,
        "rentab_desde": p.profitability_from.isoformat() if p.profitability_from else None,
        "rentab_hasta": p.profitability_to.isoformat() if p.profitability_to else None,
        "horizonte_anos": p.recommended_time_horizon,
        "fondos": len(p.funds),
    }


def _with_profile_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """In the table, `muy agresiva` instead of `VERY_AGGRESSIVE`."""
    return [{**r, "perfil": profile_label(r["perfil"])} for r in rows]


def _fund_row(f: PortfolioFund) -> dict[str, Any]:
    return {
        "isin": f.isin,
        "nombre": f.name,
        "categoria": f.category,
        "categoria_myinvestor": f.my_investor_category,
        "moneda": f.currency,
        "peso": f.percentage,
    }


def _detail(p: Portfolio, fmt: OutputFormat) -> None:
    head = {**_catalog_row(p), "descripcion": p.description, "benchmark": p.cod_benchmark}
    funds = [_fund_row(f) for f in p.funds]
    if fmt is OutputFormat.json:
        print_json({"cartera": normalize_row(head), "fondos": [normalize_row(r) for r in funds]})
    elif fmt is OutputFormat.table:
        render(_with_profile_labels([head]), fmt, title=f"Cartera {with_profile(p.name, p.profile)}")
        render(funds, fmt, title="Composición objetivo")
    else:
        render([{"cartera": p.name, **r} for r in funds], fmt)


# ---- --mine ----------------------------------------------------------------------------------------


def _mine(client: MyInvestorClient, catalog: Sequence[Portfolio], ref: str | None, fmt: OutputFormat) -> None:
    contracted = [s for s in client.securities_accounts() if is_portfolio(s)]
    if ref:
        contracted = [s for s in contracted if _is(s, ref, catalog)]
    cash = own_cash(contracted, client.cash_accounts()) if contracted else {}
    rows = [_mine_row(client, catalog, s, cash) for s in contracted]
    if fmt is OutputFormat.json:
        print_json([normalize_row(_json_mine(r)) for r in rows])
    elif fmt is OutputFormat.table:
        _table_mine(rows)
    else:
        render([{"cuenta": r["cuenta"], "cartera": r["cartera"], **p} for r in rows for p in r["posiciones"]], fmt)


def _is(sec: SecuritiesAccount, ref: str, catalog: Sequence[Portfolio]) -> bool:
    """CARTERA with --mine: the account id, or the catalog portfolio it is (by name or id)."""
    entry = catalog_entry(catalog, sec)
    needle = ref.strip().casefold()
    return needle in (sec.account_id, (sec.portfolio_name or "").casefold()) or (
        entry is not None and needle == str(entry.id)
    )


def _mine_row(
    client: MyInvestorClient, catalog: Sequence[Portfolio], sec: SecuritiesAccount, cash: dict[str, CashAccount]
) -> dict[str, Any]:
    target = catalog_entry(catalog, sec)
    if target is None:
        err.print(
            f"[yellow]{escape(portfolio_title(sec))}:[/yellow] no está en el catálogo de carteras; sin pesos objetivo."
        )
    own = cash.get(sec.account_id)
    return {
        "cuenta": sec.account_id,
        "cartera": sec.portfolio_name,
        "perfil": sec.portfolio_profile_type,
        "titulo": portfolio_title(sec),
        "valor": sec.market_value,
        "efectivo": own.balance if own else None,
        "posiciones": [_drift_row(d) for d in drift(target, sec.investments)],
        "rentabilidad_anual": [_return_row(r) for r in client.portfolio_annual_returns(sec.account_id)],
    }


def _drift_row(d: Drift) -> dict[str, Any]:
    return {
        "isin": d.isin,
        "nombre": d.name,
        "objetivo_pct": d.target,
        "real_pct": d.actual,
        "desviacion_pct": d.deviation,
        "valor": d.value,
    }


def _return_row(r: AnnualReturn) -> dict[str, Any]:
    return {"periodo": r.year, "importe": r.amount, "rentab": r.percentage}


def _json_mine(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: v for k, v in row.items() if k not in ("titulo", "posiciones", "rentabilidad_anual")},
        "posiciones": [normalize_row(p) for p in row["posiciones"]],
        "rentabilidad_anual": [normalize_row(r) for r in row["rentabilidad_anual"]],
    }


def _table_mine(rows: list[dict[str, Any]]) -> None:
    if not rows:
        notices.print("[dim]No tienes ninguna cartera del roboadvisor contratada.[/dim]")
        return
    for r in rows:
        cash = f" · efectivo {format_eur(r['efectivo'])}" if r["efectivo"] is not None else ""
        title = f"{r['titulo']} · cuenta {r['cuenta']} · valor {format_eur(r['valor'] or Decimal(0))}{cash}"
        if r["posiciones"] and all(p["real_pct"] is None for p in r["posiciones"]):
            title += " · sin posiciones todavía"
        render(r["posiciones"], OutputFormat.table, title=title)
        if r["rentabilidad_anual"]:
            render(r["rentabilidad_anual"], OutputFormat.table, title="Rentabilidad anual")
        else:
            notices.print("[dim]Sin rentabilidad anual todavía.[/dim]")
