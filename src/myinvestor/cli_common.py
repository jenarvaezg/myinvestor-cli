"""Shared Typer app, global options and helpers for the `myinvestor` CLI."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from myinvestor.auth import Authenticator, stored_password_relogin
from myinvestor.cli_errors import EXIT_CODES_HELP, record_format
from myinvestor.client import MyInvestorClient, ensure_session
from myinvestor.code_input import CodeSource, CodeSourcesUI, code_prompt, open_tty, preset_code
from myinvestor.errors import InputError, SignatureError
from myinvestor.http import build_http_client
from myinvestor.json_envelope import configure as configure_envelope
from myinvestor.output import OutputFormat, StderrConsole, notices, set_wide
from myinvestor.signature import (
    DEFAULT_APPROVAL_TIMEOUT,
    OTP_METHODS,
    Challenge,
    SignatureRequest,
    SignatureUI,
    Signer,
)
from myinvestor.stores import SessionStore, StoreKind, resolve_store
from myinvestor.verbosity import configure as configure_verbosity

# Never render locals in tracebacks: frames hold the password and tokens.
app = typer.Typer(
    no_args_is_help=True,
    help="CLI no oficial (solo lectura) para MyInvestor.",
    epilog=EXIT_CODES_HELP,
    pretty_exceptions_show_locals=False,
)
err = notices  # informative messages: silenced by --quiet
fatal = StderrConsole()  # errors: shown even with --quiet
OTP_ENV = "MYINVESTOR_OTP"

FormatOpt = Annotated[
    OutputFormat,
    typer.Option("--format", "-f", help="Formato de salida (con json, los errores también).", callback=record_format),
]
JsonOnlyFormatOpt = Annotated[
    OutputFormat,
    typer.Option(
        "--format",
        "-f",
        help="Siempre JSON: se acepta `-f json` como en las demás órdenes (con json, los errores también).",
        callback=record_format,
    ),
]
FromOpt = Annotated[str | None, typer.Option("--from", help="Desde (AAAA-MM-DD).")]
ToOpt = Annotated[str | None, typer.Option("--to", help="Hasta (AAAA-MM-DD), por defecto hoy.")]
DaysOpt = Annotated[int | None, typer.Option(min=1, help="Últimos N días (hasta hoy o --to).")]


@dataclass
class _GlobalOptions:
    kind: StoreKind = StoreKind.auto
    path: Path | None = None
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT
    approve: bool = False
    no_signature: bool = False
    min_interval: float = 0.0
    retry_blocked: int = 0


options = _GlobalOptions()


def package_version() -> str:
    try:
        return version("myinvestor")
    except PackageNotFoundError:
        return "desconocida"


def _show_version(value: bool) -> None:
    if value:
        print(f"myinvestor {package_version()}")
        raise typer.Exit()


@app.callback()
def _global_options(
    ctx: typer.Context,
    store: Annotated[
        StoreKind,
        typer.Option(
            "--store",
            envvar="MYINVESTOR_STORE",
            help="Dónde guardar la sesión: keyring, file (0600) o auto (keyring si hay, si no file).",
        ),
    ] = StoreKind.auto,
    store_path: Annotated[
        Path | None,
        typer.Option(envvar="MYINVESTOR_STORE_PATH", help="Ruta del fichero con --store file."),
    ] = None,
    approval_timeout: Annotated[
        float,
        typer.Option(help="Segundos de espera para aprobar en la app del móvil cuando MyInvestor lo pide."),
    ] = DEFAULT_APPROVAL_TIMEOUT,
    approve: Annotated[
        bool,
        typer.Option(
            "--approve",
            envvar="MYINVESTOR_APPROVE",
            help="Esperar la aprobación en la app del móvil aunque no haya terminal interactiva "
            "(p. ej. desde un script). Sin esto, fuera de una terminal falla en vez de mandarte una notificación. "
            "Solo vale para aprobar en la app, no para códigos SMS (MYINVESTOR_OTP, --code-stdin, --start/--code).",
        ),
    ] = False,
    no_signature: Annotated[
        bool,
        typer.Option(
            "--no-signature",
            envvar="MYINVESTOR_NO_SIGNATURE",
            help="No iniciar nunca un segundo factor (SMS/app), ni siquiera en una terminal o con --approve: "
            "lo que lo pida falla con el código 13.",
        ),
    ] = False,
    wide: Annotated[
        bool,
        typer.Option("--wide", envvar="MYINVESTOR_WIDE", help="Tablas a todo lo ancho, una línea por fila."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", envvar="MYINVESTOR_QUIET", help="Solo errores en stderr (para cron)."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Una línea por petición en stderr (método, ruta, status, ms), sin tokens ni contraseñas.",
        ),
    ] = False,
    min_interval: Annotated[
        float,
        typer.Option(min=0, envvar="MYINVESTOR_MIN_INTERVAL", help="Segundos mínimos entre peticiones."),
    ] = 0.0,
    retry_blocked: Annotated[
        int,
        typer.Option(
            min=0,
            envvar="MYINVESTOR_RETRY_BLOCKED",
            help="Si Imperva bloquea una consulta (GET), esperar (30 s, 2 min, 5 min…) y reintentarla hasta N veces. "
            "Nunca se repiten login, refresh ni firmas.",
        ),
    ] = 0,
    json_envelope: Annotated[
        bool,
        typer.Option(
            "--json-envelope",
            envvar="MYINVESTOR_JSON_ENVELOPE",
            help='Con -f json, envolver la salida en {"schema": "myinvestor.<orden>/1", "cli_version", "as_of", '
            '"data"} (ver docs/json.md y `myinvestor schema`). Sin esto, la salida de siempre.',
        ),
    ] = False,
    _version: Annotated[
        bool,
        typer.Option("--version", is_eager=True, callback=_show_version, help="Muestra la versión del CLI y sale."),
    ] = False,
) -> None:
    set_wide(wide)
    configure_envelope(
        enabled=json_envelope,
        command=ctx.invoked_subcommand or "",
        cli_version=package_version(),
        as_of=iso_moment(now_local()),
    )
    if quiet and verbose:
        raise InputError("Usa --quiet o --verbose, no ambos.")
    configure_verbosity(quiet=quiet, verbose=verbose)
    options.min_interval = min_interval
    options.retry_blocked = retry_blocked
    options.kind = store
    options.path = store_path
    options.approval_timeout = approval_timeout
    options.approve = approve
    options.no_signature = no_signature


def get_store() -> SessionStore:
    """Call it as `cli_common.get_store()` (never import the name): tests replace it in this one place."""
    return resolve_store(options.kind, options.path)


def stdin_is_tty() -> bool:
    return sys.stdin.isatty()


class TerminalSignatureUI:
    """Second factors from the terminal: type the SMS code, or approve in the mobile app.

    `context` names what is being approved (e.g. the account whose movements are requested), since
    MyInvestor asks once per request and two identical prompts look like a bug.
    """

    def __init__(self) -> None:
        self.context: str | None = None

    def ask_code(self, challenge: Challenge, attempt: int) -> str | None:
        return typer.prompt(code_prompt(challenge, attempt), default="", show_default=False) or None

    def approve_in_app(self, request: SignatureRequest) -> None:
        label = request.description or self.context
        what = f" «{escape(label)}»" if label else ""
        err.print(
            f"[yellow]MyInvestor pide confirmar{what} en la app del móvil.[/yellow] "
            f"Apruébalo allí; espero hasta {int(options.approval_timeout)} s…"
        )


class PipedSignatureUI(TerminalSignatureUI):
    """stdin is not a terminal: app approvals only with `--approve`, codes on /dev/tty if there is one (like ssh/gpg).

    The terminal is opened only when a code is asked (or to check, right before starting one, that there is
    one) and closed right after. Without it (cron, CI, a detached agent) no code method is started, and
    without `--approve` no app approval is: nobody gets a notification they cannot answer.
    """

    def __init__(self, *, approve: bool) -> None:
        super().__init__()
        self._approve = approve

    def supports(self, method: str) -> bool:
        if method in OTP_METHODS:
            return self._has_terminal()
        return self._approve

    def ask_code(self, challenge: Challenge, attempt: int) -> str | None:
        terminal = open_tty()
        if terminal is None:
            raise SignatureError(f"MyInvestor pide un código {challenge.method}: repite en una terminal interactiva.")
        with terminal:
            terminal.write(f"{code_prompt(challenge, attempt)}: ")
            terminal.flush()
            return terminal.readline().strip() or None

    def _has_terminal(self) -> bool:
        terminal = open_tty()
        if terminal is None:
            return False
        terminal.close()
        return True


def signature_ui(code_source: CodeSource | None = None) -> SignatureUI | None:
    """Ask in an interactive terminal; elsewhere only with `--approve`, so cron/CI never pings the phone.

    `MYINVESTOR_OTP`, then `code_source` (`--code-stdin`/`--code-fd`), answer codes before the terminal.
    `--no-signature` beats them all: no second factor is ever started.
    """
    if options.no_signature:
        return None
    otp = os.environ.get(OTP_ENV, "").strip()
    sources = [s for s in (preset_code(otp, OTP_ENV) if otp else None, code_source) if s is not None]
    ui = _interactive_ui()
    return CodeSourcesUI(sources, ui, _announce) if sources else ui


def _interactive_ui() -> TerminalSignatureUI:
    return TerminalSignatureUI() if stdin_is_tty() else PipedSignatureUI(approve=options.approve)


def _announce(message: str) -> None:
    # also with --quiet: it says where the code went
    fatal.print(message, markup=False, highlight=False, soft_wrap=True)


def build_client(store: SessionStore | None = None) -> MyInvestorClient:
    store = store or get_store()
    loaded = store.load()
    can_relogin = bool(loaded and loaded.customer_id and store.load_password(loaded.customer_id))
    session = ensure_session(loaded, can_relogin=can_relogin)
    http = build_http_client(min_interval=options.min_interval)
    ui = signature_ui()
    return MyInvestorClient(
        session,
        http=http,
        store=store,
        signature_ui=ui,
        signer_factory=partial(Signer, timeout=options.approval_timeout),
        relogin=stored_password_relogin(Authenticator(http, approval_timeout=options.approval_timeout), store, ui)
        if can_relogin
        else None,
        retry_blocked=options.retry_blocked,
        cache=True,
    )


def now_local() -> datetime:
    """Current time in the user's timezone: when a snapshot (`position`, `holdings`) was taken."""
    return datetime.now().astimezone()


def iso_moment(moment: datetime) -> str:
    """`2026-11-03T09:00:12+01:00`: ISO 8601 to the second, with the UTC offset."""
    return moment.isoformat(timespec="seconds")


def local_time(moment: datetime | None) -> str:
    """Token expiry is UTC; show it in the user's timezone."""
    return moment.astimezone().strftime("%H:%M %Z") if moment else "?"


def parse_day(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InputError(f"{option}: fecha no válida {value!r} (usa AAAA-MM-DD).") from exc


def only_json(fmt: OutputFormat, command: str) -> None:
    """For the commands whose output is always JSON: any other `-f` is a usage error."""
    if fmt is not OutputFormat.json:
        raise InputError(f"`{command}` solo sale en JSON: usa -f json o quita -f.")


def optional_range(date_from: str | None, date_to: str | None, days: int | None) -> tuple[date | None, date | None]:
    """`--from`/`--to`/`--days` as (start, end); None where the user gave no bound (`--days` ends today or `--to`)."""
    if date_from and days:
        raise InputError("Usa --from o --days, no ambos.")
    end = parse_day(date_to, "--to")
    start = parse_day(date_from, "--from") or ((end or date.today()) - timedelta(days=days) if days else None)
    if start and end and start > end:
        raise InputError("--from es posterior a --to.")
    return start, end
