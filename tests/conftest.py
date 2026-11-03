from __future__ import annotations

import base64
import json
import logging
import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import keyring.core
import pytest
import respx
import typer.rich_utils

from myinvestor import cli_common, cli_doctor, json_envelope, stores
from myinvestor.http import build_http_client
from myinvestor.output import notices
from myinvestor.session import Session

API = "https://api.myinvestor.es"
CPERF = f"{API}/cperf-server/api"
DEVICE = "11111111-2222-3333-4444-555555555555"
CASH_UUID = "aaaaaaaa-0000-0000-0000-000000000001"

# What rich (and typer's rich help and errors) read from the environment to pick colours, width and
# interactivity; `CI` and `CLICOLOR*` too, so no future reader of them depends on the runner.
TERMINAL_ENV = (
    "FORCE_COLOR",
    "NO_COLOR",
    "TERM",
    "COLORTERM",
    "COLUMNS",
    "LINES",
    "JUPYTER_COLUMNS",
    "JUPYTER_LINES",
    "TTY_COMPATIBLE",
    "TTY_INTERACTIVE",
    "CI",
    "CLICOLOR",
    "CLICOLOR_FORCE",
    "GITHUB_ACTIONS",
    "PY_COLORS",
    "TERMINAL_WIDTH",
    "_TYPER_FORCE_DISABLE_TERMINAL",
)
# Consoles created at import time: they read the environment pytest was started with, once.
MODULE_CONSOLES = (notices, cli_common.fatal, cli_doctor.out)


def make_jwt(expires_in: timedelta = timedelta(minutes=15), **claims: Any) -> str:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    exp = int((datetime.now(tz=UTC) + expires_in).timestamp())
    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, **claims})}.sig"


def ok(data: Any, **payload: Any) -> dict[str, Any]:
    """The success envelope every endpoint uses."""
    return {"payload": {"data": data, **payload}, "status": {"code": "OK", "timestamp": "2026-11-03T10:00:00.000Z"}}


def tokens(access: str | None = None, refresh: str | None = None) -> dict[str, Any]:
    return ok(
        {
            "accessToken": access or make_jwt(),
            "refreshToken": refresh or make_jwt(timedelta(minutes=30)),
            "expiresIn": 900,
            "refreshExpiresIn": 1800,
            "tokenType": "Bearer",
        }
    )


def cash_account(i: int = 1, **overrides: Any) -> dict[str, Any]:
    base = {
        "accountId": f"10{i}",
        "accountUuid": CASH_UUID if i == 1 else f"aaaaaaaa-0000-0000-0000-00000000000{i}",
        "accountCode": f"c{i}",
        "alias": f"Cuenta {i}",
        "iban": f"ES00 1544 0000 0000 0000 000{i}".replace(" ", ""),
        "accountType": "CASH_ACCOUNT",
        "enabledBalance": 1000.10,
        "withheldBalance": 0.2,
        "status": "ACTIVE",
        "currency": "EUR",
        "holders": [{"me": True, "participantType": "OWNER"}],
    }
    return {**base, **overrides}


def mock_no_remuneration(router: respx.MockRouter) -> respx.Route:
    """`accounts` asks for each cash account's remuneration (for its TAE): none here, a 404 for every account."""
    not_found = {"payload": {}, "status": {"code": "NOT_FOUND", "message": "no"}}
    return router.get(url__regex=rf"{CPERF}/v2/cash-accounts/\d+/remuneration").respond(404, json=not_found)


def flow(i: int, amount: float, **overrides: Any) -> dict[str, Any]:
    base = {
        "amount": amount,
        "concept": f"Concepto {i}",
        "currency": "EUR",
        "flowType": "IN" if amount >= 0 else "OUT",
        "operationClass": "ME",
        "operationDate": f"2026-10-{10 + i:02d}T00:00:00.000+0000",
        "operationType": "TRANSFERENCIA SEPA",
        "reference": f"ref{i}",
        "totalBalance": 500.0 + i,
        "valueDate": f"2026-10-{10 + i:02d}T00:00:00.000+0000",
    }
    return {**base, **overrides}


def investment(isin: str, kind: str = "INDEXED_FUND", **overrides: Any) -> dict[str, Any]:
    base = {
        "accountId": "201",
        "investmentProductType": kind,
        "isin": isin,
        "ticker": isin,
        "investmentName": f"Fondo {isin}",
        "initialInvestment": 1000.0,
        "marketValue": 1100.55,
        "profit": 100.55,
        "totalReturn": 10.06,
        "shares": 12.345678,
        "averageCost": 81.0,
        "liquidationValue": 89.15,
        "liquidationValueDate": "2026-11-02T00:00:00.000Z",
        "status": "ACTIVE",
    }
    return {**base, **overrides}


def securities_account(account_id: str = "201", **overrides: Any) -> dict[str, Any]:
    base = {
        "accountId": account_id,
        "codAccount": f"S{account_id}",
        "accountType": "SECURITIES_ACCOUNT",
        "status": "ACTIVE",
        "amount": 1100.55,
        "marketValue": 1100.55,
        "totalInvested": 1000.0,
        "profit": 100.55,
        "totalReturn": 10.06,
        "securitiesAccountInvestments": {
            "INDEXED_FUND": {"productNum": 1, "totalAmount": 1100.55, "investmentList": [investment("IE0000000001")]},
        },
    }
    return {**base, **overrides}


def order(ref: str, day: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "securitiesAccountId": "201",
        "codAccount": "S201",
        "orderDate": f"{day}T00:00:00.000+0000",
        "reference": ref,
        "isin": "IE0000000001",
        "ticker": "IE0000000001",
        "fundName": "Fondo indexado",
        "operationType": "INVESTMENT_FUNDS_SUBSCRIPTION",
        "cash": 150.0,
        "status": "EXECUTED",
        "orderType": "ORDINARY",
        "shares": 1.5,
        "currency": "EUR",
    }
    return {**base, **overrides}


def signature_pending(request_id: str = "sig-1", methods: list[list[str]] | None = None) -> dict[str, Any]:
    return {
        "payload": {
            "data": {
                "signatureRequestId": request_id,
                "status": "PENDING",
                "pendingSignatureMethods": methods or [["INAPP"]],
                "dataToSign": {},
            }
        },
        "status": {"code": "ACCEPTED"},
    }


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never reach MyInvestor: respx mocks above the socket layer, so this only bites leaks."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Network access is disabled in tests; mock it with respx.")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture(autouse=True)
def _no_myinvestor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's real MYINVESTOR_* (password, store…) must not leak into tests or their output."""
    for name in [n for n in os.environ if n.startswith("MYINVESTOR_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _neutral_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tables and stderr must not depend on the terminal or CI running pytest (FORCE_COLOR, TERM, COLUMNS…)."""
    for name in TERMINAL_ENV:
        monkeypatch.delenv(name, raising=False)
    # typer computes these once, when `typer.rich_utils` is imported; None is their value in a clean env.
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", None)
    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", None)
    for console in MODULE_CONSOLES:
        # Re-run the constructor in place: every module importing the console keeps the same object,
        # which now reads the clean environment (colour system, width, NO_COLOR are fixed at creation).
        console.__init__(stderr=console.stderr)


@pytest.fixture(autouse=True)
def _isolated_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))


class RealStoreAccessError(RuntimeError):
    """A test reached the real session store (OS keyring or default file) instead of a MemorySessionStore."""


class _ForbiddenKeyring:
    """Stands in for the OS keyring: any use records the access and raises (not a KeyringError, never caught)."""

    def __init__(self, accesses: list[str]) -> None:
        self._accesses = accesses

    def _refuse(self, what: str) -> RealStoreAccessError:
        self._accesses.append(what)
        return RealStoreAccessError(
            f"{what}: tests must not use the real keyring; patch `cli_common.get_store` (see the `store` fixtures)."
        )

    def get_password(self, service: str, username: str) -> str | None:
        raise self._refuse(f"keyring.get_password({service!r}, {username!r})")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise self._refuse(f"keyring.set_password({service!r}, {username!r})")

    def delete_password(self, service: str, username: str) -> None:
        raise self._refuse(f"keyring.delete_password({service!r}, {username!r})")

    def get_credential(self, service: str, username: str | None) -> None:
        raise self._refuse(f"keyring.get_credential({service!r}, {username!r})")


@pytest.fixture(autouse=True)
def real_store_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail any test that touches the user's real session store, even if the code under test swallowed the error.

    The keyring backend is replaced (so `auto` still picks it, and then refuses) and resolving the default
    session file raises. Tests that check the guard itself clear the returned list of accesses.
    """
    accesses: list[str] = []

    def default_store_path() -> Path:
        accesses.append("stores.default_store_path()")
        raise RealStoreAccessError("tests must not use the default session file; patch `cli_common.get_store`.")

    monkeypatch.setattr(keyring.core, "_keyring_backend", _ForbiddenKeyring(accesses))
    monkeypatch.setattr(stores, "default_store_path", default_store_path)
    yield accesses
    if accesses:
        pytest.fail(f"The test used the real session store: {', '.join(accesses)}")


@pytest.fixture(autouse=True)
def _no_controlling_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a TTY on stdin the CLI asks codes on /dev/tty: never on the terminal running pytest."""
    monkeypatch.setattr(cli_common, "open_tty", lambda: None)


@pytest.fixture(autouse=True)
def _reset_cli_verbosity() -> Iterator[None]:
    """`-q`/`-v` configure the process-wide `myinvestor` logger and stderr console: undo it per test."""
    logger = logging.getLogger("myinvestor")
    level, handlers = logger.level, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.handlers[:] = handlers
    notices.quiet = False


@pytest.fixture(autouse=True)
def _reset_json_envelope() -> Iterator[None]:
    """`--json-envelope` is process-wide too: a direct `render` after a CLI test must not be wrapped."""
    yield
    json_envelope.configure(enabled=False)


@pytest.fixture
def session() -> Session:
    return Session(
        device_id=DEVICE,
        customer_id="00000000T",
        access_token=make_jwt(given_name="Ana"),
        refresh_token=make_jwt(timedelta(minutes=30)),
    )


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with build_http_client(retries=3, backoff=0) as client:
        yield client
