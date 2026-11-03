from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.session import Session
from myinvestor.session_info import SessionInfo, mask_customer_id
from myinvestor.stores import FileSessionStore, MemorySessionStore, ReloginBlock
from tests.conftest import CPERF, DEVICE, make_jwt, ok

runner = CliRunner()
DEAD = make_jwt(timedelta(minutes=-5))
STATUS_KEYS = {
    "logged_in",
    "name",
    "customer_id_masked",
    "device_id",
    "store",
    "store_path",
    "access_expires_at",
    "refresh_expires_at",
    "password_remembered",
    "can_refresh",
    "can_relogin",
    "relogin_blocked_until",
    "last_relogin_error",
    "verified_online",
}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    return memory


def status_json(*args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["status", "-f", "json", *args], catch_exceptions=False)
    report: dict[str, Any] = json.loads(result.stdout)
    return result.exit_code, report


def dead(customer_id: str | None = "00000000T") -> Session:
    return Session(device_id=DEVICE, customer_id=customer_id, access_token=DEAD, refresh_token=DEAD)


def test_status_json_online(mock_api: respx.MockRouter, store: MemorySessionStore, session: Session) -> None:
    store.save_password("00000000T", "s3cret-pw")
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana López"}))

    result = runner.invoke(app, ["status", "-f", "json"], catch_exceptions=False)

    report = json.loads(result.stdout)
    assert result.exit_code == 0 and set(report) == STATUS_KEYS
    assert report["logged_in"] and report["verified_online"] and report["can_refresh"] and report["can_relogin"]
    assert (report["name"], report["customer_id_masked"]) == ("Ana López", "***000T")
    assert (report["store"], report["store_path"], report["password_remembered"]) == ("memory", None, True)
    assert datetime.fromisoformat(report["access_expires_at"]) == session.expires_at
    assert report["relogin_blocked_until"] is None
    for secret in ("s3cret-pw", "00000000T", session.access_token or "?", session.refresh_token or "?"):
        assert secret not in result.output


def test_status_offline_never_calls_the_api(store: MemorySessionStore) -> None:
    with respx.mock(assert_all_called=False) as router:  # any request would be unmocked and fail
        code, report = status_json("--offline")
    assert code == 0 and not router.calls
    assert report["logged_in"] and not report["verified_online"] and report["name"] == "Ana"


def test_status_offline_with_expired_access_token_is_renewable(store: MemorySessionStore) -> None:
    store.session = Session(device_id=DEVICE, access_token=DEAD, refresh_token=make_jwt(timedelta(minutes=20)))
    code, report = status_json("--offline")
    assert code == 0 and report["logged_in"] and report["can_refresh"]


def test_status_offline_table(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["status", "--offline"], catch_exceptions=False)
    assert result.exit_code == 0 and "sin comprobar" in result.stderr and not result.stdout


def test_status_json_without_session_exits_11(store: MemorySessionStore) -> None:
    store.session = None
    code, report = status_json()
    assert code == 11 and report["logged_in"] is False and report["device_id"] is None


def test_status_json_after_logout_exits_11(store: MemorySessionStore) -> None:
    store.session = Session(device_id=DEVICE)
    code, report = status_json("--offline")
    assert code == 11 and report["device_id"] == DEVICE and not report["logged_in"]


def test_status_json_expired_without_relogin_exits_11(store: MemorySessionStore) -> None:
    store.session = dead()
    code, report = status_json()
    assert code == 11 and not report["logged_in"] and not report["can_refresh"]


def test_status_offline_expired_with_saved_password_is_ok(store: MemorySessionStore) -> None:
    store.session = dead()
    store.save_password("00000000T", "pw")
    code, report = status_json("--offline")
    assert code == 0 and report["logged_in"] and report["can_relogin"] and not report["can_refresh"]


def test_status_expired_with_paused_relogin_exits_11(store: MemorySessionStore) -> None:
    store.session = dead()
    store.save_password("00000000T", "pw")
    until = datetime.now(tz=UTC) + timedelta(hours=3)
    store.save_relogin_block("00000000T", ReloginBlock(until=until, error="errCode X, HTTP 403"))

    code, report = status_json("--offline")

    assert code == 11 and not report["logged_in"] and not report["can_relogin"]
    assert report["password_remembered"] and report["last_relogin_error"] == "errCode X, HTTP 403"
    assert datetime.fromisoformat(report["relogin_blocked_until"]) == until
    human = runner.invoke(app, ["status"])
    assert human.exit_code == 11 and "caducada" in human.stderr


def test_status_ignores_a_finished_relogin_pause(store: MemorySessionStore) -> None:
    store.session = dead()
    store.save_password("00000000T", "pw")
    past = ReloginBlock(until=datetime.now(tz=UTC) - timedelta(minutes=1), error="x")
    store.save_relogin_block("00000000T", past)
    code, report = status_json("--offline")
    assert code == 0 and report["relogin_blocked_until"] is None and report["can_relogin"]


def test_status_csv_is_one_row(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["status", "--offline", "-f", "csv"], catch_exceptions=False)
    header, row = result.stdout.splitlines()
    assert header.startswith("logged_in,name,customer_id_masked") and row.startswith("True,Ana,***000T")


def test_session_info_reports_file_store(tmp_path: Path, session: Session) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    store.save(session)
    report = SessionInfo.read(store).to_dict()
    assert (report["store"], report["store_path"]) == ("file", str(tmp_path / "session.json"))


@pytest.mark.parametrize(
    ("customer_id", "masked"), [("12345678Z", "***678Z"), ("X1234567L", "***567L"), ("123", "***"), (None, None)]
)
def test_mask_customer_id(customer_id: str | None, masked: str | None) -> None:
    assert mask_customer_id(customer_id) == masked
