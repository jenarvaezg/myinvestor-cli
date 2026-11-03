"""Catalog commands: `fund-filters` and `pension-plans`, and the searcher options `funds` shares with them.

`fund-filters` is its own command and not `funds filters`: `funds` takes a free-text QUERY, and a
group would read `funds filters` (or any query that names a subcommand) as a subcommand.
"""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from decimal import Decimal
from typing import Annotated, Any

import typer

from myinvestor.catalog import (
    PENSION_FILTERS,
    CatalogFilters,
    Domain,
    FilterKind,
    api_code,
    clean_values,
    match_value,
    parse_risk,
    ter_range,
)
from myinvestor.cli_common import FormatOpt, app, build_client
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError
from myinvestor.models import Commission, PensionPlanSheet, PensionPlanSummary
from myinvestor.output import OutputFormat, render

QueryOpt = Annotated[
    str | None, typer.Argument(help="Texto libre tal como aparece en el nombre («S&P 500», no «sp500»), ISIN…")
]
SortOpt = Annotated[str, typer.Option(help="Orden `campo,asc|desc` (ter, name, liquidationValue…).")]
LimitOpt = Annotated[int, typer.Option("--limit", "-n", min=1, max=200, help="Máximo de resultados.")]
CategoryOpt = Annotated[
    str | None, typer.Option(help="Categoría Morningstar (`fund-filters categories`; sin distinguir mayúsculas).")
]
ManagerOpt = Annotated[str | None, typer.Option(help="Gestora (`fund-filters managers`; sin distinguir mayúsculas).")]
CurrencyOpt = Annotated[str | None, typer.Option(help="Divisa del fondo: EUR, USD…")]
AssetTypeOpt = Annotated[
    str | None, typer.Option("--asset-type", help="Tipo de activo: variable_income, fixed_income, mixed…")
]
RegionOpt = Annotated[str | None, typer.Option(help="Zona geográfica: global, europe, united_states…")]
MinTerOpt = Annotated[float | None, typer.Option("--min-ter", min=0, help="TER mínimo, en %.")]
MaxTerOpt = Annotated[float | None, typer.Option("--max-ter", min=0, help="TER máximo, en % (0.3 = 0,30 %).")]
RiskOpt = Annotated[str | None, typer.Option(help="Indicador de riesgo de 1 a 7: un nivel (`5`) o un rango (`3-6`).")]
RatingOpt = Annotated[int | None, typer.Option(min=1, max=5, help="Estrellas Morningstar (1-5).")]


def resolve_filter(client: MyInvestorClient, domain: Domain, kind: FilterKind, value: str | None) -> str | None:
    """The API's exact spelling of a free-text filter value (one more request); None without a value."""
    if value is None:
        return None
    return match_value(value, client.catalog_filter(domain, kind), kind, domain)


def code_or_none(text: str | None) -> str | None:
    return api_code(text) if text is not None else None


def risk_or_none(text: str | None) -> tuple[int, int] | None:
    return parse_risk(text) if text is not None else None


def _filter_rows(client: MyInvestorClient, domain: Domain, kinds: tuple[FilterKind, ...]) -> list[dict[str, Any]]:
    return [
        {"filtro": kind.value, "opcion": kind.option, "nombre": value}
        for kind in kinds
        for value in clean_values(client.catalog_filter(domain, kind))
    ]


@app.command("fund-filters")
def fund_filters(
    kind: Annotated[FilterKind | None, typer.Argument(help="Qué filtro listar. Sin él: todos.")] = None,
    pension: Annotated[
        bool, typer.Option("--pension", help="Los del buscador de planes de pensiones (gestoras y tipos de activo).")
    ] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Valores válidos de los filtros del buscador de fondos (o de planes) y la opción que los usa."""
    domain = Domain.pension_plans if pension else Domain.funds
    available = PENSION_FILTERS if pension else tuple(FilterKind)
    if kind is not None and kind not in available:
        raise InputError(f"Los planes de pensiones solo se filtran por {' y '.join(k.value for k in available)}.")
    with build_client() as client:
        rows = _filter_rows(client, domain, (kind,) if kind else available)
    what = "planes de pensiones" if pension else "fondos"
    render(rows, fmt, title=f"Filtros del buscador de {what}")


def pension_plan_row(plan: PensionPlanSummary | PensionPlanSheet) -> dict[str, Any]:
    """A catalog row or a plan's detail; only the detail has fees (`com_gestion`, `com_deposito`)."""
    returns = plan.annualized_profitability
    sheet = isinstance(plan, PensionPlanSheet)
    commissions = plan.commission_list if sheet else []
    return {
        "dgs": plan.dgs_code,
        "nombre": plan.name,
        "gestora": plan.management_entity if sheet else plan.managing_entity,
        "categoria": plan.category,
        "ter": plan.ter if plan.ter is not None else commission(commissions, "(ter)"),
        "com_gestion": commission(commissions, "gesti"),
        "com_deposito": commission(commissions, "dep"),
        "ytd": returns.ytd if returns else None,
        "1a": returns.one_year if returns else None,
        "3a": returns.three_year if returns else None,
        "5a": returns.five_year if returns else None,
    }


def commission(commissions: list[Commission], word: str) -> Decimal | None:
    """Percentage of the first fee whose name has `word` (accents and case ignored)."""
    for fee in commissions:
        if word in fold(fee.name or ""):
            return fee.percentage
    return None


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def plan_codes(client: MyInvestorClient) -> list[str]:
    """DGS codes of the plans held, without duplicates."""
    codes = [
        plan.dgs_code for account in client.pension_accounts() for plan in client.pension_plans(account.account_id)
    ]
    return list(dict.fromkeys(code.upper() for code in codes))


def matches(sheet: PensionPlanSheet, query: str | None) -> bool:
    return not query or fold(query) in fold(f"{sheet.dgs_code or ''} {sheet.name or ''}")


@app.command("pension-plans")
def pension_plans(
    query: QueryOpt = None,
    manager: ManagerOpt = None,
    asset_type: AssetTypeOpt = None,
    min_ter: MinTerOpt = None,
    max_ter: MaxTerOpt = None,
    risk: RiskOpt = None,
    rating: RatingOpt = None,
    sort: SortOpt = "ter,asc",
    limit: LimitOpt = 20,
    page: Annotated[int, typer.Option(min=1, help="Página (desde 1).")] = 1,
    mine: Annotated[
        bool,
        typer.Option(
            "--mine", help="Los planes que tienes, leídos de su ficha, con sus comisiones. Una petición por plan."
        ),
    ] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Busca en el catálogo de planes de pensiones de MyInvestor: TER y rentabilidades anualizadas.

    Con --mine, los planes que tienes, leídos de su ficha: `ter` es su «Coste total (TER)» y salen
    `com_gestion` y `com_deposito` (en el buscador, null). QUERY filtra entonces por nombre o código
    sin distinguir mayúsculas ni tildes, y no valen los filtros del buscador ni --page.
    """
    if mine:
        if any(v is not None for v in (manager, asset_type, min_ter, max_ter, risk, rating)) or page != 1:
            raise InputError("--mine no admite los filtros del buscador ni --page: solo QUERY y -n.")
        with build_client() as client:
            sheets = [client.pension_plan_sheet(code) for code in plan_codes(client)]
        rows = [pension_plan_row(s) for s in sheets if matches(s, query)][:limit]
        render(rows, fmt, title="Tus planes de pensiones")
        return
    low_ter, high_ter = ter_range(min_ter, max_ter)
    filters = CatalogFilters(
        asset_type=code_or_none(asset_type), min_ter=low_ter, max_ter=high_ter, risk=risk_or_none(risk), rating=rating
    )
    with build_client() as client:
        filters = replace(filters, manager=resolve_filter(client, Domain.pension_plans, FilterKind.managers, manager))
        result = client.pension_plan_catalog(query, sort=sort, page=page - 1, limit=limit, filters=filters)
    suffix = f" ({result.total} en total)" if result.total is not None else ""
    render([pension_plan_row(p) for p in result.plans], fmt, title=f"Catálogo de planes de pensiones{suffix}")
