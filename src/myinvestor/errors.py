"""Exception hierarchy and mapping from MyInvestor error responses.

Every response uses the envelope `{"payload": {"data": ...}, "status": {"code", "message", "timestamp"}}`;
errors keep it (`status.code` = `BAD_REQUEST`, `ERROR`…) and login errors add `payload.data.errCode`.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx


class MyInvestorError(Exception):
    """Base class for every error raised by this library."""


class ApiError(MyInvestorError):
    def __init__(self, status: int, message: str, path: str | None = None, code: str | None = None) -> None:
        self.status = status
        self.message = message
        self.path = path
        self.code = code
        super().__init__(f"HTTP {status} en {path or '?'}: {message}")


class InputError(MyInvestorError, ValueError):
    """Invalid user input (unknown account, bad date range...)."""


class CorruptStoreError(MyInvestorError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"El fichero de sesión {path} está corrupto. Bórralo y vuelve a ejecutar `myinvestor login`.")


class CorruptKeyringError(MyInvestorError):
    def __init__(self, service: str) -> None:
        super().__init__(
            f"La sesión guardada en el llavero ({service}) está corrupta. "
            "Ejecuta `myinvestor logout --forget-device` y vuelve a entrar."
        )


class NetworkBlockedError(MyInvestorError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(
            f"Imperva (el WAF de MyInvestor) ha bloqueado la petición (HTTP {status}). "
            "Suele ser por la IP (VPN, datacenter, CI): prueba desde otra red o más tarde."
        )


class AuthError(MyInvestorError):
    """Login failed or the session is gone."""


class InvalidCredentialsError(AuthError):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(f"Login rechazado{f' ({detail})' if detail else ''}.")


class PasswordChangeRequired(AuthError):
    def __init__(self) -> None:
        super().__init__("MyInvestor exige cambiar la contraseña. Hazlo en la web o la app y vuelve a entrar.")


class NotLoggedInError(AuthError):
    def __init__(self) -> None:
        super().__init__("No hay sesión. Ejecuta `myinvestor login`.")


class SessionExpiredError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "La sesión ha caducado (el refresh token dura 30 min sin uso). Ejecuta `myinvestor login` de nuevo."
        )


class SignatureError(MyInvestorError):
    """A second-factor approval was rejected, expired or cannot be done from the CLI."""


class SignatureRequired(SignatureError):
    def __init__(self, methods: list[str]) -> None:
        self.methods = methods
        super().__init__(
            "MyInvestor pide confirmar esta consulta con un segundo factor "
            f"({', '.join(methods) or '?'}). Repite en una terminal interactiva, o con "
            "`myinvestor --approve …` si basta con aprobarla en la app del móvil."
        )


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
