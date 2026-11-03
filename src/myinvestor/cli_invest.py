"""Investment commands: `holdings`, `orders`, `pensions`, `history`, `contributions`, `fund`, `funds`, `feed`."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

import typer

from myinvestor.allocation import is_fund, share
from myinvestor.catalog import CatalogFilters, Domain, FilterKind, ter_range
from myinvestor.cli_catalog import (
    AssetTypeOpt,
    CategoryOpt,
    CurrencyOpt,
    LimitOpt,
    ManagerOpt,
    MaxTerOpt,
    MinTerOpt,
    QueryOpt,
    RatingOpt,
    RegionOpt,
    RiskOpt,
    SortOpt,
    code_or_none,
    resolve_filter,
    risk_or_none,
)
from myinvestor.cli_common import FormatOpt, app, build_client, iso_moment, now_local, parse_day
from myinvestor.cli_state import MarkOpt, StateOpt, record, state_file
from myinvestor.client import MyInvestorClient, Period
from myinvestor.errors import InputError
from myinvestor.models import Contribution, Investment, Order, PensionAccount, PensionPlanPosition, SecuritiesAccount
from myinvestor.output import OutputFormat, print_json, render
from myinvestor.state import StateKind, changed, snapshot_marks

BROKER = "BROKER"  # `investmentProductType` of stocks and ETFs; every other type is a fund


class HoldingKind(StrEnum):
    fondos = "fondos"
    broker = "broker"


PENSION_CODE = re.compile(r"^N\d{4}$")  # DGS code of a pension plan (`N1234`)
EARLIEST_ORDER_DATE = date(2010, 1, 1)

SecAccountOpt = Annotated[
    list[str] | None,
    typer.Option("--account", "-a", help="Cuenta de valores (id o código). Repetible; por defecto todas."),
]


def _securities(client: MyInvestorClient, refs: list[str] | None) -> list[SecuritiesAccount]:
    accounts = {i: client.securities_account(i) for i in client.securities_account_ids()}
    if not refs:
        return list(accounts.values())
    chosen: list[SecuritiesAccount] = []
    for ref in refs:
        hit = accounts.get(ref) or next(
            (a for a in accounts.values() if ref in (a.cod_account, a.external_cod_account)), None
        )
        if hit is None:
            raise InputError(f"Cuenta de valores desconocida: {ref}. Disponibles: {', '.join(accounts)}.")
        if hit not in chosen:
            chosen.append(hit)
    return chosen


def _holding_row(account: SecuritiesAccount, inv: Investment) -> dict[str, Any]:
    return {
        "cuenta": account.account_id,
        "tipo": inv.investment_product_type,
        "isin": inv.isin,
        "nombre": inv.investment_name,
        "participaciones": inv.shares,
        "coste_medio": inv.average_cost,
        "vl": inv.liquidation_value,
        "fecha_vl": inv.liquidation_value_date.isoformat() if inv.liquidation_value_date else None,
        "invertido": inv.initial_investment,
        "valor": inv.market_value,
        "ganancia": inv.profit,
        "rentab": inv.total_return,
    }


def _with_weights(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`peso`: share of each position in the value of the positions listed."""
    total = sum((r["valor"] or Decimal(0) for r in rows), Decimal(0))
    return [{**r, "peso": share(r["valor"] or Decimal(0), total)} for r in rows]


@app.command()
def holdings(
    account: SecAccountOpt = None,
    kind: Annotated[
        HoldingKind | None, typer.Option(help="Solo un tipo: fondos (INDEXED_FUND…) o broker (acciones/ETF).")
    ] = None,
    ter: Annotated[
        bool, typer.Option("--ter", help="Añadir el TER de cada fondo (una petición más por fondo distinto).")
    ] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Fondos, ETF y acciones de las cuentas de valores, con coste, valor, rentabilidad y peso.

    `peso` es el % de cada posición sobre el valor de las listadas (tras `-a` y `--kind`). Con
    `--ter`, el TER de la ficha de cada fondo (vacío en acciones y ETF).
    En JSON/CSV/TSV cada fila lleva además `consultado_en` (fecha y hora de la consulta, con zona).
    """
    consulted = now_local()
    with build_client() as client:
        positions = [(sec, inv) for sec in _securities(client, account) for inv in sec.investments]
        if kind:
            positions = [
                (s, i) for s, i in positions if (i.investment_product_type == BROKER) == (kind is HoldingKind.broker)
            ]
        isins = dict.fromkeys(i.isin for _, i in positions if is_fund(i)) if ter else {}
        ters = {isin: client.fund_sheet(isin).ter for isin in isins if isin}
    positions.sort(key=lambda p: p[1].market_value or 0, reverse=True)
    rows = _with_weights([_holding_row(sec, inv) for sec, inv in positions])
    if ter:
        rows = [{**r, "ter": ters.get(inv.isin or "")} for r, (_, inv) in zip(rows, positions, strict=True)]
    if fmt is OutputFormat.table:
        render(rows, fmt, title=f"Cartera a {consulted:%Y-%m-%d %H:%M}")
        return
    render([{**r, "consultado_en": iso_moment(consulted)} for r in rows], fmt)


def _order_row(order: Order) -> dict[str, Any]:
    return {
        "fecha": order.order_date.isoformat(),
        "cuenta": order.account_id,
        "ref": order.reference,
        "producto": order.product,
        "nombre": order.fund_name,
        "operacion": order.operation_type,
        "estado": order.status,
        "importe": order.cash,
        "participaciones": order.shares,
    }


@app.command()
def orders(
    account: SecAccountOpt = None,
    date_from: Annotated[str | None, typer.Option("--from", help="Desde (AAAA-MM-DD).")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Hasta (AAAA-MM-DD).")] = None,
    all_orders: Annotated[
        bool, typer.Option("--all", help="Todo el histórico (sin fechas: solo las recientes).")
    ] = False,
    pension: Annotated[
        bool, typer.Option("--pension", help="Aportaciones a planes de pensiones en vez de valores.")
    ] = False,
    status: Annotated[str | None, typer.Option(help="Filtrar por estado (IN_PROGRESS, EXECUTED…).")] = None,
    new: Annotated[
        bool,
        typer.Option("--new", help="Solo las órdenes nuevas o con otro estado que la vez anterior con --new."),
    ] = False,
    state_path: StateOpt = None,
    mark: MarkOpt = True,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Órdenes de compra/venta/traspaso de fondos y acciones, o de planes de pensiones.

    Con --new solo salen las que no se habían mostrado con --new o han cambiado de estado desde
    entonces (p. ej. IN_PROGRESS → EXECUTED); se guarda `ref → estado` en el estado local.
    """
    start = parse_day(date_from, "--from") or (EARLIEST_ORDER_DATE if all_orders else None)
    end = parse_day(date_to, "--to")
    if start and end and start > end:
        raise InputError("--from es posterior a --to.")
    with build_client() as client:
        if pension:
            ids = _pension_ids(client, account)
            found = [o for i in ids for o in client.pension_orders(i)]
            found = [o for o in found if (not start or o.order_date >= start) and (not end or o.order_date <= end)]
        else:
            ids = [s.account_id for s in _securities(client, account)] if account else client.securities_account_ids()
            found = [o for i in ids for o in client.orders(i, start, end)]
    if status:
        found = [o for o in found if (o.status or "").upper() == status.upper()]
    found.sort(key=lambda o: (o.order_date, o.reference), reverse=True)
    if not new:
        render([_order_row(o) for o in found], fmt, title="Órdenes")
        return
    kind = StateKind.pension_orders if pension else StateKind.orders
    file = state_file(state_path)
    fresh = set(changed(((o.reference, o.status) for o in found), file.load().marked.snapshot(kind)))
    found = [o for o in found if o.reference in fresh]
    render([_order_row(o) for o in found], fmt, title="Órdenes nuevas o con cambio de estado")
    record(file, snapshot_marks(kind, [(o.reference, o.status) for o in found]), mark=mark, kind=kind)


@app.command()
def pensions(fmt: FormatOpt = OutputFormat.table) -> None:
    """Planes de pensiones: participaciones, coste, valor y rentabilidad."""
    with build_client() as client:
        rows = [pension_row(acc, p) for acc in client.pension_accounts() for p in client.pension_plans(acc.account_id)]
    render(rows, fmt, title="Planes de pensiones")


def pension_row(acc: PensionAccount, p: PensionPlanPosition) -> dict[str, Any]:
    return {
        "cuenta": acc.account_id,
        "codigo": p.dgs_code,
        "nombre": p.name,
        "participaciones": p.shares,
        "coste_medio": p.average_cost,
        "vl": p.liquidation_value,
        "fecha_vl": p.liquidation_value_date.isoformat() if p.liquidation_value_date else None,
        "invertido": p.total_invested,
        "valor": p.market_value,
        "ganancia": p.profit,
        "rentab": p.total_return,
    }


PERIODS = {p.name: p for p in Period}


def _pension_ids(client: MyInvestorClient, refs: list[str] | None) -> list[str]:
    ids = [a.account_id for a in client.pension_accounts()]
    unknown = [r for r in refs or [] if r not in ids]
    if unknown:
        raise InputError(f"Cuenta de pensiones desconocida: {', '.join(unknown)}. Disponibles: {', '.join(ids)}.")
    return list(dict.fromkeys(refs)) if refs else ids


def _history_targets(client: MyInvestorClient, product: str | None, account: str | None) -> list[tuple[str, bool]]:
    """(account id, is pension) pairs whose series to fetch; a code like N1234 means pension plans."""
    pension_ids = [a.account_id for a in client.pension_accounts()]
    wants_pension = bool(product and PENSION_CODE.match(product.upper()))
    if account:
        if account in pension_ids:
            if product and not wants_pension:
                raise InputError(f"{product} es un ISIN pero {account} es una cuenta de pensiones.")
            return [(account, True)]
        [sec] = _securities(client, [account])
        if wants_pension:
            raise InputError(f"{product} es un plan de pensiones pero {account} es una cuenta de valores.")
        return [(sec.account_id, False)]
    if wants_pension:
        return [(i, True) for i in pension_ids]
    securities = [(i, False) for i in client.securities_account_ids()]
    return securities if product else securities + [(i, True) for i in pension_ids]


@app.command()
def history(
    product: Annotated[str | None, typer.Argument(help="ISIN o código de plan (N1234). Sin él: todos.")] = None,
    account: Annotated[str | None, typer.Option("--account", "-a", help="Cuenta de valores o de pensiones.")] = None,
    period: Annotated[
        str, typer.Option(help=f"Rango: {', '.join(PERIODS)} (la API no da más de un año).")
    ] = Period.six_months.name,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Evolución diaria de cada posición: valor, invertido, rentabilidad y participaciones."""
    if period not in PERIODS:
        raise InputError(f"--period {period!r} no válido: {', '.join(PERIODS)}.")
    rows: list[dict[str, Any]] = []
    with build_client() as client:
        targets = _history_targets(client, product, account)
        for account_id, is_pension in targets:
            series = client.position_history(account_id, PERIODS[period], pension=is_pension)
            for code, points in series.items():
                if product and code.upper() != product.upper():
                    continue
                rows.extend(
                    {
                        "fecha": p.date.isoformat(),
                        "cuenta": account_id,
                        "producto": code,
                        "valor": p.market_value,
                        "invertido": p.investment_added_value,
                        "rentab": p.return_value,
                        "participaciones": p.total_shares,
                        "coste_medio": p.avg_position_cost,
                    }
                    for p in points
                )
    rows.sort(key=lambda r: (r["producto"], r["fecha"]))
    render(rows, fmt, title=f"Evolución ({period})")


def contribution_row(c: Contribution) -> dict[str, Any]:
    frame = c.contribution_time_frame
    return {
        "id": c.contribution_id,
        "estado": c.status,
        "producto": c.isin,
        "nombre": c.fund_name,
        "importe": c.amount,
        "frecuencia": frame.recurrence if frame else None,
        "proxima": frame.next_execution_date.isoformat() if frame and frame.next_execution_date else None,
        "destino": c.to_account_type,
    }


@app.command()
def contributions(
    all_contributions: Annotated[bool, typer.Option("--all", help="Incluir las canceladas.")] = False,
    new: Annotated[
        bool,
        typer.Option("--new", help="Solo las nuevas o con otro estado o próxima fecha que la vez anterior con --new."),
    ] = False,
    state_path: StateOpt = None,
    mark: MarkOpt = True,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Aportaciones periódicas programadas a fondos y planes."""
    with build_client() as client:
        found = client.contributions()
    if not all_contributions:
        found = [c for c in found if c.status not in ("CANCELED", "CANCELLED")]
    rows = [contribution_row(c) for c in found]
    if not new:
        render(rows, fmt, title="Aportaciones periódicas")
        return
    file = state_file(state_path)
    items = [(r["id"], {"estado": r["estado"], "proxima": r["proxima"]}) for r in rows]
    fresh = set(changed(items, file.load().marked.snapshot(StateKind.contributions)))
    render([r for r in rows if r["id"] in fresh], fmt, title="Aportaciones periódicas nuevas o con cambios")
    kind = StateKind.contributions
    record(file, snapshot_marks(kind, [i for i in items if i[0] in fresh]), mark=mark, kind=kind)


FUND_FIELDS = (
    "name",
    "isinCode",
    "dgsCode",
    "productType",
    "category",
    "morningstarCategory",
    "myinvestorCategory",
    "assetType",
    "geoZone",
    "currency",
    "ter",
    "riskIndicator",
    "morningstarRating",
    "mstarRating",
    "dividendType",
    "currencyHedging",
    "currencyHedged",
    "liquidationValue",
    "liquidationValueDate",
    "releaseDate",
    "managementEntity",
    "agent",
    "discontinued",
    "annualizedProfitability",
    "cumulativeProfitability",
)


@app.command()
def fund(
    code: Annotated[str, typer.Argument(help="ISIN del fondo/ETF, o código de plan de pensiones (N1234).")],
    full: Annotated[bool, typer.Option(help="Respuesta completa en JSON (composición, comisiones…).")] = False,
) -> None:
    """Ficha de un fondo o plan del catálogo de MyInvestor."""
    with build_client() as client:
        data = client.pension_plan(code.upper()) if PENSION_CODE.match(code.upper()) else client.fund(code.upper())
        price = None if PENSION_CODE.match(code.upper()) or full else client.fund_price(code.upper())
    if not full:
        fund_data = data.get("fundData")
        data = {k: data[k] for k in FUND_FIELDS if data.get(k) not in (None, "", [], {})}
        if "managementEntity" not in data and isinstance(fund_data, dict) and fund_data.get("managingEntity"):
            data["managementEntity"] = fund_data["managingEntity"]
        if price:
            data["lastPrice"] = {k: price.get(k) for k in ("date", "price", "currency", "dayProfitPercentage")}
    print_json(data)


@app.command()
def funds(
    query: QueryOpt = None,
    sort: SortOpt = "ter,asc",
    limit: LimitOpt = 20,
    page: Annotated[int, typer.Option(min=0, help="Página (desde 0).")] = 0,
    category: CategoryOpt = None,
    manager: ManagerOpt = None,
    currency: CurrencyOpt = None,
    asset_type: AssetTypeOpt = None,
    region: RegionOpt = None,
    min_ter: MinTerOpt = None,
    max_ter: MaxTerOpt = None,
    risk: RiskOpt = None,
    rating: RatingOpt = None,
    esg: Annotated[bool, typer.Option("--esg", help="Solo fondos sostenibles (ESG).")] = False,
    hedged: Annotated[bool, typer.Option("--hedged", help="Solo fondos con la divisa cubierta.")] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Busca en el catálogo de fondos de MyInvestor.

    Los valores de `--category`, `--manager`, `--currency`, `--asset-type` y `--region`
    los lista `myinvestor fund-filters`; `--category` y `--manager` piden la lista para enviarlos tal
    como los escribe MyInvestor (una petición más cada una).
    """
    low_ter, high_ter = ter_range(min_ter, max_ter)
    filters = CatalogFilters(
        region=code_or_none(region),
        asset_type=code_or_none(asset_type),
        currency=code_or_none(currency),
        min_ter=low_ter,
        max_ter=high_ter,
        risk=risk_or_none(risk),
        rating=rating,
        esg=esg,
        hedged=hedged,
    )
    with build_client() as client:
        filters = replace(
            filters,
            category=resolve_filter(client, Domain.funds, FilterKind.categories, category),
            manager=resolve_filter(client, Domain.funds, FilterKind.managers, manager),
        )
        result = client.funds(query, sort=sort, page=page, limit=limit, filters=filters)
    rows = [
        {
            "isin": f.isin,
            "nombre": f.name,
            "gestora": f.managing_entity,
            "categoria": f.category,
            "ter": f.ter,
            "riesgo": f.risk_rating,
            "vl": f.liquidation_value,
            "ytd": f.annualized_profitability.ytd if f.annualized_profitability else None,
            "1a": f.annualized_profitability.one_year if f.annualized_profitability else None,
        }
        for f in result.funds
    ]
    suffix = f" ({result.total} en total)" if result.total is not None else ""
    render(rows, fmt, title=f"Fondos{suffix}")


@app.command()
def feed(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Máximo de entradas.")] = 20,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Actividad reciente (lo que la web muestra en «Actividad»)."""
    with build_client() as client:
        items = client.feed()
    items.sort(key=lambda i: i.date or date.min, reverse=True)
    rows = [
        {
            "fecha": i.date.isoformat() if i.date else None,
            "producto": i.product,
            "tipo": i.type,
            "titulo": i.title,
            "detalle": i.body,
        }
        for i in items[:limit]
    ]
    render(rows, fmt, title="Actividad")


@app.command()
def raw(
    path: Annotated[
        str, typer.Argument(help="Ruta bajo api.myinvestor.es, p. ej. /cperf-server/api/v2/cash-accounts/self")
    ],
    param: Annotated[list[str] | None, typer.Option("--param", "-p", help="Parámetro clave=valor (repetible).")] = None,
) -> None:
    """GET arbitrario a la API (exploración). Solo lectura: no hay POST."""
    if any(part == ".." for part in path.split("/")) or "?" in path or "#" in path:
        raise InputError("La ruta no puede llevar `..`, `?` ni `#`: pasa la query con --param.")
    params: list[tuple[str, str]] = []
    for item in param or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise InputError(f"--param {item!r}: usa clave=valor.")
        params.append((key, value))
    with build_client() as client:
        print_json(client.get("/" + path.lstrip("/"), params or None))
