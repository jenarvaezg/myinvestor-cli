"""`--quiet`, `--verbose` and the request log, which must never leak secrets (#31)."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_auth, cli_common
from myinvestor.auth import LOGIN_PATH, REFRESH_PATH, Authenticator
from myinvestor.cli import app, main
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError, NotLoggedInError
from myinvestor.http import log_path
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CPERF, DEVICE, cash_account, make_jwt, ok, signature_pending, tokens
from tests.test_signature import FakeUI

runner = CliRunner()
CASH = f"{CPERF}/v2/cash-accounts/self"

# Synthetic secrets: none of them may show up in the request log.
PASSWORD = "Contrasena-Sintetica-7731"
CUSTOMER = "99999999R"
OTP_CODE = "864209"
SIGNATURE_REQUEST = "sigreq-0f0f0f0f"
SIGNATURE_ID = "sigid-1e1e1e1e"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_auth, "get_store", lambda: memory)
    return memory


# ---- request log ---------------------------------------------------------------


def test_request_log_never_contains_secrets(
    mock_api: respx.MockRouter, http: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    login_access, login_refresh = make_jwt(timedelta(minutes=-1), jti="login-a"), make_jwt(jti="login-r")
    new_access, new_refresh = make_jwt(jti="refresh-a"), make_jwt(timedelta(minutes=30), jti="refresh-r")
    mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens(login_access, login_refresh))
    mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens(new_access, new_refresh))
    flows = mock_api.get(url__regex=r".*/flows")
    flows.side_effect = [
        httpx.Response(202, json=signature_pending(SIGNATURE_REQUEST, [["OTP_SMS"]])),
        httpx.Response(200, json=ok({"flowList": []})),
    ]
    mock_api.post(f"{API}/signature/api/v3/signature/{SIGNATURE_REQUEST}").respond(
        200, json=ok({"signatureId": SIGNATURE_ID, "destination": "***12"})
    )
    mock_api.post(f"{API}/signature/api/v3/signature/{SIGNATURE_REQUEST}/validate/{SIGNATURE_ID}").respond(
        200, json=ok({"signatureRequestId": SIGNATURE_REQUEST, "status": "COMPLETE", "pendingSignatureMethods": []})
    )
    caplog.set_level(logging.DEBUG, logger="myinvestor")

    session = Authenticator(http).login_direct(CUSTOMER, PASSWORD, Session(device_id=DEVICE), None)
    client = MyInvestorClient(session, http=http, signature_ui=FakeUI(codes=[OTP_CODE]))
    client.get("/account/api/v3/account/x/flows", {"dateFrom": "2020-01-01"})

    log = caplog.text
    assert f"POST {LOGIN_PATH} 200" in log
    assert f"POST {REFRESH_PATH} 200" in log and "[refresh]" in log
    assert "GET /account/api/v3/account/x/flows 202" in log
    assert "GET /account/api/v3/account/x/flows 200" in log and "[firmada]" in log
    assert " ms" in log
    for secret in (
        PASSWORD,
        CUSTOMER,
        OTP_CODE,
        SIGNATURE_REQUEST,
        SIGNATURE_ID,
        login_access,
        login_refresh,
        new_access,
        new_refresh,
        "Bearer",
        "authorization",
        "x-signature-id",
        "dateFrom",
        "2020-01-01",
    ):
        assert secret not in log, secret


def test_log_path_hides_signature_ids() -> None:
    assert log_path("/signature/api/v3/signature/abc/validate/def") == "/signature/api/v3/signature/*/validate/*"
    assert log_path("/signature/api/v3/public/signature/abc") == "/signature/api/v3/public/signature/*"
    assert log_path("/signature/api/v3/signature") == "/signature/api/v3/signature"
    assert log_path("/cperf-server/api/v2/funds/IE0000000001") == "/cperf-server/api/v2/funds/IE0000000001"


def test_relogin_is_logged(mock_api: respx.MockRouter, http: httpx.Client, caplog: pytest.LogCaptureFixture) -> None:
    dead = make_jwt(timedelta(minutes=-1))
    expired = Session(device_id=DEVICE, customer_id=CUSTOMER, access_token=dead, refresh_token=dead)
    mock_api.get(CASH).respond(200, json=ok([]))
    caplog.set_level(logging.DEBUG, logger="myinvestor")

    client = MyInvestorClient(expired, http=http, relogin=lambda s: s.with_tokens(make_jwt(), make_jwt()))
    client.cash_accounts()

    assert "re-login" in caplog.text


# ---- CLI -----------------------------------------------------------------------


def test_verbose_logs_each_request_to_stderr(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    old = store.session
    assert old is not None
    store.session = Session(
        device_id=DEVICE, access_token=make_jwt(timedelta(minutes=-1)), refresh_token=old.refresh_token
    )
    mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens())
    mock_api.get(CASH).respond(200, json=ok([cash_account()]))

    result = runner.invoke(app, ["-v", "accounts", "-f", "json"], catch_exceptions=False)

    assert result.exit_code == 0
    assert f"POST {REFRESH_PATH} 200" in result.stderr and "[refresh]" in result.stderr
    assert "GET /cperf-server/api/v2/cash-accounts/self 200" in result.stderr
    assert old.refresh_token not in result.stderr and "Bearer" not in result.stderr
    assert result.stdout.lstrip().startswith("[")  # the log never goes to stdout


def test_without_verbose_no_request_log(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(CASH).respond(200, json=ok([cash_account()]))
    result = runner.invoke(app, ["accounts", "-f", "json"], catch_exceptions=False)
    assert result.stderr == ""


def test_quiet_silences_notices(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(CASH).respond(200, json=ok([cash_account()]))
    mock_api.get(url__regex=r".*/flows").respond(200, json=ok({"flowList": []}))

    loud = runner.invoke(app, ["movements", "--days", "120", "-f", "table"], catch_exceptions=False)
    quiet = runner.invoke(app, ["-q", "movements", "--days", "120", "-f", "table"], catch_exceptions=False)

    assert "Rango de más de" in loud.stderr and "Sin resultados" in loud.stderr
    assert quiet.exit_code == 0 and quiet.stderr == ""


def test_quiet_from_environment(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["logout"], env={"MYINVESTOR_QUIET": "1"}, catch_exceptions=False)
    assert result.exit_code == 0 and result.stderr == ""


def test_quiet_and_verbose_are_exclusive(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["-q", "-v", "accounts"])
    assert isinstance(result.exception, InputError)


def test_quiet_still_prints_errors(
    monkeypatch: pytest.MonkeyPatch, store: MemorySessionStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.session = None
    monkeypatch.setattr("sys.argv", ["myinvestor", "-q", "accounts"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == NotLoggedInError.exit_code
    assert "myinvestor login" in capsys.readouterr().err


@pytest.mark.parametrize("fmt", ["json", "csv", "tsv"])
def test_machine_formats_without_rows_print_no_notice(
    mock_api: respx.MockRouter, store: MemorySessionStore, fmt: str
) -> None:
    mock_api.get(CASH).respond(200, json=ok([]))

    result = runner.invoke(app, ["accounts", "-f", fmt], catch_exceptions=False)

    assert result.stderr == ""
    assert result.stdout.strip() == ("[]" if fmt == "json" else "")


def test_stderr_has_no_markup_or_colors_without_a_terminal(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_api.get(CASH).respond(200, json=ok([cash_account()]))
    mock_api.get(url__regex=r".*/flows").respond(200, json=ok({"flowList": []}))

    result = runner.invoke(app, ["movements", "--days", "120"], catch_exceptions=False)

    assert "Rango de más de" in result.stderr
    assert "\x1b[" not in result.stderr and "[dim]" not in result.stderr
