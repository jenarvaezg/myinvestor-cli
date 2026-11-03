"""Exception hierarchy and mapping from MyInvestor error responses.

Every response uses the envelope `{"payload": {"data": ...}, "status": {"code", "message", "timestamp"}}`;
errors keep it (`status.code` = `BAD_REQUEST`, `ERROR`…) and login errors add `payload.data.errCode`.

Every class carries the `exit_code` the CLI ends with (documented in the README and in `--help`):
1 other errors, 2 bad input, 10 credentials rejected (stored password deleted), 11 no session,
12 network/WAF/5xx (retryable), 13 second factor needed, 14 corrupt session store.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CREDENTIALS = 10
EXIT_NO_SESSION = 11
EXIT_NETWORK = 12
EXIT_SIGNATURE = 13
EXIT_CORRUPT_STORE = 14


class MyInvestorError(Exception):
    """Base class for every error raised by this library."""

    exit_code: int = EXIT_ERROR


class ApiError(MyInvestorError):
    def __init__(self, status: int, message: str, path: str | None = None, code: str | None = None) -> None:
        self.status = status
        self.message = message
        self.path = path
        self.code = code
        self.exit_code = EXIT_NETWORK if status >= 500 else EXIT_ERROR  # 5xx survived the retries
        super().__init__(f"HTTP {status} en {path or '?'}: {message}")


class InputError(MyInvestorError, ValueError):
    """Invalid user input (unknown account, bad date range...)."""

    exit_code = EXIT_USAGE


class CorruptStoreError(MyInvestorError):
    exit_code = EXIT_CORRUPT_STORE

    def __init__(self, path: Path) -> None:
        super().__init__(f"El fichero de sesión {path} está corrupto. Bórralo y vuelve a ejecutar `myinvestor login`.")


class CorruptKeyringError(MyInvestorError):
    exit_code = EXIT_CORRUPT_STORE

    def __init__(self, service: str) -> None:
        super().__init__(
            f"La sesión guardada en el llavero ({service}) está corrupta. "
            "Ejecuta `myinvestor logout --forget-device` y vuelve a entrar."
        )


class NetworkBlockedError(MyInvestorError):
    exit_code = EXIT_NETWORK

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(
            f"Imperva (el WAF de MyInvestor) ha bloqueado la petición (HTTP {status}). "
            "Suele ser por la IP (VPN, datacenter, CI): prueba desde otra red o más tarde."
        )


class AuthError(MyInvestorError):
    """Login failed or the session is gone."""


class InvalidCredentialsError(AuthError):
    """MyInvestor says the customer id or password is wrong (`errCode` `UNAUTHORIZED`)."""

    exit_code = EXIT_CREDENTIALS

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(f"Login rechazado{f' ({detail})' if detail else ''}.")


class StoredPasswordRejectedError(AuthError):
    """The unattended re-login was refused for good, so the saved password has been deleted."""

    exit_code = EXIT_CREDENTIALS


class LoginRejectedError(AuthError):
    """MyInvestor refused the login for a reason other than a wrong password (reCAPTCHA, rate limit...)."""

    def __init__(self, status: int, err_code: str | None) -> None:
        self.status = status
        self.err_code = err_code
        super().__init__(
            f"MyInvestor ha rechazado el login ({self.summary}). No parece una contraseña incorrecta: puede "
            "que exija reCAPTCHA o que haya bloqueado el acceso un rato. Prueba `myinvestor login` (navegador)."
        )

    @property
    def summary(self) -> str:
        return f"{f'errCode {self.err_code}' if self.err_code else 'sin errCode'}, HTTP {self.status}"


class ReloginBlockedError(AuthError):
    """The unattended re-login is paused after an unexplained rejection; the password is kept.

    Same exit code as an expired session: until a manual `login` there is no way to renew it.
    """

    exit_code = EXIT_NO_SESSION


class PasswordChangeRequired(AuthError):
    exit_code = EXIT_CREDENTIALS

    def __init__(self) -> None:
        super().__init__("MyInvestor exige cambiar la contraseña. Hazlo en la web o la app y vuelve a entrar.")


class NotLoggedInError(AuthError):
    exit_code = EXIT_NO_SESSION

    def __init__(self) -> None:
        super().__init__("No hay sesión. Ejecuta `myinvestor login`.")


class SessionExpiredError(AuthError):
    exit_code = EXIT_NO_SESSION

    def __init__(self) -> None:
        super().__init__(
            "La sesión ha caducado (el refresh token dura 30 min sin uso). Ejecuta `myinvestor login` de nuevo."
        )


class SignatureError(MyInvestorError):
    """A second-factor approval was rejected, expired or cannot be done from the CLI."""

    exit_code = EXIT_SIGNATURE


_CODE_HINT = (
    " Para un código SMS sin terminal: `myinvestor login --direct --start` y luego `--code`, "
    "o MYINVESTOR_OTP/--code-stdin (ver el README)."
)


class SignatureRequired(SignatureError):
    def __init__(self, methods: list[str]) -> None:
        self.methods = methods
        super().__init__(
            "MyInvestor pide confirmar esta consulta con un segundo factor "
            f"({', '.join(methods) or '?'}). Repite en una terminal interactiva, o con "
            "`myinvestor --approve …` si basta con aprobarla en la app del móvil."
            + (_CODE_HINT if any(m.startswith("OTP_") for m in methods) else "")
        )


class PartialResultError(SignatureError):
    """Part of the requested data needs a second factor that was not given; the rest was already printed."""


class SignatureCodeRequired(SignatureError):
    """`login --direct --start` sent the code and saved the login: `login --direct --code` completes it."""


class WrongCodeError(SignatureError):
    """`finish_login`: MyInvestor rejected the code; `attempts_left` more before the login must restart."""

    def __init__(self, attempts_left: int) -> None:
        self.attempts_left = max(attempts_left, 0)
        hint = (
            f"te quedan {self.attempts_left}: repite `myinvestor login --direct --code <código>`"
            if self.attempts_left
            else "no quedan más intentos, vuelve a empezar con `myinvestor login --direct --start`"
        )
        super().__init__(f"Código incorrecto; {hint}.")


def envelope_status(body: object) -> tuple[str | None, str | None]:
    if not isinstance(body, dict):
        return None, None
    status = body.get("status")
    if not isinstance(status, dict):
        return None, None
    code, message = status.get("code"), status.get("message")
    return (str(code) if code else None, str(message) if message else None)


def error_code(body: object) -> str | None:
    """`payload.data.errCode`, set by the login endpoint (the web maps it to a message)."""
    if isinstance(body, dict):
        data = (body.get("payload") or {}).get("data") if isinstance(body.get("payload"), dict) else None
        if isinstance(data, dict) and data.get("errCode"):
            return str(data["errCode"])
    return None


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def extract_error_message(response: httpx.Response) -> str:
    """Best-effort human message from an error response, without dumping whole bodies."""
    body = _json(response)
    if body is None:
        return response.text[:200] or response.reason_phrase
    code, message = envelope_status(body)
    parts = [p for p in (error_code(body), message or code) if p]
    return ": ".join(parts) if parts else str(body)[:200]


def _is_waf_block(response: httpx.Response) -> bool:
    if response.status_code not in (403, 429, 503):
        return False
    if "text/html" in response.headers.get("content-type", ""):
        return True
    return "_Incapsula_Resource" in response.text[:2000]


def raise_for_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    if _is_waf_block(response):
        raise NetworkBlockedError(response.status_code)
    if response.status_code == 401:
        raise SessionExpiredError()
    body = _json(response)
    code, _ = envelope_status(body)
    raise ApiError(response.status_code, extract_error_message(response), response.request.url.path, code)
