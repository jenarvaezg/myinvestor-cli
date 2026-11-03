"""`login --direct --remember`: unattended re-login with the saved password."""

from __future__ import annotations

import json
import stat
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_auth, cli_common
from myinvestor.auth import LOGIN_PATH, REFRESH_PATH, Authenticator, stored_password_relogin
from myinvestor.cli import app
from myinvestor.client import MyInvestorClient
from myinvestor.errors import AuthError, InputError, SessionExpiredError
from myinvestor.session import Session
from myinvestor.stores import FileSessionStore, MemorySessionStore
from tests.conftest import API, CPERF, DEVICE, make_jwt, ok, tokens

runner = CliRunner()
DEAD = make_jwt(timedelta(minutes=-5))


def dead_session() -> Session:
    return Session(device_id=DEVICE, customer_id="00000000T", access_token=DEAD, refresh_token=DEAD)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemorySessionStore:
    memory = MemorySessionStore(dead_session())
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_auth, "get_store", lambda: memory)
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    monkeypatch.setattr(cli_auth, "stdin_is_tty", lambda: False)
    return memory


def test_expired_session_logs_in_again_with_saved_password(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    store.save_password("00000000T", "pw")
    login = mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens())
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([]))

    result = runner.invoke(app, ["accounts", "-f", "json"], catch_exceptions=False)

    assert result.exit_code == 0
    assert json.loads(login.calls.last.request.content) == {"customerId": "00000000T", "password": "pw"}
    assert login.calls.last.request.headers["x-device-id"] == DEVICE
    assert store.session is not None and store.session.access_token != DEAD


def test_rejected_saved_password_is_deleted(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    store = MemorySessionStore(dead_session())
    store.save_password("00000000T", "old")
    mock_api.post(f"{API}{LOGIN_PATH}").respond(
        400, json={"payload": {"data": {"errCode": "INVALID_CREDENTIALS"}}, "status": {"code": "BAD_REQUEST"}}
    )
    relogin = stored_password_relogin(Authenticator(http), store, None)
    client = MyInvestorClient(dead_session(), http=http, store=store, relogin=relogin)

    with pytest.raises(AuthError, match="He borrado la contraseña"):
        client.cash_accounts()
    assert store.load_password("00000000T") is None


def test_without_saved_password_expired_is_expired(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["accounts"])
    assert isinstance(result.exception, SessionExpiredError)


def test_refresh_preferred_over_relogin(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    live = Session(
        device_id=DEVICE, customer_id="00000000T", access_token=DEAD, refresh_token=make_jwt(timedelta(minutes=20))
    )
    store = MemorySessionStore(live)
    store.save_password("00000000T", "pw")
    mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens())
    login = mock_api.post(f"{API}{LOGIN_PATH}")
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([]))

    relogin = stored_password_relogin(Authenticator(http), store, None)
    MyInvestorClient(live, http=http, store=store, relogin=relogin).cash_accounts()

    assert not login.called


def test_login_remember_saves_password_only_on_success(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    route = mock_api.post(f"{API}{LOGIN_PATH}")
    route.side_effect = [
        httpx.Response(400, json={"status": {"code": "BAD_REQUEST"}}),
        httpx.Response(200, json=tokens()),
    ]
    runner.invoke(app, ["login", "--direct", "--remember", "-u", "00000000T", "--password-stdin"], input="bad\n")
    assert store.load_password("00000000T") is None

    runner.invoke(app, ["login", "--direct", "--remember", "-u", "00000000T", "--password-stdin"], input="good\n")
    assert store.load_password("00000000T") == "good"


def test_remember_needs_direct(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["login", "--remember"])
    assert isinstance(result.exception, InputError)


def test_logout_forgets_password(store: MemorySessionStore) -> None:
    store.save_password("00000000T", "pw")
    runner.invoke(app, ["logout"], catch_exceptions=False)
    assert store.load_password("00000000T") is None
    assert store.session is not None and store.session.device_id == DEVICE  # device kept


def test_status_mentions_auto_relogin(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    store.save_password("00000000T", "pw")
    mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens())
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana"}))
    result = runner.invoke(app, ["status"], catch_exceptions=False)
    assert "re-login automático" in " ".join(result.stderr.split())


def test_file_store_passwords_are_private(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    store.save(dead_session())
    store.save_password("00000000T", "pw")
    assert store.load_password("00000000T") == "pw"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.delete_password("00000000T")
    assert store.load_password("00000000T") is None
    assert store.load() == dead_session()
