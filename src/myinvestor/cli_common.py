"""Shared Typer app, global options and helpers for the `myinvestor` CLI."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from myinvestor.auth import Authenticator, stored_password_relogin
from myinvestor.client import MyInvestorClient, ensure_session
from myinvestor.errors import InputError, SignatureError
from myinvestor.http import build_http_client
from myinvestor.output import OutputFormat, set_wide
from myinvestor.signature import DEFAULT_APPROVAL_TIMEOUT, Challenge, SignatureRequest, SignatureUI, Signer
from myinvestor.stores import SessionStore, StoreKind, resolve_store

# Never render locals in tracebacks: frames hold the password and tokens.
app = typer.Typer(
    no_args_is_help=True,
    help="CLI no oficial (solo lectura) para MyInvestor.",
    pretty_exceptions_show_locals=False,
)
err = Console(stderr=True)

FormatOpt = Annotated[OutputFormat, typer.Option("--format", "-f", help="Formato de salida.")]


@dataclass
class _GlobalOptions:
    kind: StoreKind = StoreKind.auto
    path: Path | None = None
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT
    approve: bool = False


options = _GlobalOptions()


@app.callback()
def _global_options(
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
            "(p. ej. desde un script). Sin esto, fuera de una terminal falla en vez de mandarte una notificación.",
        ),
    ] = False,
    wide: Annotated[
        bool,
        typer.Option("--wide", envvar="MYINVESTOR_WIDE", help="Tablas a todo lo ancho, una línea por fila."),
    ] = False,
) -> None:
    set_wide(wide)
    options.kind = store
    options.path = store_path
    options.approval_timeout = approval_timeout
    options.approve = approve


def get_store() -> SessionStore:
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
        where = f" enviado a {challenge.destination}" if challenge.destination else ""
        retry = " (incorrecto, prueba otra vez)" if attempt > 1 else ""
        return typer.prompt(f"Código {challenge.method}{where}{retry}", default="", show_default=False) or None

    def approve_in_app(self, request: SignatureRequest) -> None:
        label = request.description or self.context
        what = f" «{escape(label)}»" if label else ""
        err.print(
            f"[yellow]MyInvestor pide confirmar{what} en la app del móvil.[/yellow] "
            f"Apruébalo allí; espero hasta {int(options.approval_timeout)} s…"
        )


class AppOnlySignatureUI(TerminalSignatureUI):
    """`--approve` without a terminal: app approvals need no input, codes can't be typed."""

    def ask_code(self, challenge: Challenge, attempt: int) -> str | None:
        raise SignatureError(f"MyInvestor pide un código {challenge.method}: repite en una terminal interactiva.")


def signature_ui() -> SignatureUI | None:
    """Ask in an interactive terminal; elsewhere only with `--approve`, so cron/CI never pings the phone."""
    if stdin_is_tty():
        return TerminalSignatureUI()
    return AppOnlySignatureUI() if options.approve else None


def build_client(store: SessionStore | None = None) -> MyInvestorClient:
    store = store or get_store()
    loaded = store.load()
    can_relogin = bool(loaded and loaded.customer_id and store.load_password(loaded.customer_id))
    session = ensure_session(loaded, can_relogin=can_relogin)
    http = build_http_client()
    ui = signature_ui()
    return MyInvestorClient(
        session,
        http=http,
        store=store,
        signature_ui=ui,
        signer_factory=partial(Signer, timeout=options.approval_timeout),
        relogin=stored_password_relogin(Authenticator(http), store, ui) if can_relogin else None,
    )


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
