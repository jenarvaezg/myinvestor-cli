"""`export DIR`: full, then incremental, local history of movements, orders, contributions, holdings, accounts."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from myinvestor.cli_accounts import (
    AllowPartialOpt,
    _account_row,
    _fetch_statements,
    _Fetched,
    _Gap,
    _movement_order,
    _rows_of,
)
from myinvestor.cli_common import app, build_client, err, iso_moment, now_local, package_version, parse_day
from myinvestor.cli_invest import EARLIEST_ORDER_DATE, _holding_row, _order_row, _with_weights, contribution_row
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError, PartialResultError
from myinvestor.export import (
    DATASETS,
    MANIFEST,
    MANIFEST_VERSION,
    Appended,
    CorruptExportError,
    Coverage,
    ExportDir,
    ExportFormat,
    HoleReason,
    Interval,
    SpanCheck,
    check_span,
    coverage_from_dict,
    coverage_to_dict,
    next_start,
    update_coverage,
)
from myinvestor.models import CashAccount, Order
from myinvestor.transfers_match import with_transfer_fields

# First export of an account without --since: from as far back as MyInvestor may have anything.
EARLIEST_EXPORT_DATE = EARLIEST_ORDER_DATE

Row = dict[str, Any]


@app.command()
def export(
    directory: Annotated[
        Path, typer.Argument(metavar="DIR", help="Directorio de destino (si no existe, se crea con permisos 0700).")
    ],
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Pedir al menos desde este día (AAAA-MM-DD). La primera vez, por defecto, desde "
            f"{EARLIEST_EXPORT_DATE}; después, desde lo último exportado.",
        ),
    ] = None,
    fmt: Annotated[
        ExportFormat,
        typer.Option("--format", "-f", help="csv, o json (JSON Lines: un objeto por línea, para poder añadir)."),
    ] = ExportFormat.csv,
    allow_partial: AllowPartialOpt = False,
) -> None:
    """Exporta a DIR el histórico: movements, orders, pension_orders, contributions, holdings y accounts.

    Incremental: solo añade filas nuevas (por id) y nunca reescribe las que ya hay, así que se puede
    llamar a diario. La primera vez los movimientos anteriores a los últimos 90 días piden una
    aprobación en la app por cuenta (solo en una terminal o con --approve); sin ella se exporta lo
    demás, el hueco queda en manifest.json y se sale con 13 (0 con --allow-partial).
    """
    since_day = parse_day(since, "--since")
    out = ExportDir(directory, fmt)
    today = date.today()
    with out.locked():
        manifest = out.load_manifest()
        _check_format(manifest, fmt, out)
        old = _coverages(manifest, out.path / MANIFEST)
        consulted = now_local()
        with build_client() as client:
            accounts = client.cash_accounts()
            fetched, gaps = _fetch_movements(client, accounts, old, since_day, today)
            datasets = _datasets(client, accounts, fetched, since_day, today, iso_moment(consulted))
        results = {d.name: out.append(d, datasets[d.name]) for d in DATASETS}
        coverage = {a.account_uuid: _account_coverage(a, old, fetched, gaps) for a in accounts}
        out.write_manifest(_manifest(manifest, fmt, iso_moment(consulted), results, accounts, coverage))
    _report(results, accounts, coverage)
    _report_export_gaps(gaps, out.path / MANIFEST, allow_partial=allow_partial)


def _check_format(manifest: dict[str, Any] | None, fmt: ExportFormat, out: ExportDir) -> None:
    if manifest and manifest.get("format") != fmt.value:
        raise InputError(
            f"{out.path} ya tiene una exportación en {manifest.get('format')}: usa --format {manifest.get('format')} "
            "u otro directorio."
        )


def _coverages(manifest: dict[str, Any] | None, path: Path) -> dict[str, Coverage]:
    raw = (manifest or {}).get("accounts", {})
    try:
        return {uuid: coverage_from_dict(c) for uuid, c in raw.items()}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise CorruptExportError(path, "cobertura no válida") from exc


def _fetch_movements(
    client: MyInvestorClient,
    accounts: list[CashAccount],
    old: dict[str, Coverage],
    since: date | None,
    today: date,
) -> tuple[list[_Fetched], list[_Gap]]:
    """Statements of every account from where its export left off (accounts starting the same day together)."""
    by_start: dict[date, list[CashAccount]] = {}
    for acc in accounts:
        start = min(next_start(old.get(acc.account_uuid), since, EARLIEST_EXPORT_DATE), today)
        by_start.setdefault(start, []).append(acc)
    fetched: list[_Fetched] = []
    gaps: list[_Gap] = []
    for start, accs in sorted(by_start.items()):
        got, missing = _fetch_statements(client, accs, start, today)
        fetched += got
        gaps += missing
    return fetched, gaps


def _datasets(
    client: MyInvestorClient,
    accounts: list[CashAccount],
    fetched: list[_Fetched],
    since: date | None,
    today: date,
    consulted: str,
) -> dict[str, list[Row]]:
    """Rows of every file, oldest first, with the same columns as the commands that show them."""
    start = since or EARLIEST_ORDER_DATE
    orders = [o for i in client.securities_account_ids() for o in client.orders(i, start, today)]
    pension = [o for p in client.pension_accounts() for o in client.pension_orders(p.account_id, start, today)]
    return {
        "movements": sorted(with_transfer_fields(_rows_of(accounts, fetched)), key=_movement_order),
        "orders": _order_rows(orders),
        "pension_orders": _order_rows(o for o in pension if o.order_date >= start),
        "contributions": [contribution_row(c) for c in client.contributions()],
        "holdings": [
            {**r, "consultado_en": consulted}
            for r in _with_weights([_holding_row(s, i) for s in client.securities_accounts() for i in s.investments])
        ],
        "accounts": [{**_account_row(a, with_holders=False), "consultado_en": consulted} for a in accounts],
    }


def _order_rows(orders: Any) -> list[Row]:
    found: list[Order] = sorted(orders, key=lambda o: (o.order_date, o.reference))
    return [_order_row(o) for o in found]


def _span_check(f: _Fetched) -> SpanCheck:
    st = f.statement
    return check_span(
        Interval(f.span.start, f.span.end), st.start_balance, st.end_balance, [x.amount for x in st.flows]
    )


def _account_coverage(
    acc: CashAccount, old: dict[str, Coverage], fetched: list[_Fetched], gaps: list[_Gap]
) -> Coverage:
    uuid = acc.account_uuid
    checks = [_span_check(f) for f in fetched if f.account.account_uuid == uuid]
    missing = [Interval(g.span.start, g.span.end) for g in gaps if g.account.account_uuid == uuid]
    return update_coverage(old.get(uuid), checks, missing)


def _manifest(
    previous: dict[str, Any] | None,
    fmt: ExportFormat,
    as_of: str,
    results: dict[str, Appended],
    accounts: list[CashAccount],
    coverage: dict[str, Coverage],
) -> dict[str, Any]:
    """Accounts that no longer show up keep their previous entry."""
    names = {a.account_uuid: {"cuenta": a.alias or a.account_id, "cuenta_id": a.account_id} for a in accounts}
    return {
        "version": MANIFEST_VERSION,
        "as_of": as_of,
        "cli_version": package_version(),
        "format": fmt.value,
        "files": {name: {"fichero": r.file.name, "filas": r.total} for name, r in results.items()},
        "accounts": {
            **(previous or {}).get("accounts", {}),
            **{uuid: coverage_to_dict(cov, names[uuid]) for uuid, cov in coverage.items()},
        },
    }


def _report(results: dict[str, Appended], accounts: list[CashAccount], coverage: dict[str, Coverage]) -> None:
    for r in results.values():
        err.print(f"{r.file.name}: {r.new} filas nuevas ({r.total} en total)")
        if r.dropped_columns:
            err.print(
                f"[yellow]{r.file.name}:[/yellow] su cabecera no tiene {', '.join(r.dropped_columns)}; esas columnas "
                "no se guardan (para tenerlas, exporta a otro directorio)."
            )
    for acc in accounts:
        for check in coverage[acc.account_uuid].last_checks:
            if check.ok is False:
                err.print(
                    f"[yellow]Descuadre en {escape(acc.alias or acc.account_id)} del {check.interval.start} al "
                    f"{check.interval.end}:[/yellow] saldo inicial {check.start_balance} + movimientos "
                    f"{check.total} ≠ saldo final {check.end_balance}; anotado como hueco "
                    f"«{HoleReason.mismatch.value}» en {MANIFEST}."
                )


def _report_export_gaps(gaps: list[_Gap], manifest: Path, *, allow_partial: bool) -> None:
    if not gaps:
        return
    missing = "; ".join(map(str, gaps))
    if allow_partial:
        err.print(f"[yellow]Exportación parcial:[/yellow] faltan los movimientos que piden firma ({escape(missing)}).")
        return
    raise PartialResultError(
        f"Faltan movimientos que MyInvestor solo da con un segundo factor: {missing}. Quedan anotados como huecos "
        f"en {manifest} y la próxima exportación los vuelve a pedir: repítela en una terminal interactiva o con "
        "`myinvestor --approve export …`. Con --allow-partial sale con 0."
    )
