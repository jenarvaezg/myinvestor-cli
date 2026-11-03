"""`--new` options shared by `movements`, `orders` and `contributions`, and `state show|mark|reset`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from myinvestor.cli_common import FormatOpt, app, err
from myinvestor.output import OutputFormat, print_json, render
from myinvestor.state import Marks, SeenState, StateFile, StateKind, default_state_path, state_to_dict

StateOpt = Annotated[
    Path | None,
    typer.Option(
        "--state",
        envvar="MYINVESTOR_STATE",
        help="Fichero de estado de --new (por defecto $XDG_STATE_HOME/myinvestor/seen.json).",
    ),
]
NewOpt = Annotated[
    bool,
    typer.Option("--new", help="Solo lo que no se mostró en la ejecución anterior con --new (estado local)."),
]
MarkOpt = Annotated[
    bool,
    typer.Option(
        "--mark-seen/--no-mark",
        help="Con --new: marcar lo mostrado como visto (por defecto) o dejarlo pendiente de `myinvestor state mark`.",
    ),
]
KindArg = Annotated[
    StateKind | None,
    typer.Argument(help="movements, orders, pension_orders o contributions; sin él, todo."),
]

state_app = typer.Typer(no_args_is_help=True, help="Estado local de --new: qué se ha mostrado ya.")
app.add_typer(state_app, name="state")


def state_file(path: Path | None) -> StateFile:
    return StateFile(path or default_state_path())


def record(file: StateFile, marks: Marks, *, mark: bool, kind: StateKind) -> None:
    """Mark what was shown, or (`--no-mark`) leave it pending for `state mark`."""
    if marks.is_empty():
        return
    file.update(lambda state: state.mark(marks) if mark else state.defer(marks))
    if not mark:
        err.print(f"[dim]Sin marcar como visto; confirma con `myinvestor state mark {kind}`.[/dim]")


def _summary(state: SeenState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "tipo": StateKind.movements.value,
            "clave": uuid,
            "ultimo_dia": cursor.last_day.isoformat(),
            "vistos": len(cursor.seen),
            "pendientes": len(state.pending.movements[uuid].seen) if uuid in state.pending.movements else 0,
        }
        for uuid, cursor in sorted(state.marked.movements.items())
    ]
    rows += [
        {
            "tipo": StateKind.movements.value,
            "clave": uuid,
            "ultimo_dia": None,
            "vistos": 0,
            "pendientes": len(cursor.seen),
        }
        for uuid, cursor in sorted(state.pending.movements.items())
        if uuid not in state.marked.movements
    ]
    for kind in (StateKind.orders, StateKind.pension_orders, StateKind.contributions):
        seen, pending = state.marked.snapshot(kind), state.pending.snapshot(kind)
        if seen or pending:
            rows.append(
                {"tipo": kind.value, "clave": None, "ultimo_dia": None, "vistos": len(seen), "pendientes": len(pending)}
            )
    return rows


@state_app.command("show")
def show(path: StateOpt = None, fmt: FormatOpt = OutputFormat.table) -> None:
    """Qué hay en el estado: por cuenta, último día consultado e ids vistos; órdenes y aportaciones vistas.

    Con -f json, el contenido completo del fichero (con `path`).
    """
    file = state_file(path)
    state = file.load()
    if fmt is OutputFormat.json:
        print_json({"path": str(file.path), **state_to_dict(state)})
        return
    render(_summary(state), fmt, title=f"Estado ({file.path})")


@state_app.command("mark")
def mark_pending(kind: KindArg = None, path: StateOpt = None) -> None:
    """Confirma como visto lo que se mostró con --new --no-mark."""
    file = state_file(path)
    if file.load().pending.only(kind).is_empty():
        err.print("[dim]No hay nada pendiente de marcar.[/dim]")
        return
    file.update(lambda state: state.commit(kind))
    err.print(f"Marcado como visto{f' ({kind})' if kind else ''}.")


@state_app.command("reset")
def reset(kind: KindArg = None, path: StateOpt = None) -> None:
    """Olvida el estado (todo o un tipo): la próxima vez con --new todo saldrá como nuevo."""
    file = state_file(path)
    if kind is None:
        file.clear()
    else:
        file.update(lambda state: state.reset(kind))
    err.print(f"Estado borrado{f' ({kind})' if kind else ''}: {file.path}")
