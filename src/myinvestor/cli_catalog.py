"""Catalog commands: `fund-filters` and `pension-plans`, and the searcher options `funds` shares with them.

`fund-filters` is its own command and not `funds filters`: `funds` takes a free-text QUERY, and a
group would read `funds filters` (or any query that names a subcommand) as a subcommand.
"""

from __future__ import annotations

from dataclasses import replace
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
from myinvestor.models import PensionPlanSummary
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
FundTypeOpt = Annotated[str | None, typer.Option("--type", help="indexados o activos.")]
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


def pension_plan_row(plan: PensionPlanSummary) -> dict[str, Any]:
    returns = plan.annualized_profitability
    return {
        "dgs": plan.dgs_code,
        "nombre": plan.name,
        "gestora": plan.managing_entity,
        "categoria": plan.category,
        "ter": plan.ter,
        "ytd": returns.ytd if returns else None,
        "1a": returns.one_year if returns else None,
        "3a": returns.three_year if returns else None,
        "5a": returns.five_year if returns else None,
    }


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
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Busca en el catálogo de planes de pensiones de MyInvestor: TER y rentabilidades anualizadas."""
    low_ter, high_ter = ter_range(min_ter, max_ter)
    filters = CatalogFilters(
        asset_type=code_or_none(asset_type), min_ter=low_ter, max_ter=high_ter, risk=risk_or_none(risk), rating=rating
    )
    with build_client() as client:
        filters = replace(filters, manager=resolve_filter(client, Domain.pension_plans, FilterKind.managers, manager))
        result = client.pension_plan_catalog(query, sort=sort, page=page, limit=limit, filters=filters)
    suffix = f" ({result.total} en total)" if result.total is not None else ""
    render([pension_plan_row(p) for p in result.plans], fmt, title=f"Catálogo de planes de pensiones{suffix}")
