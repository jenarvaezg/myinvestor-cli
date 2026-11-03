"""`doctor`: one OK/WARN/FAIL line per check, exit 1 if any check fails."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from myinvestor import cli_common
from myinvestor.cli_common import FormatOpt, app, build_client, options, package_version
from myinvestor.doctor import Check, Level, has_failures, run_doctor
from myinvestor.errors import EXIT_ERROR
from myinvestor.http import build_http_client
from myinvestor.output import OutputFormat, print_json, render

# The probes are cheap GETs: fail fast instead of the 60 s / 4 retries of normal commands.
DOCTOR_TIMEOUT = 10.0
COLORS = {Level.ok: "green", Level.warn: "yellow", Level.fail: "red"}

out = Console()


def _print_lines(checks: list[Check]) -> None:
    width = max(len(c.name) for c in checks)
    for c in checks:
        color = COLORS[c.level]
        out.print(
            f"[{color}]{c.level.value:<4}[/{color}]  {c.name:<{width}}  {escape(c.message)}",
            highlight=False,
            soft_wrap=True,
        )


def _report(checks: list[Check], fmt: OutputFormat) -> None:
    if fmt is OutputFormat.json:
        print_json(
            {"ok": not has_failures(checks), "cli_version": package_version(), "checks": [c.to_dict() for c in checks]}
        )
    elif fmt is OutputFormat.table:
        _print_lines(checks)
    else:
        render([{"check": c.name, "status": c.level.value, "message": c.message} for c in checks], fmt)


@app.command()
def doctor(
    fmt: FormatOpt = OutputFormat.table,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Solo las comprobaciones locales (almacén y sesión): sin red ni renovar."),
    ] = False,
) -> None:
    """Diagnóstico: almacén, sesión, red e Imperva, API, reloj, versión de la web y mantenimiento."""
    store = cli_common.get_store()
    if offline:
        checks = run_doctor(store, http=None, client_factory=None)
    else:
        with build_http_client(retries=0, timeout=DOCTOR_TIMEOUT, min_interval=options.min_interval) as http:
            checks = run_doctor(store, http=http, client_factory=lambda: build_client(store))
    _report(checks, fmt)
    if has_failures(checks):
        raise typer.Exit(EXIT_ERROR)
