"""`login`, `logout` and `status`."""

from __future__ import annotations

import getpass
import os
import sys
from typing import Annotated

import typer

from myinvestor.auth import Authenticator
from myinvestor.browser_login import DEFAULT_LOGIN_TIMEOUT, browser_login
from myinvestor.cli_common import app, build_client, err, get_store, local_time, signature_ui, stdin_is_tty
from myinvestor.errors import InputError, NotLoggedInError, SessionExpiredError
from myinvestor.http import build_http_client
from myinvestor.session import Session, new_device_id

PASSWORD_ENV = "MYINVESTOR_PASSWORD"


def _read_secret(from_stdin: bool, prompt: str, env: str | None = None) -> str:
    if from_stdin:
        value = sys.stdin.readline().strip()
    elif env and os.environ.get(env):
        value = os.environ[env]
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
            help="Login por terminal con DNI/NIE y contraseña, sin navegador ni reCAPTCHA. "
            "Un dispositivo nuevo pide además SMS o aprobación en la app.",
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
            help="--direct: guardar la contraseña (Keychain, o el fichero 0600 con --store file) para volver a "
            "entrar solo cuando caduque la sesión. Se borra con `logout` o si MyInvestor la rechaza."
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
) -> None:
    """Inicia sesión. Por defecto abre un navegador: entras como siempre y el CLI se queda con la sesión."""
    if direct and refresh_token_stdin:
        raise InputError("Usa --direct o --refresh-token-stdin, no ambos.")
    if remember and not direct:
        raise InputError("--remember solo tiene sentido con --direct (los otros logins no usan la contraseña).")
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
        user = base.customer_id or typer.prompt("DNI/NIE")
        password = _read_secret(password_stdin, "Contraseña", PASSWORD_ENV)
        with build_http_client() as http:
            session = Authenticator(http).login_direct(user, password, base, signature_ui())
        if remember:  # only once MyInvestor accepted it
            store.save_password(user, password)
    else:
        err.print("Abriendo el navegador: inicia sesión en MyInvestor y espera a que se cierre solo…")
        session = browser_login(timeout=timeout)

    if previous and previous.customer_id and previous.customer_id != session.customer_id:
        store.delete_password(previous.customer_id)
    store.save(session)
    if session.customer_id:  # a manual login resumes a paused unattended re-login
        store.clear_relogin_block(session.customer_id)
    err.print(
        f"[green]Sesión iniciada[/green] (token hasta las {local_time(session.expires_at)}, "
        f"renovable hasta las {local_time(session.refresh_expires_at)} si no se usa)."
    )


@app.command()
def logout(
    forget_device: Annotated[
        bool, typer.Option(help="Olvidar también el id de dispositivo (el próximo login pedirá segundo factor).")
    ] = False,
) -> None:
    """Borra la sesión local y la contraseña guardada. Los tokens ya emitidos caducan solos (≤ 30 min)."""
    store = get_store()
    session = store.load()
    if session and session.customer_id:
        store.delete_password(session.customer_id)
    if forget_device or session is None:
        store.clear()
    else:
        store.save(session.without_tokens())
    err.print("Sesión cerrada.")


@app.command()
def status() -> None:
    """Estado de la sesión; si sigue viva, la renueva y confirma con MyInvestor."""
    store = get_store()
    session = store.load()
    if session is None or not session.refresh_token:
        raise NotLoggedInError()
    remembered = bool(session.customer_id and store.load_password(session.customer_id))
    if not session.can_refresh() and session.is_expired() and not remembered:
        err.print(f"[yellow]Sesión caducada[/yellow] (dispositivo {session.device_id}). Ejecuta `myinvestor login`.")
        raise typer.Exit(SessionExpiredError.exit_code)
    with build_client(store) as client:
        customer = client.customer()
        session = client.session
    name = customer.get("name") or session.name or "?"
    renewal = (
        "re-login automático con la contraseña guardada"
        if remembered
        else f"se renueva sola si la usas antes de las {local_time(session.refresh_expires_at)}"
    )
    err.print(
        f"[green]Sesión activa[/green] como {name} · dispositivo {session.device_id}\n"
        f"Token hasta las {local_time(session.expires_at)}; {renewal}."
    )
