"""`fund-history ISIN`: NAV history of any fund of the catalog, owned or not."""

from __future__ import annotations

import re
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError

from myinvestor.cli_common import FormatOpt, app, build_client, err, parse_day
from myinvestor.cli_invest import PENSION_CODE
from myinvestor.client import MyInvestorClient
from myinvestor.errors import ApiError, InputError, NetworkBlockedError
from myinvestor.fund_history import Every, history_rows, in_range, resample
from myinvestor.models import NavPoint
from myinvestor.output import OutputFormat, render

ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")  # 2-letter country, 9 alphanumerics, check digit
NOT_FOUND_STATUSES = frozenset({400, 404})


def _isin(value: str) -> str:
    code = value.strip().upper()
    if PENSION_CODE.match(code):
        raise InputError(f"fund-history es para fondos y ETF (ISIN); para un plan propio usa `history {code}`.")
    if not ISIN.match(code):
        raise InputError(f"{value!r} no parece un ISIN (2 letras, 9 letras o cifras y una cifra: IE0032126645).")
    return code


def _no_history(isin: str, detail: str = "") -> InputError:
    return InputError(
        f"MyInvestor no tiene histórico del VL de {isin}{detail}: comprueba que está en el catálogo "
        f"(`myinvestor funds {isin}`)."
    )


def _fetch(client: MyInvestorClient, isin: str) -> list[NavPoint]:
    try:
        points = client.fund_nav_history(isin)
    except ApiError as exc:
        if exc.status in NOT_FOUND_STATUSES:
            raise _no_history(isin, f" (HTTP {exc.status}: {exc.message})") from exc
        raise
    if not points:
        raise _no_history(isin)
    return points


def _currency(client: MyInvestorClient, isin: str) -> str | None:
    """The fund's currency, from its sheet (`INIT_GRAPH` does not say it); None if the sheet fails."""
    try:
        return client.fund_sheet(isin).currency
    except (ApiError, NetworkBlockedError, httpx.HTTPError, ValidationError) as exc:
        err.print(
            f"[yellow]No se ha podido leer la moneda de {isin} ({type(exc).__name__}): `moneda` va vacía.[/yellow]"
        )
        return None


@app.command("fund-history")
def fund_history(
    isin: Annotated[str, typer.Argument(help="ISIN del fondo o ETF del catálogo (no hace falta tenerlo).")],
    since: Annotated[str | None, typer.Option("--since", help="Desde (AAAA-MM-DD).")] = None,
    until: Annotated[str | None, typer.Option("--until", help="Hasta (AAAA-MM-DD).")] = None,
    every: Annotated[
        Every, typer.Option("--every", help="Un punto por día, semana (ISO) o mes: el último de cada una.")
    ] = Every.day,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Histórico del valor liquidativo de un fondo del catálogo, desde su inicio.

    El espaciado de la API es irregular (semanal en lo antiguo, diario en lo reciente): `--every week`
    o `month` deja el último punto de cada semana o mes. En la tabla, `variacion_pct` es la variación
    frente al primer punto mostrado; en JSON/CSV/TSV van `fecha`, `vl` y `moneda` (de la ficha del fondo).
    """
    code = _isin(isin)
    start, end = parse_day(since, "--since"), parse_day(until, "--until")
    if start and end and start > end:
        raise InputError("--since es posterior a --until.")
    with build_client() as client:
        points = _fetch(client, code)
        currency = _currency(client, code)
    shown = resample(in_range(points, start, end), every)
    if not shown:
        err.print(
            f"[dim]Sin puntos en ese rango; el histórico de {code} va de "
            f"{points[0].position_date} a {points[-1].position_date}.[/dim]"
        )
    table = fmt is OutputFormat.table
    rows = history_rows(shown, currency, with_change=table)
    render(rows, fmt, title=f"VL de {code}" if table else None)
