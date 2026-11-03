"""`login --direct`: in one go, or in two steps (`--start`, then `--code`) for agents without a terminal.

`--start` sends the SMS, saves a `PendingLogin` and exits with 13; `--code 123456` validates the code and
repeats the login with `x-signature-id`, which needs the password again (the web does the same). So
`--code` takes it from `--password-stdin`/`MYINVESTOR_PASSWORD`, from the pending login (only with
`--start --remember`), from the password already saved by `--remember`, or from the terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import typer

from myinvestor.auth import Authenticator
from myinvestor.cli_common import options, signature_ui
from myinvestor.cli_errors import exit_code
from myinvestor.code_input import CodeSource
from myinvestor.errors import EXIT_NETWORK, InputError, SignatureCodeRequired, WrongCodeError
from myinvestor.http import build_http_client
from myinvestor.session import Session
from myinvestor.stores import PendingLogin, SessionStore

ReadPassword = Callable[[str | None], str]  # (saved password to fall back on) -> password


@dataclass(frozen=True)
class DirectLogin:
    remember: bool = False
    start: bool = False
    code: str | None = None
    code_source: CodeSource | None = None


def direct_login(store: SessionStore, base: Session, how: DirectLogin, read_password: ReadPassword) -> Session:
    """Log in (or run one of the two steps) and save the password if asked; the session is saved by the caller."""
    if how.code is not None:
        return _finish(store, base, how.code, remember=how.remember, read_password=read_password)
    user = base.customer_id or typer.prompt("DNI/NIE")
    password = read_password(store.load_password(user) if how.start else None)
    with build_http_client() as http:
        auth = Authenticator(http)
        if how.start:
            result = auth.start_login(
                user, password, base, keep_password=how.remember, send_code=not options.no_signature
            )
        else:
            result = auth.login_direct(user, password, base, signature_ui(how.code_source))
    if isinstance(result, PendingLogin):
        store.save_pending_login(result)
        raise SignatureCodeRequired(_code_sent(result))
    if how.remember:  # only once MyInvestor accepted it
        store.save_password(user, password)
    return result


def _finish(store: SessionStore, base: Session, code: str, *, remember: bool, read_password: ReadPassword) -> Session:
    pending = store.load_pending_login()
    if pending is None:
        raise InputError("No hay ningún login pendiente: empieza con `myinvestor login --direct --start`.")
    if base.customer_id and base.customer_id != pending.customer_id:
        raise InputError("El login pendiente es de otro usuario: repite `myinvestor login --direct --start`.")
    password = read_password(pending.password or store.load_password(pending.customer_id))
    try:
        with build_http_client() as http:
            session = Authenticator(http).finish_login(pending, password, code, signature_ui())
    except WrongCodeError as exc:
        _after_wrong_code(store, pending, exc)
        raise
    except Exception as exc:
        if exit_code(exc) != EXIT_NETWORK:  # retryable failures keep it, so `--code` can be repeated
            store.clear_pending_login()
        raise
    store.clear_pending_login()
    if remember or pending.password:
        store.save_password(pending.customer_id, password)
    return session


def _after_wrong_code(store: SessionStore, pending: PendingLogin, exc: WrongCodeError) -> None:
    if exc.attempts_left:
        store.save_pending_login(pending.with_wrong_code())
    else:
        store.clear_pending_login()


def _code_sent(pending: PendingLogin) -> str:
    where = f" a {pending.destination}" if pending.destination else ""
    return (
        f"MyInvestor ha enviado un código {pending.method}{where}. Complétalo con "
        "`myinvestor login --direct --code <código>`."
    )
