"""`login`, `logout` and `status`."""

from __future__ import annotations

import getpass
import os
import sys
from typing import Annotated, Any

import typer

from myinvestor.auth import Authenticator
from myinvestor.browser_login import DEFAULT_LOGIN_TIMEOUT, browser_login
from myinvestor.cli_common import FormatOpt, app, build_client, err, fatal, get_store, local_time, stdin_is_tty
from myinvestor.cli_direct_login import DirectLogin, direct_login
from myinvestor.code_input import CodeSource, fd_codes, line_codes
from myinvestor.errors import InputError, NotLoggedInError, SessionExpiredError
from myinvestor.http import build_http_client
from myinvestor.output import OutputFormat, print_json, render
from myinvestor.session import Session, new_device_id
from myinvestor.session_info import SessionInfo

PASSWORD_ENV = "MYINVESTOR_PASSWORD"


def _read_secret(from_stdin: bool, prompt: str, env: str | None = None, saved: str | None = None) -> str:
    if from_stdin:
        value = sys.stdin.readline().strip()
    elif env and os.environ.get(env):
        value = os.environ[env]
    elif saved:
        value = saved
    elif stdin_is_tty():
        value = getpass.getpass(f"{prompt}: ")
    else:
        raise InputError(f"Sin terminal: pasa el valor por stdin (--{'password' if env else 'refresh-token'}-stdin).")
    if not value:
        raise InputError(f"{prompt} vacío.")
    return value


@app.command()
def login(
    direct: Annotated[
        bool,
        typer.Option(
            "--direct",
            help="Login por terminal con DNI/NIE y contraseña, sin navegador ni reCAPTCHA: MyInvestor lo "
            "acepta desde un dispositivo conocido (verificado el 2026-11-03); si algún día lo rechaza, usa "
            "`myinvestor login` (navegador). Si MyInvestor pide un segundo factor: el código SMS por la "
            "terminal, --code-stdin, --code-fd, MYINVESTOR_OTP o --start/--code; la aprobación en la app, en "
            "una terminal o con --approve (que no sirve para el SMS). Para cron: --direct --remember con "
            "--store file.",
        ),
    ] = False,
    refresh_token_stdin: Annotated[
        bool,
        typer.Option(
            "--refresh-token-stdin",
            help="Importar un refresh token (localStorage `myinvestor-app:REFRESH_TOKEN` de la web) leído de stdin.",
        ),
    ] = False,
    customer_id: Annotated[
        str | None,
        typer.Option("--customer-id", "-u", envvar="MYINVESTOR_CUSTOMER_ID", help="DNI/NIE (usuario) para --direct."),
    ] = None,
    remember: Annotated[
        bool,
        typer.Option(
            help="--direct: guardar la contraseña (llavero del sistema, o el fichero 0600 con --store file) para "
            "volver a entrar solo cuando caduque la sesión. Se borra con `logout` o si MyInvestor dice que es "
            "incorrecta o que hay que cambiarla; otros rechazos solo pausan el re-login 24 h."
        ),
    ] = False,
    password_stdin: Annotated[
        bool, typer.Option(help=f"--direct: leer la contraseña de stdin (también vale {PASSWORD_ENV}).")
    ] = False,
    device_id: Annotated[
        str | None,
        typer.Option(
            help="Id de dispositivo (`x-device-id`). Con --refresh-token-stdin, el `_capuid` de la web que emitió "
            "el token. Por defecto se reutiliza el guardado."
        ),
    ] = None,
    timeout: Annotated[
        float, typer.Option(help="Segundos para completar el login en el navegador.")
    ] = DEFAULT_LOGIN_TIMEOUT,
    start: Annotated[
        bool,
        typer.Option(
            "--start",
            help="--direct en dos pasos: si MyInvestor pide un código, lo envía, deja el login pendiente y sale "
            "con 13. Si no le pasas la contraseña, usa la guardada con --remember.",
        ),
    ] = False,
    code: Annotated[
        str | None,
        typer.Option("--code", help="--direct en dos pasos: completa el login pendiente de --start con este código."),
    ] = None,
    code_stdin: Annotated[
        bool,
        typer.Option(help="--direct: leer el código SMS de la siguiente línea de stdin cuando se pida."),
    ] = False,
    code_fd: Annotated[
        int | None,
        typer.Option(
            min=0, help="--direct: leer el código SMS de este descriptor de fichero (p. ej. 3) cuando se pida."
        ),
    ] = None,
) -> None:
    """Inicia sesión. Por defecto abre un navegador: entras como siempre y el CLI se queda con la sesión."""
    if direct and refresh_token_stdin:
        raise InputError("Usa --direct o --refresh-token-stdin, no ambos.")
    if remember and not direct:
        raise InputError("--remember solo tiene sentido con --direct (los otros logins no usan la contraseña).")
    how = DirectLogin(remember=remember, start=start, code=code, code_source=_code_source(code_stdin, code_fd))
    _check_direct_options(direct, how)
    store = get_store()
    previous = store.load()
    device = device_id or (previous.device_id if previous else None) or new_device_id()
    base = Session(device_id=device, customer_id=customer_id or (previous.customer_id if previous else None))

    if refresh_token_stdin:
        token = _read_secret(True, "Refresh token")
        with build_http_client() as http:
            session = Authenticator(http).refresh(
                Session(device_id=device, customer_id=base.customer_id, refresh_token=token)
            )
    elif direct:
        session = direct_login(
            store, base, how, lambda saved: _read_secret(password_stdin, "Contraseña", PASSWORD_ENV, saved)
        )
    else:
        err.print("Abriendo el navegador: inicia sesión en MyInvestor y espera a que se cierre solo…")
        session = browser_login(timeout=timeout)

    if previous and previous.customer_id and previous.customer_id != session.customer_id:
        store.delete_password(previous.customer_id)
    store.save(session)
    store.clear_pending_login()  # a finished login makes any half-done `--start` useless
    if session.customer_id:  # a manual login resumes a paused unattended re-login
        store.clear_relogin_block(session.customer_id)
    err.print(
        f"[green]Sesión iniciada[/green] (token hasta las {local_time(session.expires_at)}, "
        f"renovable hasta las {local_time(session.refresh_expires_at)} si no se usa)."
    )


def _code_source(code_stdin: bool, code_fd: int | None) -> CodeSource | None:
    if code_stdin and code_fd is not None:
        raise InputError("Usa --code-stdin o --code-fd, no ambos.")
    if code_stdin:
        return line_codes(sys.stdin, "stdin")
    return fd_codes(code_fd) if code_fd is not None else None


def _check_direct_options(direct: bool, how: DirectLogin) -> None:
    if not direct and (how.start or how.code is not None or how.code_source):
        raise InputError("--start, --code, --code-stdin y --code-fd solo valen con --direct.")
    if how.start and how.code is not None:
        raise InputError("--start envía el código y --code lo completa: son dos ejecuciones distintas.")
    if how.code_source and (how.start or how.code is not None):
        raise InputError("--code-stdin/--code-fd son para el login en un paso, no con --start ni --code.")
    if how.code is not None and not how.code.strip():
        raise InputError("--code vacío.")


@app.command()
def logout(
    forget_device: Annotated[
        bool, typer.Option(help="Olvidar también el id de dispositivo (el próximo login pedirá segundo factor).")
    ] = False,
) -> None:
    """Borra la sesión local, la contraseña guardada, la pausa del re-login y el login pendiente de --start.

    Los tokens ya emitidos caducan solos (≤ 30 min).
    """
    store = get_store()
    session = store.load()
    store.clear_pending_login()
    if session and session.customer_id:
        store.delete_password(session.customer_id)
        store.clear_relogin_block(session.customer_id)
    if forget_device or session is None:
        store.clear()
    else:
        store.save(session.without_tokens())
    err.print("Sesión cerrada.")


@app.command()
def status(
    fmt: FormatOpt = OutputFormat.table,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Solo leer el almacén local: no llama a MyInvestor ni renueva la sesión."),
    ] = False,
) -> None:
    """Estado de la sesión. Sin --offline la confirma con MyInvestor, y eso la renueva si hace falta."""
    store = get_store()
    info = SessionInfo.read(store)
    session = info.session
    if session is None or not session.refresh_token:
        _exit_with_report(info, fmt)
        raise NotLoggedInError()
    if not session.can_refresh() and session.is_expired() and not info.can_relogin:
        _exit_with_report(info, fmt)
        fatal.print(f"[yellow]Sesión caducada[/yellow] (dispositivo {session.device_id}). Ejecuta `myinvestor login`.")
        raise typer.Exit(SessionExpiredError.exit_code)
    if offline:
        _print_status(info, session, fmt, name=None, verified=False)
        return
    with build_client(store) as client:
        customer = client.customer()
        session = client.session
    _print_status(info.with_session(session), session, fmt, name=customer.get("name"), verified=True)


def _exit_with_report(info: SessionInfo, fmt: OutputFormat) -> None:
    """Machine formats still get the report on stdout before exiting with 11."""
    if fmt is not OutputFormat.table:
        _emit(info.to_dict(), fmt)
        raise typer.Exit(SessionExpiredError.exit_code)


def _emit(report: dict[str, Any], fmt: OutputFormat) -> None:
    if fmt is OutputFormat.json:
        print_json(report)
    else:
        render([report], fmt)


def _print_status(info: SessionInfo, session: Session, fmt: OutputFormat, *, name: str | None, verified: bool) -> None:
    if fmt is not OutputFormat.table:
        _emit(info.to_dict(name=name, verified_online=verified), fmt)
        return
    renewal = (
        "re-login automático con la contraseña guardada"
        if info.can_relogin
        else f"se renueva sola si la usas antes de las {local_time(session.refresh_expires_at)}"
    )
    state = "[green]Sesión activa[/green]" if verified else "[green]Sesión guardada[/green] (sin comprobar: --offline)"
    err.print(
        f"{state} como {name or session.name or '?'} · dispositivo {session.device_id}\n"
        f"Token hasta las {local_time(session.expires_at)}; {renewal}."
    )
