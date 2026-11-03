"""Investment commands: `holdings`, `orders`, `pensions`, `history`, `contributions`, `fund`, `funds`, `feed`."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Annotated, Any

import typer

from myinvestor.cli_common import FormatOpt, app, build_client, parse_day
from myinvestor.client import MyInvestorClient, Period
from myinvestor.errors import InputError
from myinvestor.models import Investment, Order, SecuritiesAccount
from myinvestor.output import OutputFormat, print_json, render

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


@app.command()
def holdings(
    account: SecAccountOpt = None,
    kind: Annotated[
        HoldingKind | None, typer.Option(help="Solo un tipo: fondos (INDEXED_FUND…) o broker (acciones/ETF).")
    ] = None,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Fondos, ETF y acciones de las cuentas de valores, con coste, valor y rentabilidad."""
    with build_client() as client:
        rows = [_holding_row(sec, inv) for sec in _securities(client, account) for inv in sec.investments]
    if kind:
        rows = [r for r in rows if (r["tipo"] == BROKER) == (kind is HoldingKind.broker)]
    rows.sort(key=lambda r: r["valor"] or 0, reverse=True)
    render(rows, fmt, title="Cartera")


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
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Órdenes de compra/venta/traspaso de fondos y acciones, o de planes de pensiones."""
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
    render([_order_row(o) for o in found], fmt, title="Órdenes")


@app.command()
def pensions(fmt: FormatOpt = OutputFormat.table) -> None:
    """Planes de pensiones: participaciones, coste, valor y rentabilidad."""
    with build_client() as client:
        rows = [
            {
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
            for acc in client.pension_accounts()
            for p in client.pension_plans(acc.account_id)
        ]
    render(rows, fmt, title="Planes de pensiones")


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


@app.command()
def contributions(
    all_contributions: Annotated[bool, typer.Option("--all", help="Incluir las canceladas.")] = False,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Aportaciones periódicas programadas a fondos y planes."""
    with build_client() as client:
        found = client.contributions()
    if not all_contributions:
        found = [c for c in found if c.status not in ("CANCELED", "CANCELLED")]
    rows = [
        {
            "id": c.contribution_id,
            "estado": c.status,
            "producto": c.isin,
            "nombre": c.fund_name,
            "importe": c.amount,
            "frecuencia": c.contribution_time_frame.recurrence if c.contribution_time_frame else None,
            "proxima": (
                c.contribution_time_frame.next_execution_date.isoformat()
                if c.contribution_time_frame and c.contribution_time_frame.next_execution_date
                else None
            ),
            "destino": c.to_account_type,
        }
        for c in found
    ]
    render(rows, fmt, title="Aportaciones periódicas")


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
    query: Annotated[
        str | None, typer.Argument(help="Texto libre tal como aparece en el nombre («S&P 500», no «sp500»), ISIN…")
    ] = None,
    sort: Annotated[str, typer.Option(help="Orden `campo,asc|desc` (ter, name, liquidationValue…).")] = "ter,asc",
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=200, help="Máximo de resultados.")] = 20,
    page: Annotated[int, typer.Option(min=0, help="Página (desde 0).")] = 0,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Busca en el catálogo de fondos de MyInvestor."""
    with build_client() as client:
        result = client.funds(query, sort=sort, page=page, limit=limit)
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
