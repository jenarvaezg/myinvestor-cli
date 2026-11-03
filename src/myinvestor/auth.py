"""Keycloak login and token refresh against `api.myinvestor.es/login`.

- `POST /login/api/v2/auth/token` `{customerId, password}`: the web adds a reCAPTCHA Enterprise
  token (`x-recaptcha-token`, `x-recaptcha-action: LOGIN_V2`) that a CLI cannot produce, so
  `login_direct` is a best-effort path; the default login runs in a real browser
  (`myinvestor.browser_login`). An unknown device answers with a signature request (SMS/app)
  and the call is repeated with `x-signature-id`.
- `POST /login/api/v1/auth/token/refresh` `{refreshToken}` (no Authorization header) returns a
  new access/refresh pair. Access tokens live 15 min, refresh tokens 30 min.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from myinvestor import envelope
from myinvestor.config import API_BASE
from myinvestor.errors import (
    ApiError,
    AuthError,
    InvalidCredentialsError,
    PasswordChangeRequired,
    SessionExpiredError,
    error_code,
    raise_for_response,
)
from myinvestor.session import Session
from myinvestor.signature import SIGNATURE_HEADER, SignatureUI, Signer, parse_signature_request
from myinvestor.stores import SessionStore

Relogin = Callable[[Session], Session]

LOGIN_PATH = "/login/api/v2/auth/token"
REFRESH_PATH = "/login/api/v1/auth/token/refresh"


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
            code = error_code(body)
            raise InvalidCredentialsError(code or "MyInvestor no acepta el login sin reCAPTCHA o la contraseña")
        raise_for_response(response)
        return envelope.data(envelope.decode(response))

    def _public_call(self, headers: dict[str, str]) -> Any:
        def call(method: str, path: str, *, params: Any = None, json: Any = None) -> Any:
            response = self._http.request(method, f"{API_BASE}{path}", params=params, json=json, headers=headers)
            raise_for_response(response)
            return envelope.data(envelope.decode(response))

        return call


def stored_password_relogin(auth: Authenticator, store: SessionStore, ui: SignatureUI | None) -> Relogin:
    """Callback that logs in again with the password saved by `login --direct --remember`.

    A rejected password is deleted on the spot: retrying it on every cron run would lock the account.
    """

    def relogin(current: Session) -> Session:
        password = store.load_password(current.customer_id) if current.customer_id else None
        if not current.customer_id or not password:
            raise SessionExpiredError()
        try:
            return auth.login_direct(current.customer_id, password, current, ui)
        except (InvalidCredentialsError, PasswordChangeRequired) as exc:
            store.delete_password(current.customer_id)
            raise AuthError(
                f"El re-login automático ha fallado ({exc}). He borrado la contraseña guardada para no "
                "bloquear la cuenta; ejecuta `myinvestor login --direct --remember`."
            ) from exc

    return relogin
