"""Exit codes of the CLI and the machine-readable error line printed with `-f json`."""

from __future__ import annotations

import json
import re

import httpx

from myinvestor.errors import (
    EXIT_CORRUPT_STORE,
    EXIT_CREDENTIALS,
    EXIT_ERROR,
    EXIT_NETWORK,
    EXIT_NO_SESSION,
    EXIT_SIGNATURE,
    EXIT_USAGE,
    MyInvestorError,
)
from myinvestor.output import OutputFormat

EXIT_CODES = (
    (0, "OK"),
    (EXIT_ERROR, "otro error"),
    (EXIT_USAGE, "uso o datos de entrada incorrectos"),
    (EXIT_CREDENTIALS, "contraseña rechazada (la guardada se borra) o cambio de contraseña obligatorio"),
    (EXIT_NO_SESSION, "sin sesión, o caducada sin forma de renovarla (incluye el re-login en pausa)"),
    (EXIT_NETWORK, "red, WAF (Imperva) o MyInvestor caído: reintentable"),
    (EXIT_SIGNATURE, "hace falta un segundo factor (SMS/app) que no se puede dar sin terminal"),
    (EXIT_CORRUPT_STORE, "sesión guardada corrupta"),
)

EXIT_CODES_HELP = "\n\n".join(["Códigos de salida:", *(f"  {code:>2}  {meaning}" for code, meaning in EXIT_CODES)])


class _RequestedFormat:
    """`-f` of the running command, so errors can be reported in the same format."""

    json = False


requested = _RequestedFormat()


def record_format(value: OutputFormat | None) -> OutputFormat | None:
    """Typer callback for `--format`: remembers whether the caller wants JSON."""
    requested.json = value == OutputFormat.json
    return value


def error_kind(exc: BaseException) -> str:
    """Stable snake_case id of the error (`SignatureRequired` → `signature_required`)."""
    if isinstance(exc, httpx.HTTPError):
        return "network"
    name = type(exc).__name__.removesuffix("Error") or type(exc).__name__
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def exit_code(exc: BaseException) -> int:
    if isinstance(exc, MyInvestorError):
        return exc.exit_code
    return EXIT_NETWORK if isinstance(exc, httpx.HTTPError) else EXIT_ERROR


def json_error(exc: BaseException, message: str) -> str:
    return json.dumps({"error": error_kind(exc), "exit_code": exit_code(exc), "message": message}, ensure_ascii=False)
