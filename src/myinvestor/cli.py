"""`myinvestor` command-line interface (read-only)."""

from __future__ import annotations

import httpx
from rich.markup import escape

# Importing the command modules registers their commands on `app`.
from myinvestor import cli_accounts, cli_auth, cli_invest  # noqa: F401
from myinvestor.cli_common import app, err
from myinvestor.errors import MyInvestorError


def main() -> None:
    try:
        app()
    except MyInvestorError as exc:
        err.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        err.print(f"[red]Error de red:[/red] {type(exc).__name__}. MyInvestor no responde; reintenta en un rato.")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
