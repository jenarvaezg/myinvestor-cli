from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common, cli_doctor
from myinvestor.cli import app
from myinvestor.client import MyInvestorClient
from myinvestor.config import APP_VERSION
from myinvestor.doctor import (
    PROBE_PATH,
    Check,
    Level,
    check_api,
    check_clock,
    check_maintenance,
    check_network,
    check_session,
    check_store,
    check_version,
    run_doctor,
)
from myinvestor.session import Session
from myinvestor.session_info import SessionInfo
from myinvestor.stores import FileSessionStore, MemorySessionStore, ReloginBlock
from myinvestor.web_info import SETTINGS_URL, bundle_version, entry_script_url, parse_settings
from tests.conftest import API, CPERF, DEVICE, make_jwt, ok

runner = CliRunner()
WEB = "https://newapp.myinvestor.es"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
DEAD = make_jwt(timedelta(minutes=-5))
INDEX_HTML = '<html><head><script type="module" crossorigin src="/assets/index-AbC123.js"></script></head></html>'
SETTINGS = {"mode_name": "transactional", "min_version": {"android": "1.0.0", "ios": "1.0.0", "web": "3.0.0"}}
WAF_PAGE = "<html><script src='/_Incapsula_Resource?x=1'></script></html>"


def bundle(version: str = APP_VERSION) -> str:
    return (
        f"const a=1,Vm=`https://api.myinvestor.es`,qte=`https://config.myinvestor.es`,Um=`{version}`,Xte=`PROD`;"
        'sV=async()=>({"x-myinvestor-app":`version=${Um},platform=${await oV}`});'
    )


def date_header(moment: datetime) -> dict[str, str]:
    return {"date": format_datetime(moment, usegmt=True)}


def mock_public(
    router: respx.MockRouter, *, version: str = APP_VERSION, settings: Any = None, server_time: datetime = NOW
) -> None:
    unauthorized = {"status": {"code": "UNAUTHORIZED"}}
    router.get(f"{API}{PROBE_PATH}").respond(401, json=unauthorized, headers=date_header(server_time))
    router.get(f"{WEB}/").respond(200, text=INDEX_HTML)
    router.get(f"{WEB}/assets/index-AbC123.js").respond(200, text=bundle(version))
    router.get(SETTINGS_URL).respond(200, json=SETTINGS if settings is None else settings)


def info(session: Session | None, *, password: bool = False, block: ReloginBlock | None = None) -> SessionInfo:
    store = MemorySessionStore(session)
    if password and session and session.customer_id:
        store.save_password(session.customer_id, "pw")
    if block and session and session.customer_id:
        store.save_relogin_block(session.customer_id, block)
    return SessionInfo.read(store)


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


# ---- store -------------------------------------------------------------------


def private_store(tmp_path: Path, session: Session) -> FileSessionStore:
    store = FileSessionStore(tmp_path / "session.json")
    store.save(session)  # creates the file (0600) and the lock (0600)
    return store


def test_store_file_private_and_valid(tmp_path: Path, session: Session) -> None:
    check, read = check_store(private_store(tmp_path, session))
    assert check.level is Level.ok and "0600" in check.message
    assert read is not None and read.session == session


@pytest.mark.parametrize("target", ["session.json", "session.lock"])
def test_store_open_permissions_fail(tmp_path: Path, session: Session, target: str) -> None:
    store = private_store(tmp_path, session)
    (tmp_path / target).chmod(0o644)
    check, _ = check_store(store)
    assert check.level is Level.fail and "0644" in check.message and target in check.message


def test_store_corrupt_json_fails(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{not json")
    path.chmod(0o600)
    check, read = check_store(FileSessionStore(path))
    assert check.level is Level.fail and "corrupto" in check.message and read is None


def test_store_missing_file_warns(tmp_path: Path) -> None:
    check, read = check_store(FileSessionStore(tmp_path / "session.json"))
    assert check.level is Level.warn and "no existe" in check.message
    assert read is not None and read.session is None


# ---- session -----------------------------------------------------------------


def test_session_active(session: Session) -> None:
    check = check_session(info(session, password=True), NOW)
    assert check.level is Level.ok and "contraseña guardada: sí" in check.message
    assert "pw" not in json.dumps(check.to_dict()) and session.refresh_token not in json.dumps(check.to_dict())
    assert check.data["customer_id_masked"] == "***000T"


def test_session_missing_fails() -> None:
    assert check_session(info(None), NOW).level is Level.fail
    assert check_session(info(Session(device_id=DEVICE)), NOW).level is Level.fail
    assert check_session(None, NOW).level is Level.fail


def test_session_expired_without_password_fails() -> None:
    dead = Session(device_id=DEVICE, customer_id="00000000T", access_token=DEAD, refresh_token=DEAD)
    check = check_session(info(dead), datetime.now(tz=UTC))
    assert check.level is Level.fail and "caducada" in check.message


def test_session_expired_with_password_is_ok() -> None:
    dead = Session(device_id=DEVICE, customer_id="00000000T", access_token=DEAD, refresh_token=DEAD)
    check = check_session(info(dead, password=True), datetime.now(tz=UTC))
    assert check.level is Level.ok and "contraseña guardada" in check.message


def test_session_paused_relogin() -> None:
    dead = Session(device_id=DEVICE, customer_id="00000000T", access_token=DEAD, refresh_token=DEAD)
    block = ReloginBlock(until=datetime.now(tz=UTC) + timedelta(hours=2), error="errCode X, HTTP 403")
    check = check_session(info(dead, password=True, block=block), datetime.now(tz=UTC))
    assert check.level is Level.fail and "pausa" in check.message and "errCode X" in check.message


def test_session_alive_but_relogin_paused_warns(session: Session) -> None:
    block = ReloginBlock(until=datetime.now(tz=UTC) + timedelta(hours=2), error="errCode X, HTTP 403")
    check = check_session(info(session, password=True, block=block), datetime.now(tz=UTC))
    assert check.level is Level.warn and "pausa" in check.message


# ---- network and clock --------------------------------------------------------


def ticks(*values: float) -> Callable[[], float]:
    it = iter(values)
    return lambda: next(it)


def test_network_ok_on_any_non_waf_answer(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.get(f"{API}{PROBE_PATH}").respond(401, json={"status": {"code": "UNAUTHORIZED"}})
    check, response = check_network(http, ticks(1.0, 1.25))
    assert check.level is Level.ok and check.data == {"http_status": 401, "latency_ms": 250}
    assert response is not None and "authorization" not in route.calls.last.request.headers


def test_network_detects_imperva(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.get(f"{API}{PROBE_PATH}").respond(403, text=WAF_PAGE, headers={"content-type": "text/html"})
    check, _ = check_network(http)
    assert check.level is Level.fail and "Imperva" in check.message


def test_network_unreachable(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.get(f"{API}{PROBE_PATH}").mock(side_effect=httpx.ConnectError("down"))
    check, response = check_network(http)
    assert check.level is Level.fail and "ConnectError" in check.message and response is None


def test_network_server_error_warns(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.get(f"{API}{PROBE_PATH}").respond(502, json={"status": {"code": "ERROR"}})
    check, _ = check_network(http)
    assert check.level is Level.warn and "502" in check.message


@pytest.mark.parametrize(("offset", "level"), [(2, Level.ok), (-90, Level.warn), (600, Level.fail)])
def test_clock_skew(offset: int, level: Level) -> None:
    response = httpx.Response(200, headers=date_header(NOW))
    check = check_clock(response, NOW + timedelta(seconds=offset))
    assert check.level is level and check.data == {"skew_seconds": offset}


def test_clock_without_date_header_is_not_checkable() -> None:
    check = check_clock(httpx.Response(200), NOW)
    assert check.level is Level.warn and "Date" in check.message
    assert check_clock(None, NOW).level is Level.warn


# ---- API -------------------------------------------------------------------------


def client_for(session: Session, http: httpx.Client) -> Callable[[], MyInvestorClient]:
    return lambda: MyInvestorClient(session, http=http, store=MemorySessionStore(session))


def test_api_ok(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana"}))
    assert check_api(client_for(session, http)).level is Level.ok


def test_api_rejected_session_fails(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{CPERF}/v3/customers/self").respond(403, text=WAF_PAGE, headers={"content-type": "text/html"})
    check = check_api(client_for(session, http))
    assert check.level is Level.fail and "Imperva" in check.message and check.data == {"exit_code": 12}


# ---- version and maintenance ------------------------------------------------------


def test_bundle_version_follows_the_header_constant() -> None:
    assert bundle_version(bundle("3.140.2")) == "3.140.2"
    assert bundle_version("const Um=`3.1.0`;") is None
    assert entry_script_url(INDEX_HTML) == f"{WEB}/assets/index-AbC123.js"
    assert entry_script_url('<script src="/legacy.js"></script>') is None


def test_version_same_as_web(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_public(mock_api)
    check = check_version(http, parse_settings(SETTINGS))
    assert check.level is Level.ok and check.data["web_version"] == APP_VERSION


def test_version_behind_the_web_warns(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_public(mock_api, version="9.9.9")
    check = check_version(http, None)
    assert check.level is Level.warn and "9.9.9" in check.message


def test_version_unreadable_is_not_checkable(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.get(f"{WEB}/").respond(200, text="<html>sin scripts</html>")
    check = check_version(http, None)
    assert check.level is Level.warn and "no comprobable" in check.message


def test_version_below_web_minimum_warns(http: httpx.Client) -> None:
    settings = parse_settings({**SETTINGS, "min_version": {"web": "99.0.0"}})
    check = check_version(http, settings)
    assert check.level is Level.warn and "mínima" in check.message


@pytest.mark.parametrize(
    ("raw", "level"),
    [
        ({"mode_name": "transactional"}, Level.ok),
        ({"mode_name": "read_only"}, Level.warn),
        ({"mode_name": "maintenance", "active_until": "2026-11-03T12:00:00Z"}, Level.fail),
    ],
)
def test_maintenance_modes(raw: dict[str, Any], level: Level) -> None:
    check = check_maintenance(parse_settings(raw), None, None)
    assert check.level is level and check.data["mode"] == raw["mode_name"]


def test_maintenance_exception_for_listed_customer() -> None:
    raw = {"mode_name": "maintenance", "exception": {"mode_name": "transactional", "enabled_clients": ["00000000t"]}}
    assert check_maintenance(parse_settings(raw), None, "00000000T").level is Level.ok
    assert check_maintenance(parse_settings(raw), None, "11111111H").level is Level.fail


def test_maintenance_unknown_settings_is_not_checkable() -> None:
    assert check_maintenance(None, "settings.json responde HTTP 404", None).level is Level.warn


# ---- runner and CLI -----------------------------------------------------------------


def test_run_doctor_all_ok(mock_api: respx.MockRouter, http: httpx.Client, tmp_path: Path, session: Session) -> None:
    mock_public(mock_api)
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana"}))
    store = private_store(tmp_path, session)

    checks = run_doctor(store, http=http, client_factory=client_for(session, http), now=lambda: NOW)

    assert [c.name for c in checks] == ["store", "sesion", "red", "api", "reloj", "version", "mantenimiento"]
    assert {c.name: c.level for c in checks if c.level is not Level.ok} == {}


def test_run_doctor_offline_touches_nothing(session: Session) -> None:
    calls: list[str] = []

    def factory() -> MyInvestorClient:
        calls.append("api")
        raise AssertionError("offline must not build an API client")

    checks = by_name(run_doctor(MemorySessionStore(session), http=None, client_factory=factory))
    assert checks["sesion"].level is Level.ok and checks["red"].level is Level.warn and not calls


def test_run_doctor_skips_api_without_session(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_public(mock_api)
    checks = by_name(run_doctor(MemorySessionStore(), http=http, client_factory=lambda: pytest.fail("no client")))
    assert checks["sesion"].level is Level.fail and checks["api"].level is Level.warn


@pytest.fixture
def cli_store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_doctor, "get_store", lambda: memory)
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    return memory


def test_doctor_cli_json_and_exit_code(mock_api: respx.MockRouter, cli_store: MemorySessionStore) -> None:
    mock_public(mock_api, settings={"mode_name": "maintenance"}, server_time=datetime.now(tz=UTC))
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana"}))

    result = runner.invoke(app, ["doctor", "-f", "json"], catch_exceptions=False)

    report = json.loads(result.stdout)
    statuses = {c["check"]: c["status"] for c in report["checks"]}
    assert result.exit_code == 1 and report["ok"] is False and statuses["mantenimiento"] == "FAIL"
    assert statuses["api"] == statuses["red"] == statuses["reloj"] == "OK"
    assert cli_store.session is not None and cli_store.session.access_token not in result.stdout


def test_doctor_cli_lines(mock_api: respx.MockRouter, cli_store: MemorySessionStore) -> None:
    mock_public(mock_api, server_time=datetime.now(tz=UTC))
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana"}))
    result = runner.invoke(app, ["doctor"], catch_exceptions=False)
    lines = result.stdout.splitlines()
    assert len(lines) == 7 and all(line.split()[0] in ("OK", "WARN") for line in lines)


def test_doctor_cli_offline(cli_store: MemorySessionStore) -> None:
    with respx.mock(assert_all_called=False) as router:
        result = runner.invoke(app, ["doctor", "--offline"], catch_exceptions=False)
    assert result.exit_code == 0 and not router.calls and "--offline" in result.stdout


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"], catch_exceptions=False)
    assert result.exit_code == 0 and result.stdout.startswith("myinvestor ")
