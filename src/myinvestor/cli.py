"""`myinvestor` command-line interface (read-only)."""

from __future__ import annotations

import sys
from typing import NoReturn

import httpx
from rich.markup import escape

# Importing the command modules registers their commands on `app`.
from myinvestor import (  # noqa: F401
    cli_accounts,
    cli_allocation,
    cli_auth,
    cli_doctor,
    cli_export,
    cli_income,
    cli_invest,
    cli_state,
    cli_transfers,
    cli_upcoming,
)
from myinvestor.cli_common import app, fatal
from myinvestor.cli_errors import exit_code, json_error, requested
from myinvestor.errors import MyInvestorError


def _fail(exc: Exception, label: str, message: str) -> NoReturn:
    """Exit with the code of the error type; with `-f json` the error goes to stderr as one JSON line."""
    if requested.json:
        sys.stderr.write(json_error(exc, message) + "\n")
    else:
        fatal.print(f"[red]{label}:[/red] {escape(message)}")
    raise SystemExit(exit_code(exc)) from exc


def main() -> None:
    requested.json = False
    try:
        app()
    except MyInvestorError as exc:
        _fail(exc, "Error", str(exc))
    except httpx.HTTPError as exc:
        _fail(exc, "Error de red", f"{type(exc).__name__}. MyInvestor no responde; reintenta en un rato.")


if __name__ == "__main__":
    main()
