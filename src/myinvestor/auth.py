"""Keycloak login and token refresh against `api.myinvestor.es/login`.

- `POST /login/api/v2/auth/token` `{customerId, password}`: the web adds a reCAPTCHA Enterprise
  token (`x-recaptcha-token`, `x-recaptcha-action: LOGIN_V2`) that a CLI cannot produce, so
  `login_direct` is a best-effort path; the default login runs in a real browser
  (`myinvestor.browser_login`). An unknown device answers with a signature request (SMS/app)
  and the call is repeated with `x-signature-id`.
- `POST /login/api/v1/auth/token/refresh` `{refreshToken}` (no Authorization header) returns a
  new access/refresh pair. Access tokens live 15 min, refresh tokens 30 min.

Rejected logins answer 400/401/403 with `payload.data.errCode`. The web (bundle of 2026-11) knows:

- `UNAUTHORIZED`: wrong customer id or password. The form shows «¡Ups! Parece que algo no funciona.
  Si no recuerdas la contraseña…» with a link to reset it. The web also assumes it when there is no
  `errCode`; here a missing code is not proof enough to delete a saved password.
- `UPDATE_PASSWORD_REQUIRED`: the password must be changed (the web opens that flow); a 200 can also
  say so with `isPasswordChangeRequired`.
- anything else: the web only shows a generic «oops» toast, so its meaning is unknown.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from myinvestor import envelope
from myinvestor.config import API_BASE, LOGIN_PATH, REFRESH_PATH
from myinvestor.errors import (
    ApiError,
    AuthError,
    InvalidCredentialsError,
    LoginRejectedError,
    PasswordChangeRequired,
    ReloginBlockedError,
    SessionExpiredError,
    StoredPasswordRejectedError,
    error_code,
    raise_for_response,
)
from myinvestor.session import Session
from myinvestor.signature import SIGNATURE_HEADER, SignatureUI, Signer, parse_signature_request
from myinvestor.stores import ReloginBlock, SessionStore

Relogin = Callable[[Session], Session]

CREDENTIALS_ERR_CODES = frozenset({"UNAUTHORIZED"})
PASSWORD_CHANGE_ERR_CODES = frozenset({"UPDATE_PASSWORD_REQUIRED"})
RELOGIN_PAUSE = timedelta(hours=24)


def _login_rejection(status: int, err_code: str | None) -> AuthError:
    if err_code in CREDENTIALS_ERR_CODES:
        return InvalidCredentialsError(f"{err_code}: usuario o contraseña incorrectos")
    if err_code in PASSWORD_CHANGE_ERR_CODES:
        return PasswordChangeRequired()
    return LoginRejectedError(status, err_code)


def _tokens(data: Any) -> tuple[str, str | None]:
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise ApiError(200, "respuesta de login sin accessToken", LOGIN_PATH)
    return str(data["accessToken"]), data.get("refreshToken")


class Authenticator:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def _post(self, path: str, body: Any, headers: dict[str, str]) -> httpx.Response:
        return self._http.post(f"{API_BASE}{path}", json=body, headers=headers)

    def refresh(self, session: Session) -> Session:
        if not session.refresh_token or not session.can_refresh():
            raise SessionExpiredError()
        response = self._post(REFRESH_PATH, {"refreshToken": session.refresh_token}, {"x-device-id": session.device_id})
        if response.status_code in (400, 401, 403) and "text/html" not in response.headers.get("content-type", ""):
            raise SessionExpiredError()
        raise_for_response(response)
        access, refresh = _tokens(envelope.data(envelope.decode(response)))
        return session.with_tokens(access, refresh)

    def login_direct(self, customer_id: str, password: str, session: Session, ui: SignatureUI | None) -> Session:
        """Login without reCAPTCHA. MyInvestor may reject it; then use the browser login."""
        headers = {"x-device-id": session.device_id}
        body = {"customerId": customer_id, "password": password}
        response = self._post(LOGIN_PATH, body, headers)
        payload = self._login_payload(response)
        request = parse_signature_request(payload)
        if request is not None and request.is_pending:
            signer = Signer(self._public_call(headers), public=True)
            signer.complete(request, ui)
            response = self._post(LOGIN_PATH, body, {**headers, SIGNATURE_HEADER: request.id})
            payload = self._login_payload(response)
        if isinstance(payload, dict) and payload.get("isPasswordChangeRequired"):
            raise PasswordChangeRequired()
        access, refresh = _tokens(payload)
        return Session(device_id=session.device_id, customer_id=customer_id, access_token=access, refresh_token=refresh)

    def _login_payload(self, response: httpx.Response) -> Any:
        if response.status_code in (400, 401, 403) and "text/html" not in response.headers.get("content-type", ""):
            body = envelope.decode(response) if response.content else None
            raise _login_rejection(response.status_code, error_code(body))
        raise_for_response(response)
        return envelope.data(envelope.decode(response))

    def _public_call(self, headers: dict[str, str]) -> Any:
        def call(method: str, path: str, *, params: Any = None, json: Any = None) -> Any:
            response = self._http.request(method, f"{API_BASE}{path}", params=params, json=json, headers=headers)
            raise_for_response(response)
            return envelope.data(envelope.decode(response))

        return call


def _when(moment: datetime) -> str:
    return moment.astimezone().strftime("%d/%m %H:%M")


def stored_password_relogin(auth: Authenticator, store: SessionStore, ui: SignatureUI | None) -> Relogin:
    """Callback that logs in again with the password saved by `login --direct --remember`.

    Retrying a bad password on every cron run would lock the account, so:
    - wrong password (or a forced change): the saved password is deleted on the spot;
    - any other rejection: the password is kept, but the re-login pauses for `RELOGIN_PAUSE` or until a
      manual `login`, so it is tried at most once a day;
    - a second factor that cannot be given (no terminal) or a network error: nothing changes.
    """

    def relogin(current: Session) -> Session:
        customer_id = current.customer_id
        password = store.load_password(customer_id) if customer_id else None
        if not customer_id or not password:
            raise SessionExpiredError()
        block = store.load_relogin_block(customer_id)
        if block is not None and block.is_active():
            raise ReloginBlockedError(
                f"El re-login automático está en pausa hasta el {_when(block.until)} porque MyInvestor lo "
                f"rechazó ({block.error}). La contraseña guardada sigue ahí; para reanudarlo ya, ejecuta "
                "`myinvestor login --direct --remember`."
            )
        try:
            session = auth.login_direct(customer_id, password, current, ui)
        except (InvalidCredentialsError, PasswordChangeRequired) as exc:
            store.delete_password(customer_id)
            raise StoredPasswordRejectedError(
                f"El re-login automático ha fallado ({str(exc).rstrip('.')}). He borrado la contraseña guardada "
                "para no bloquear la cuenta; ejecuta `myinvestor login --direct --remember`."
            ) from exc
        except LoginRejectedError as exc:
            paused = ReloginBlock(until=datetime.now(tz=UTC) + RELOGIN_PAUSE, error=exc.summary)
            store.save_relogin_block(customer_id, paused)
            raise ReloginBlockedError(
                f"MyInvestor ha rechazado el re-login automático ({exc.summary}), pero no parece una contraseña "
                f"incorrecta: no he borrado la contraseña guardada. No reintentaré hasta el {_when(paused.until)} "
                "o hasta que ejecutes `myinvestor login --direct --remember`."
            ) from exc
        if block is not None:
            store.clear_relogin_block(customer_id)
        return session

    return relogin
