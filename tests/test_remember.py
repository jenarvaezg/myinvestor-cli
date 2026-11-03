"""`login --direct --remember`: unattended re-login with the saved password."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import keyring
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_auth, cli_common
from myinvestor.auth import LOGIN_PATH, REFRESH_PATH, RELOGIN_PAUSE, Authenticator, stored_password_relogin
from myinvestor.cli import app
from myinvestor.client import MyInvestorClient
from myinvestor.errors import (
    CorruptStoreError,
    InputError,
    ReloginBlockedError,
    SessionExpiredError,
    SignatureRequired,
    StoredPasswordRejectedError,
)
from myinvestor.session import Session
from myinvestor.stores import FileSessionStore, KeyringSessionStore, MemorySessionStore, ReloginBlock
from tests.conftest import API, CPERF, DEVICE, make_jwt, ok, signature_pending, tokens

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


def rejected(err_code: str | None, status: int = 400) -> dict[str, object]:
    data = {"payload": {"data": {"errCode": err_code}}} if err_code else {}
    return {**data, "status": {"code": "BAD_REQUEST"}}


def remembered_client(http: httpx.Client, store: MemorySessionStore) -> MyInvestorClient:
    relogin = stored_password_relogin(Authenticator(http), store, None)
    return MyInvestorClient(dead_session(), http=http, store=store, relogin=relogin)


@pytest.fixture
def remembered() -> MemorySessionStore:
    store = MemorySessionStore(dead_session())
    store.save_password("00000000T", "pw")
    return store


def test_wrong_saved_password_is_deleted(
    mock_api: respx.MockRouter, http: httpx.Client, remembered: MemorySessionStore
) -> None:
    mock_api.post(f"{API}{LOGIN_PATH}").respond(401, json=rejected("UNAUTHORIZED"))

    with pytest.raises(StoredPasswordRejectedError, match="He borrado la contraseña") as exc:
        remembered_client(http, remembered).cash_accounts()
    assert exc.value.exit_code == 10
    assert remembered.load_password("00000000T") is None
    assert remembered.load_relogin_block("00000000T") is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json=rejected("UPDATE_PASSWORD_REQUIRED")),
        httpx.Response(200, json={**tokens(), "payload": {"data": {"isPasswordChangeRequired": True}}}),
    ],
    ids=["errCode", "flag"],
)
def test_forced_password_change_deletes_saved_password(
    mock_api: respx.MockRouter, http: httpx.Client, remembered: MemorySessionStore, response: httpx.Response
) -> None:
    mock_api.post(f"{API}{LOGIN_PATH}").mock(return_value=response)

    with pytest.raises(StoredPasswordRejectedError, match="cambiar la contraseña"):
        remembered_client(http, remembered).cash_accounts()
    assert remembered.load_password("00000000T") is None


@pytest.mark.parametrize("err_code", ["SOMETHING_NEW", None])
def test_unexplained_rejection_keeps_password_and_pauses_relogin(
    mock_api: respx.MockRouter, http: httpx.Client, remembered: MemorySessionStore, err_code: str | None
) -> None:
    login = mock_api.post(f"{API}{LOGIN_PATH}").respond(403, json=rejected(err_code))
    before = datetime.now(tz=UTC)

    with pytest.raises(ReloginBlockedError, match="no he borrado la contraseña") as exc:
        remembered_client(http, remembered).cash_accounts()
    assert exc.value.exit_code == 11
    assert remembered.load_password("00000000T") == "pw"
    block = remembered.load_relogin_block("00000000T")
    assert block is not None
    assert before + RELOGIN_PAUSE <= block.until <= datetime.now(tz=UTC) + RELOGIN_PAUSE
    assert block.error == (f"errCode {err_code}, HTTP 403" if err_code else "sin errCode, HTTP 403")

    # The next cron run does not even try: one attempt per pause, so the account cannot get locked.
    with pytest.raises(ReloginBlockedError, match="en pausa"):
        remembered_client(http, remembered).cash_accounts()
    assert login.call_count == 1
    assert remembered.load_password("00000000T") == "pw"


def test_relogin_retries_after_the_pause_and_clears_it(
    mock_api: respx.MockRouter, http: httpx.Client, remembered: MemorySessionStore
) -> None:
    expired = ReloginBlock(until=datetime.now(tz=UTC) - timedelta(minutes=1), error="errCode X, HTTP 403")
    remembered.save_relogin_block("00000000T", expired)
    mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens())
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([]))

    remembered_client(http, remembered).cash_accounts()

    assert remembered.load_relogin_block("00000000T") is None


def test_manual_login_resumes_paused_relogin(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    paused = ReloginBlock(until=datetime.now(tz=UTC) + timedelta(hours=5), error="errCode X, HTTP 403")
    store.save_relogin_block("00000000T", paused)
    mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens())

    runner.invoke(app, ["login", "--direct", "--remember", "-u", "00000000T", "--password-stdin"], input="pw\n")

    assert store.load_relogin_block("00000000T") is None


def test_relogin_needing_a_code_keeps_password(
    mock_api: respx.MockRouter, http: httpx.Client, remembered: MemorySessionStore
) -> None:
    """#11: without a terminal the SMS cannot be typed; that is no reason to forget the password."""
    mock_api.post(f"{API}{LOGIN_PATH}").respond(202, json=signature_pending(methods=[["OTP_SMS"]]))

    with pytest.raises(SignatureRequired) as exc:
        remembered_client(http, remembered).cash_accounts()
    assert exc.value.exit_code == 13
    assert remembered.load_password("00000000T") == "pw"
    assert remembered.load_relogin_block("00000000T") is None


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


@pytest.mark.parametrize("forget_device", [False, True])
def test_logout_clears_the_relogin_pause(store: MemorySessionStore, forget_device: bool) -> None:
    store.save_relogin_block("00000000T", ReloginBlock(datetime.now(tz=UTC) + RELOGIN_PAUSE, "errCode X, HTTP 403"))
    runner.invoke(app, ["logout", *(["--forget-device"] if forget_device else [])], catch_exceptions=False)
    assert store.load_relogin_block("00000000T") is None


def test_logout_clears_the_relogin_pause_in_the_file_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_store = FileSessionStore(tmp_path / "session.json")
    file_store.save(dead_session())
    file_store.save_relogin_block("00000000T", ReloginBlock(datetime.now(tz=UTC) + RELOGIN_PAUSE, "errCode X"))
    monkeypatch.setattr(cli_auth, "get_store", lambda: file_store)

    runner.invoke(app, ["logout", "--forget-device"], catch_exceptions=False)

    assert json.loads(file_store.path.read_text()).get("relogin_blocks") == {}


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


def test_file_store_relogin_block_roundtrip(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    store.save(dead_session())
    store.save_password("00000000T", "pw")
    block = ReloginBlock(until=datetime(2026, 11, 4, 10, 0, tzinfo=UTC), error="errCode X, HTTP 403")

    store.save_relogin_block("00000000T", block)

    assert store.load_relogin_block("00000000T") == block
    assert json.loads(store.path.read_text())["relogin_blocks"] == {
        "00000000T": {"relogin_blocked_until": "2026-11-04T10:00:00+00:00", "last_relogin_error": "errCode X, HTTP 403"}
    }
    store.clear_relogin_block("00000000T")
    assert store.load_relogin_block("00000000T") is None
    assert store.load_password("00000000T") == "pw" and store.load() == dead_session()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_file_store_corrupt_relogin_block(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"relogin_blocks": {"00000000T": {"relogin_blocked_until": "mañana"}}}))
    with pytest.raises(CorruptStoreError):
        FileSessionStore(path).load_relogin_block("00000000T")


def test_keyring_store_relogin_block_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    entries: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, key: entries.get((service, key)))
    monkeypatch.setattr(keyring, "set_password", lambda service, key, value: entries.update({(service, key): value}))
    monkeypatch.setattr(keyring, "delete_password", lambda service, key: entries.pop((service, key)))
    store = KeyringSessionStore("test-service")
    block = ReloginBlock(until=datetime(2026, 11, 4, 10, 0, tzinfo=UTC), error="sin errCode, HTTP 400")

    store.save_relogin_block("00000000T", block)
    assert store.load_relogin_block("00000000T") == block
    assert ("test-service", "relogin-block:00000000T") in entries

    store.clear_relogin_block("00000000T")
    store.clear_relogin_block("00000000T")  # idempotent
    assert store.load_relogin_block("00000000T") is None
