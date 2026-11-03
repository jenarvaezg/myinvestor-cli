"""`snapshot` (one JSON document per day for automation, with the delta) and `summary` (the same, as tables).

Every section is fetched with the same client (one session renewal, shared GET cache) and fails on its
own: its error goes to `errors` and the rest still comes out. Only a dead session (exit codes 10, 11 and
14) aborts everything, since every other request would fail the same way.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, TypeVar

import httpx
import typer
from rich.markup import escape

from myinvestor.cli_accounts import (
    DEFAULT_MOVEMENT_DAYS,
    _account_row,
    _Gap,
    _movement_order,
    _movement_rows,
    _new_start,
    _pension_position,
    _securities_position,
    hold_row,
)
from myinvestor.cli_common import app, build_client, err, fatal, iso_moment, now_local, package_version
from myinvestor.cli_errors import error_kind, exit_code, requested
from myinvestor.cli_invest import _holding_row, _order_row, _with_weights, pension_row
from myinvestor.cli_state import MarkOpt, StateOpt, state_file
from myinvestor.cli_upcoming import DEFAULT_UPCOMING_DAYS, _warn
from myinvestor.client import MyInvestorClient
from myinvestor.errors import AuthError, CorruptKeyringError, CorruptStoreError, InputError, PartialResultError
from myinvestor.json_output import dumps
from myinvestor.models import (
    CashAccount,
    Contribution,
    Hold,
    Order,
    PensionAccount,
    PensionPlanPosition,
    SecuritiesAccount,
)
from myinvestor.output import OutputFormat, render
from myinvestor.psd2 import free_start
from myinvestor.snapshot import (
    SCHEMA,
    IncompleteSnapshotError,
    InvalidSnapshotError,
    Section,
    SectionError,
    compute_delta,
    latest_before,
    load_snapshot,
    normalized,
    save_snapshot,
    snapshot_day,
    snapshot_total,
)
from myinvestor.snapshot_tables import summary_tables
from myinvestor.state import (
    MOVEMENT_MARGIN_DAYS,
    CorruptStateError,
    Marks,
    SeenState,
    StateFile,
    StateKind,
    changed,
    merge_marks,
    movement_marks,
    unseen_movements,
)
from myinvestor.transfers_match import with_transfer_fields
from myinvestor.upcoming import IN_PROGRESS_ORDER_STATUSES, shortfalls, upcoming_rows

logger = logging.getLogger(__name__)
T = TypeVar("T")
Row = dict[str, Any]

# Errors after which no other request can succeed: abort instead of failing every section.
FATAL_ERRORS = (AuthError, CorruptStoreError, CorruptKeyringError)

SaveOpt = Annotated[
    Path | None,
    typer.Option(
        "--save",
        metavar="DIR",
        help="Guardar también en DIR/AAAA-MM-DD.json (0600; DIR 0700), sustituyendo el del día. Sin --previous, "
        "el delta se calcula contra el último de DIR anterior a hoy.",
    ),
]
PreviousOpt = Annotated[
    Path | None,
    typer.Option("--previous", metavar="FILE", help="Snapshot anterior contra el que calcular `delta`."),
]


@dataclass(frozen=True)
class _Request:
    save: Path | None
    previous: Path | None
    new: bool
    state_path: Path | None
    mark: bool


@app.command()
def snapshot(
    save: SaveOpt = None,
    previous: PreviousOpt = None,
    new: Annotated[
        bool,
        typer.Option("--new/--no-new", help="Incluir `new`: movimientos y órdenes nuevos desde el anterior --new."),
    ] = True,
    state_path: StateOpt = None,
    mark: MarkOpt = True,
) -> None:
    """Un único JSON con la posición, la cartera, lo pendiente, lo que viene, lo nuevo y el delta.

    Para automatizar (cron, un agente): un solo login/refresh para todo. Si una sección falla, el resto
    sale igual y el error va a `errors`; el código de salida es el peor de las secciones (0 si todo va
    bien). Esquema completo y fórmula del delta: docs/snapshot.md.
    """
    requested.json = True  # errors on stderr as one JSON line, like any `-f json`
    doc, errors, commit = _take(_Request(save, previous, new, state_path, mark))
    print(dumps(doc))
    commit()
    _finish(errors)


@app.command()
def summary(
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", metavar="FILE", help="Mostrar un snapshot guardado, sin llamar a MyInvestor."),
    ] = None,
    save: SaveOpt = None,
    previous: PreviousOpt = None,
    new: Annotated[
        bool,
        typer.Option(
            "--new/--no-new", help="Incluir lo nuevo desde el anterior --new (por defecto no: no toca el estado)."
        ),
    ] = False,
    state_path: StateOpt = None,
    mark: MarkOpt = True,
) -> None:
    """El snapshot en tablas: posición, variación (aportado/mercado), cartera, pendiente, lo que viene y lo nuevo.

    Con --from-file lee uno guardado (y con --previous recalcula su delta contra otro).
    """
    if from_file is not None:
        if save is not None or new:
            raise InputError("--from-file no se combina con --save ni con --new.")
        doc = load_snapshot(from_file)
        if previous is not None:
            doc = {**doc, "delta": normalized(compute_delta(doc, load_snapshot(previous)))}
        _show_summary(doc)
        return
    doc, errors, commit = _take(_Request(save, previous, new, state_path, mark))
    _show_summary(doc)
    commit()
    _finish(errors)


def _finish(errors: list[SectionError]) -> None:
    if errors:
        raise IncompleteSnapshotError(errors)


def _show_summary(doc: dict[str, Any]) -> None:
    for title, rows in summary_tables(doc):
        render(rows, OutputFormat.table, title=title)
    for shortfall in shortfalls(doc.get("upcoming") or []):
        _warn(shortfall)
    for e in doc.get("errors") or []:
        fatal.print(f"[red]{escape(str(e['section']))}:[/red] {escape(str(e['message']))} (código {e['exit_code']})")


# ---- taking the snapshot ----------------------------------------------------------------------------


def _take(req: _Request) -> tuple[dict[str, Any], list[SectionError], Callable[[], None]]:
    """The snapshot (saved with --save), its section errors and the callback that records what is new.

    The state is only touched by the callback, after the snapshot has been printed.
    """
    if req.save is not None and req.save.exists() and not req.save.is_dir():
        raise InputError(f"--save: {req.save} no es un directorio.")
    previous = load_snapshot(req.previous) if req.previous else None  # a wrong --previous fails before any request
    consulted = now_local()
    today = consulted.date()
    errors: list[SectionError] = []
    if previous is None and req.save is not None:
        previous = _auto_previous(req.save, today, errors)
    with build_client() as client:
        if client.session.is_expired():
            client.refresh()  # once, outside the sections: a failed renewal aborts instead of repeating it
        built = _Builder(_Sources(client), today, errors, previous, req)
        doc = built.document(iso_moment(consulted))
    if req.save is not None:
        save_snapshot(req.save, today, doc)
    return doc, errors, built.commit


def _auto_previous(directory: Path, today: date, errors: list[SectionError]) -> dict[str, Any] | None:
    path = latest_before(directory, today)
    if path is None:
        return None
    try:
        return load_snapshot(path)
    except InvalidSnapshotError as exc:
        errors.append(_section_error(Section.delta, exc))
        return None


def _section_error(section: Section, exc: BaseException) -> SectionError:
    if isinstance(exc, httpx.HTTPError):
        message = f"{type(exc).__name__}. MyInvestor no responde; reintenta en un rato."
    else:
        message = str(exc) or type(exc).__name__
    return SectionError(section.value, error_kind(exc), exit_code(exc), message)


class _Sources:
    """What the sections share, fetched once. A failure is kept and raised again to every section that
    needs it, without asking MyInvestor again."""

    def __init__(self, client: MyInvestorClient) -> None:
        self.client = client
        self._done: dict[str, tuple[bool, Any]] = {}

    def once(self, name: str, fetch: Callable[[], T]) -> T:
        if name not in self._done:
            try:
                self._done[name] = (True, fetch())
            except FATAL_ERRORS:
                raise
            except Exception as exc:
                self._done[name] = (False, exc)
        succeeded, value = self._done[name]
        if not succeeded:
            raise value
        return value

    def cash_accounts(self) -> list[CashAccount]:
        return self.once("cash", self.client.cash_accounts)

    def holds(self) -> dict[str, list[Hold]]:
        return self.once(
            "holds", lambda: {a.account_id: self.client.holds(a.account_uuid) for a in self.cash_accounts()}
        )

    def securities(self) -> list[SecuritiesAccount]:
        return self.once("securities", self.client.securities_accounts)

    def pension_accounts(self) -> list[PensionAccount]:
        return self.once("pension_accounts", self.client.pension_accounts)

    def pension_plans(self) -> list[tuple[PensionAccount, list[PensionPlanPosition]]]:
        return self.once(
            "pension_plans", lambda: [(p, self.client.pension_plans(p.account_id)) for p in self.pension_accounts()]
        )

    def securities_orders(self) -> list[tuple[Order, str | None]]:
        """Recent and in-progress orders (no dates, like `orders`), with the cash account each one charges."""
        return self.once(
            "securities_orders",
            lambda: [(o, s.cash_account_id) for s in self.securities() for o in self.client.orders(s.account_id)],
        )

    def pension_orders(self) -> list[tuple[Order, str | None]]:
        return self.once(
            "pension_orders",
            lambda: [
                (o, p.cash_account_id)
                for p in self.pension_accounts()
                for o in self.client.pension_orders(p.account_id)
            ],
        )

    def contributions(self) -> list[Contribution]:
        return self.once("contributions", self.client.contributions)


@dataclass(frozen=True)
class _Movements:
    """Rows of the movements fetched for the snapshot: the delta window and, per account, the `--new` range."""

    rows: list[Row]
    start: date  # of the delta window (`movements.desde`)
    new_starts: dict[str, date]  # account uuid → first day of its `--new` range
    gaps: list[_Gap]


@dataclass
class _Builder:
    sources: _Sources
    today: date
    errors: list[SectionError]
    previous: dict[str, Any] | None
    req: _Request
    marks: list[Marks] = field(default_factory=list)
    _state: tuple[StateFile, SeenState] | None = None

    def section(self, name: Section, build: Callable[[], T]) -> T | None:
        try:
            return build()
        except FATAL_ERRORS:
            raise
        except Exception as exc:
            logger.debug("snapshot: la sección %s ha fallado", name, exc_info=True)
            self.errors.append(_section_error(name, exc))
            return None

    def document(self, as_of: str) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema": SCHEMA,
            "as_of": as_of,
            "cli_version": package_version(),
            "total": None,
            "cash": self.section(Section.cash, self.cash),
            "securities": self.section(Section.securities, self.securities),
            "pensions": self.section(Section.pensions, self.pensions),
            "pending": self.section(Section.pending, self.pending),
            "upcoming": self.section(Section.upcoming, self.upcoming),
            "movements": self.section(Section.movements, self.movements_section),
            "new": self.new() if self.req.new else None,
        }
        doc["total"] = snapshot_total(doc)
        doc = normalized(doc)
        previous = self.previous
        doc["delta"] = (
            self.section(Section.delta, lambda: normalized(compute_delta(doc, previous))) if previous else None
        )
        doc["errors"] = [e.to_dict() for e in self.errors]
        return doc

    # ---- sections -------------------------------------------------------------------------------

    def cash(self) -> list[Row]:
        return [
            {**_account_row(a, with_holders=True), "fecha_valor": self.today.isoformat()}  # the balance is live
            for a in self.sources.cash_accounts()
        ]

    def securities(self) -> list[Row]:
        accounts = self.sources.securities()
        positions = sorted(
            ((s, i) for s in accounts for i in s.investments), key=lambda p: p[1].market_value or 0, reverse=True
        )
        weighted = _with_weights([_holding_row(s, i) for s, i in positions])  # `peso` over the whole portfolio
        return [
            {**_securities_position(s), "posiciones": [r for r in weighted if r["cuenta"] == s.account_id]}
            for s in accounts
        ]

    def pensions(self) -> list[Row]:
        return [
            {**_pension_position(p, plans), "planes": [pension_row(p, plan) for plan in plans]}
            for p, plans in self.sources.pension_plans()
        ]

    def pending(self) -> dict[str, list[Row]]:
        orders = [o for o, _ in self.sources.securities_orders() + self.sources.pension_orders()]
        in_progress = [o for o in orders if (o.status or "").upper() in IN_PROGRESS_ORDER_STATUSES]
        holds = self.sources.holds()
        return {
            "orders": [_order_row(o) for o in _newest_first(in_progress)],
            "holds": [hold_row(a, h) for a in self.sources.cash_accounts() for h in holds.get(a.account_id, [])],
        }

    def upcoming(self) -> list[Row]:
        return upcoming_rows(
            today=self.today,
            days=DEFAULT_UPCOMING_DAYS,
            accounts=self.sources.cash_accounts(),
            contributions=self.sources.contributions(),
            orders=self.sources.securities_orders() + self.sources.pension_orders(),
            holds=self.sources.holds(),
        )

    def movements_section(self) -> dict[str, Any]:
        fetched = self.fetched_movements()
        rows = [r for r in fetched.rows if r["fecha"] >= fetched.start.isoformat()]
        return {
            "desde": fetched.start.isoformat(),
            "hasta": self.today.isoformat(),
            "huecos": [
                {
                    "cuenta_id": g.account.account_id,
                    "cuenta": g.account.alias or g.account.account_id,
                    "desde": g.span.start.isoformat(),
                    "hasta": g.span.end.isoformat(),
                }
                for g in fetched.gaps
            ],
            "movimientos": sorted(rows, key=_movement_order, reverse=True),
        }

    def new(self) -> dict[str, list[Row] | None]:
        return {
            "movements": self.section(Section.new_movements, self.new_movements),
            "orders": self.section(
                Section.new_orders, lambda: self.new_orders(StateKind.orders, self.sources.securities_orders())
            ),
            "pension_orders": self.section(
                Section.new_pension_orders,
                lambda: self.new_orders(StateKind.pension_orders, self.sources.pension_orders()),
            ),
        }

    def new_movements(self) -> list[Row]:
        _, state = self.state()
        cursors = state.marked.movements
        fetched = self.fetched_movements()
        accounts = self.sources.cash_accounts()
        in_range = [
            r for r in fetched.rows if r["fecha"] >= fetched.new_starts.get(r["cuenta_uuid"], self.today).isoformat()
        ]
        gapped = {g.account.account_uuid for g in fetched.gaps}  # never mark a gap as seen
        complete = [a.account_uuid for a in accounts if a.account_uuid not in gapped]
        self.marks.append(movement_marks([r for r in in_range if r["cuenta_uuid"] not in gapped], complete, self.today))
        return sorted(unseen_movements(in_range, cursors), key=_movement_order, reverse=True)

    def new_orders(self, kind: StateKind, orders: list[tuple[Order, str | None]]) -> list[Row]:
        _, state = self.state()
        found = _newest_first([o for o, _ in orders])
        fresh = set(changed(((o.reference, o.status) for o in found), state.marked.snapshot(kind)))
        shown = [o for o in found if o.reference in fresh]
        self.marks.append(Marks(snapshots={kind: {o.reference: o.status for o in shown}}))
        return [_order_row(o) for o in shown]

    # ---- shared pieces --------------------------------------------------------------------------

    def state(self) -> tuple[StateFile, SeenState]:
        if self._state is None:
            file = state_file(self.req.state_path)
            self._state = (file, file.load())
        return self._state

    def cursor_state(self) -> SeenState | None:
        """The `--new` state if it is wanted and readable (a corrupt one only fails the `new` sections)."""
        if not self.req.new:
            return None
        try:
            return self.state()[1]
        except CorruptStateError:  # reported by the `new` sections, which load it again
            return None

    def fetched_movements(self) -> _Movements:
        return self.sources.once("movements", self._fetch_movements)

    def _fetch_movements(self) -> _Movements:
        """One fetch for both uses: the delta window (from the previous snapshot's day minus the `--new`
        margin, never outside the window served without a second factor) and each account's `--new` range."""
        accounts = self.sources.cash_accounts()
        anchor = min(snapshot_day(self.previous) or self.today, self.today) if self.previous else self.today
        start = max(anchor - timedelta(days=MOVEMENT_MARGIN_DAYS), free_start(self.today))
        state = self.cursor_state()
        first = self.today - timedelta(days=DEFAULT_MOVEMENT_DAYS)  # an account's first --new, as in `movements`
        cursors = state.marked.movements if state is not None else None
        new_starts = (
            {a.account_uuid: _new_start(a, cursors.get(a.account_uuid), first, self.today) for a in accounts}
            if cursors is not None
            else {}
        )
        by_start: dict[date, list[CashAccount]] = {}
        for a in accounts:
            by_start.setdefault(min(start, new_starts.get(a.account_uuid, start)), []).append(a)
        rows: list[Row] = []
        gaps: list[_Gap] = []
        for day, group in sorted(by_start.items()):
            got, missing = _movement_rows(self.sources.client, group, day, self.today)
            rows += got
            gaps += missing
        if gaps:  # the rows fetched are kept; the section is marked partial
            missing = PartialResultError(
                f"Faltan movimientos que MyInvestor solo da con un segundo factor: {'; '.join(map(str, gaps))}. "
                "Lo nuevo y el aportado del efectivo de esas cuentas quedan sin revisar."
            )
            self.errors.append(_section_error(Section.movements, missing))
        return _Movements(with_transfer_fields(rows), start, new_starts, gaps)

    def commit(self) -> None:
        """Record what `new` showed: marked as seen, or pending with `--no-mark`."""
        marks = Marks()
        for m in self.marks:
            marks = merge_marks(marks, m)
        if self._state is None or marks.is_empty():
            return
        file, _ = self._state
        file.update(lambda state: state.mark(marks) if self.req.mark else state.defer(marks))
        if not self.req.mark:
            err.print("[dim]Lo nuevo queda sin marcar como visto; confírmalo con `myinvestor state mark`.[/dim]")


def _newest_first(orders: list[Order]) -> list[Order]:
    return sorted(orders, key=lambda o: (o.order_date, o.reference), reverse=True)
