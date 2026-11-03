from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError
from typer.testing import CliRunner

from myinvestor import cli, cli_accounts, cli_auth, cli_common
from myinvestor.auth import REFRESH_PATH
from myinvestor.cli import app
from myinvestor.errors import (
    ApiError,
    AuthError,
    CorruptKeyringError,
    CorruptStoreError,
    InputError,
    InvalidCredentialsError,
    LoginRejectedError,
    NetworkBlockedError,
    NotLoggedInError,
    PasswordChangeRequired,
    ReloginBlockedError,
    SessionExpiredError,
    SignatureError,
    SignatureRequired,
    StoredPasswordRejectedError,
)
from myinvestor.models import Order
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import (
    API,
    CASH_UUID,
    CPERF,
    DEVICE,
    cash_account,
    flow,
    investment,
    make_jwt,
    mock_no_remuneration,
    ok,
    order,
    securities_account,
    signature_pending,
    tokens,
)

runner = CliRunner()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


@pytest.fixture
def no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    monkeypatch.setattr(cli_auth, "stdin_is_tty", lambda: False)


def invoke(*args: str, stdin: str | None = None) -> Any:
    return runner.invoke(app, list(args), input=stdin, catch_exceptions=False)


def mock_portfolio(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    funds_and_stocks = securities_account(
        securitiesAccountInvestments={
            "INDEXED_FUND": {"investmentList": [investment("IE0000000001")]},
            "BROKER": {"investmentList": [investment("JE0000000002", "BROKER", marketValue=50.0)]},
        }
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(funds_and_stocks))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(
        200, json=ok([{"accountId": "301", "marketValue": 2000.0, "totalInvested": 1500.0, "profit": 500.0}])
    )
    plans = [
        {"dgsCode": "N0001", "marketValue": 1500.0, "liquidationValueDate": "2026-10-30T00:00:00.000Z"},
        {"dgsCode": "N0002", "marketValue": 500.0, "liquidationValueDate": "2026-10-29T00:00:00.000Z"},
    ]
    mock_api.get(f"{CPERF}/v2/pension-accounts/301").respond(200, json=ok(plans))


def test_position_json(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)

    result = invoke("position", "-f", "json")

    rows = json.loads(result.stdout, parse_float=Decimal)
    assert [(r["producto"], r["valor"]) for r in rows] == [
        ("efectivo", Decimal("1000.30")),
        ("efectivo", Decimal("1000.30")),
        ("valores", Decimal("1100.55")),
        ("pensiones", Decimal("2000.00")),
        ("total", Decimal("5101.15")),
    ]
    assert '"valor": 1000.30' in result.stdout
    assert all(r["invertido"] is None or isinstance(r["invertido"], Decimal) for r in rows)


def test_position_json_has_value_dates_and_query_time(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    before = datetime.now().astimezone().replace(microsecond=0)

    rows = json_rows("position")

    today = date.today().isoformat()
    # securities: latest NAV date of its funds/stocks; pensions: latest of its plans; cash: live balance
    assert [r["fecha_valor"] for r in rows] == [today, today, "2026-11-02", "2026-10-30", None]
    [consulted] = {r["consultado_en"] for r in rows}
    moment = datetime.fromisoformat(consulted)
    assert moment.tzinfo is not None and before <= moment <= datetime.now().astimezone()
    assert len(consulted) == len("2026-11-03T09:00:12+01:00")


def test_position_csv_ends_with_total(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    rows = list(csv.DictReader(io.StringIO(invoke("position", "-f", "csv").stdout)))
    assert (rows[-1]["producto"], rows[-1]["valor"], rows[-1]["fecha_valor"]) == ("total", "5101.15", "")
    assert "consultado_en" in rows[0]


def test_position_table_shows_value_date_but_not_query_time(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_portfolio(mock_api)
    monkeypatch.setenv("COLUMNS", "60")
    result = invoke("position")
    assert "2026-10-30" in result.stdout and "consultado_en" not in result.stdout
    assert f"Posición global a {date.today().isoformat()}" in result.stdout


def test_holdings_json_has_query_time(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    rows = json_rows("holdings")
    assert all(datetime.fromisoformat(r["consultado_en"]).tzinfo is not None for r in rows)
    assert "consultado_en" not in invoke("holdings").stdout


def json_rows(*args: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(invoke(*args, "-f", "json").stdout, parse_float=Decimal)
    return rows


def test_accounts_json_amounts_are_numbers(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_no_remuneration(mock_api)
    [row] = json_rows("accounts")
    amounts = (row["disponible"], row["retenido"], row["saldo"])
    assert amounts == (Decimal("1000.10"), Decimal("0.20"), Decimal("1000.30"))


def test_movements_json_amounts_are_numbers(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(
        200, json=ok({"flowList": [flow(1, -50.0, totalBalance=0.0)]})
    )
    [row] = json_rows("movements", "--from", "2026-10-01", "--to", "2026-10-31")
    assert (row["importe"], row["saldo"]) == (Decimal("-50.00"), Decimal("0.00"))
    assert str(row["importe"]) == "-50.00"


def test_holdings_json_keeps_unit_precision(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    [row] = json_rows("holdings", "--kind", "fondos")
    assert row["participaciones"] == Decimal("12.345678") and row["vl"] == Decimal("89.15")
    amounts = (row["valor"], row["invertido"], row["ganancia"])
    assert amounts == (Decimal("1100.55"), Decimal("1000.00"), Decimal("100.55"))
    assert row["rentab"] == Decimal("10.06")


def test_orders_json_amounts_are_numbers(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(200, json=ok([order("a", "2026-10-01")]))
    [row] = json_rows("orders")
    assert (row["importe"], row["participaciones"]) == (Decimal("150.00"), Decimal("1.5"))


@pytest.mark.parametrize("wide", [["--wide"], []])
def test_wide_option_and_env(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch, wide: list[str]
) -> None:
    long_concept = "Concepto larguísimo " * 6
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(
        200, json=ok({"flowList": [flow(1, -12.5, concept=long_concept.strip())]})
    )
    monkeypatch.setenv("COLUMNS", "60")
    if not wide:
        monkeypatch.setenv("MYINVESTOR_WIDE", "1")

    result = invoke(*wide, "movements", "--from", "2026-10-01", "--to", "2026-10-31")

    assert long_concept.strip() in result.stdout
    monkeypatch.delenv("MYINVESTOR_WIDE", raising=False)
    narrow = invoke("movements", "--from", "2026-10-01", "--to", "2026-10-31").stdout
    assert long_concept.strip() not in narrow and "2026-10-11" in narrow


def test_position_table_has_total(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    result = invoke("position")
    assert "TOTAL" in result.stdout and "5.101,15 €" in result.stdout


def test_holdings_filter_broker(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_portfolio(mock_api)
    rows = json.loads(invoke("holdings", "--kind", "broker", "-f", "json").stdout)
    assert [r["isin"] for r in rows] == ["JE0000000002"]


def test_movements_csv_for_one_account(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    route = mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(
        200, json=ok({"flowList": [flow(1, -12.5), flow(2, 100.0, concept="Nómina")]})
    )

    result = invoke("movements", "-a", "Cuenta 1", "--from", "2026-10-01", "--to", "2026-10-31", "-f", "csv")

    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [(r["fecha"], r["importe"]) for r in rows] == [("2026-10-12", "100.00"), ("2026-10-11", "-12.50")]
    assert route.calls.last.request.url.params["dateFrom"] == "2026-10-01"


def test_movements_search_and_iban_suffix(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(
        200, json=ok({"flowList": [flow(1, -12.5), flow(2, 100.0, concept="Nómina")]})
    )
    rows = json.loads(invoke("movements", "-a", "0001", "-s", "nómina", "-f", "json").stdout)
    assert [r["concepto"] for r in rows] == ["Nómina"]


def test_movements_unknown_account(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    result = runner.invoke(app, ["movements", "-a", "nope"])
    assert result.exit_code != 0
    assert "Disponibles" in str(result.exception)


def test_old_movements_without_terminal_explain_approval(
    mock_api: respx.MockRouter, store: MemorySessionStore, no_tty: None
) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1)]))
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(202, json=signature_pending())

    result = runner.invoke(app, ["movements", "--from", "2024-01-01"])

    assert result.exit_code != 0
    assert "segundo factor" in str(result.exception)


def test_orders_all_history(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    route = mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(
        200, json=ok([order("a", "2025-05-01"), order("b", "2026-10-01", status="IN_PROGRESS")])
    )

    rows = json.loads(invoke("orders", "--all", "--status", "executed", "-f", "json").stdout)

    assert [r["ref"] for r in rows] == ["a"]
    assert route.calls.last.request.url.params["dateFrom"] == "2010-01-01"


def test_login_refresh_token_stdin(mock_api: respx.MockRouter, store: MemorySessionStore, no_tty: None) -> None:
    store.session = None
    route = mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens())

    result = invoke("login", "--refresh-token-stdin", "--device-id", "dev-web", stdin="rt-123\n")

    assert "Sesión iniciada" in result.stderr
    assert json.loads(route.calls.last.request.content) == {"refreshToken": "rt-123"}
    assert route.calls.last.request.headers["x-device-id"] == "dev-web"
    assert store.session is not None and store.session.device_id == "dev-web"


def test_login_direct_reuses_device_and_reads_password_stdin(
    mock_api: respx.MockRouter, store: MemorySessionStore, no_tty: None
) -> None:
    route = mock_api.post(f"{API}/login/api/v2/auth/token").respond(200, json=tokens())

    invoke("login", "--direct", "-u", "00000000T", "--password-stdin", stdin="pw\n")

    assert json.loads(route.calls.last.request.content) == {"customerId": "00000000T", "password": "pw"}
    assert route.calls.last.request.headers["x-device-id"] == DEVICE


def test_login_direct_without_terminal_or_stdin_refuses(store: MemorySessionStore, no_tty: None) -> None:
    result = runner.invoke(app, ["login", "--direct", "-u", "00000000T"])
    assert result.exit_code != 0
    assert "stdin" in str(result.exception)


def test_logout_keeps_device(store: MemorySessionStore) -> None:
    invoke("logout")
    assert store.session == Session(device_id=DEVICE, customer_id="00000000T")
    invoke("logout", "--forget-device")
    assert store.session is None


def test_status_refreshes_and_greets(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana"}))
    result = invoke("status")
    assert "Sesión activa" in result.stderr and "Ana" in result.stderr


def test_status_when_expired(store: MemorySessionStore) -> None:
    old = make_jwt(timedelta(minutes=-5))
    store.session = Session(device_id=DEVICE, access_token=old, refresh_token=old)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 11 and "caducada" in result.stderr


def test_raw_rejects_bad_param(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["raw", "/x", "-p", "nokey"])
    assert result.exit_code != 0 and "clave=valor" in str(result.exception)


def test_raw_get(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    route = mock_api.get(f"{API}/feed/api/x").respond(200, json=ok([1]))
    result = invoke("raw", "feed/api/x", "-p", "a=1")
    assert json.loads(result.stdout)["payload"]["data"] == [1]
    assert route.calls.last.request.url.params["a"] == "1"


def test_history_for_pension_code(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([{"accountId": "301"}]))
    point = {"date": "2026-10-01", "marketValue": 1.0, "totalShares": 1.0}
    route = mock_api.get(f"{CPERF}/v2/pension-accounts/301/positions").respond(
        200, json=ok({"positions": {"N0001": [point], "N0002": [point]}})
    )

    rows = json.loads(invoke("history", "n0001", "--period", "year", "-f", "json").stdout)

    assert {r["producto"] for r in rows} == {"N0001"}
    assert route.calls.last.request.url.params["groupType"] == "ONE_YEAR"


def test_fund_summary_adds_price(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/funds/IE0000000001").respond(
        200, json=ok({"name": "Global", "isinCode": "IE0000000001", "ter": 0.1, "fundData": {"managingEntity": "Gest"}})
    )
    mock_api.get(f"{CPERF}/v2/funds/IE0000000001/prices/last").respond(
        200, json=ok({"date": "2026-11-02", "price": 10.5, "currency": "EUR", "dayProfitPercentage": 0.2})
    )

    data = json.loads(invoke("fund", "ie0000000001").stdout)

    assert data["managementEntity"] == "Gest"
    assert data["lastPrice"]["price"] == 10.5 and data["ter"] == 0.1


def test_not_logged_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = MemorySessionStore()
    monkeypatch.setattr(cli_common, "get_store", lambda: empty)
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code != 0 and "myinvestor login" in str(result.exception)


def test_contributions_hide_cancelled(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    def contribution(cid: str, state: str) -> dict[str, Any]:
        return {
            "contributionId": cid,
            "amount": 100,
            "status": state,
            "isin": "IE0000000001",
            "contributionTimeFrame": {"recurrence": "ONE_MONTH", "nextExecutionDate": str(date.today() + timedelta(1))},
        }

    mock_api.get(f"{CPERF}/v2/automatic-contributions/self").respond(
        200, json=ok([contribution("c1", "ACTIVE"), contribution("c2", "CANCELED")])
    )
    rows = json.loads(invoke("contributions", "-f", "json").stdout)
    assert [r["id"] for r in rows] == ["c1"]


# ---- exit codes ----------------------------------------------------------------------

EXIT_CASES = [
    (AuthError("x"), 1),
    (ApiError(404, "x", "/p"), 1),
    (LoginRejectedError(400, "NEW_CODE"), 1),
    (InputError("x"), 2),
    (InvalidCredentialsError(), 10),
    (StoredPasswordRejectedError("x"), 10),
    (PasswordChangeRequired(), 10),
    (NotLoggedInError(), 11),
    (SessionExpiredError(), 11),
    (ReloginBlockedError("x"), 11),
    (NetworkBlockedError(403), 12),
    (ApiError(503, "x", "/p"), 12),
    (httpx.ConnectError("boom"), 12),
    (SignatureRequired(["OTP_SMS"]), 13),
    (SignatureError("x"), 13),
    (CorruptStoreError(Path("session.json")), 14),
    (CorruptKeyringError("svc"), 14),
]


def run_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[Any, str]:
    monkeypatch.setattr(sys, "argv", ["myinvestor", *args])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code, capsys.readouterr().err


def failing_client(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def boom(*_args: Any) -> Any:
        raise exc

    monkeypatch.setattr(cli_accounts, "build_client", boom)


@pytest.mark.parametrize(("exc", "code"), EXIT_CASES, ids=[f"{type(e).__name__}-{c}" for e, c in EXIT_CASES])
def test_exit_code_per_error_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exc: Exception, code: int
) -> None:
    failing_client(monkeypatch, exc)
    status, stderr = run_main(monkeypatch, capsys, "accounts")
    assert status == code
    assert stderr.startswith("Error")  # human message without -f json


def unexpected_shape() -> ValidationError:
    """What a model raises when MyInvestor changes the shape of a response (here, with personal data in it)."""
    try:
        Order.model_validate({"reference": "R1", "orderDate": "2026-10-01", "shares": "SECRETO 12345678Z"})
    except ValidationError as exc:
        return exc
    raise AssertionError("the payload should not validate")


def test_unexpected_response_is_a_clear_error_without_the_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_client(monkeypatch, unexpected_shape())

    status, stderr = run_main(monkeypatch, capsys, "accounts")
    json_status, json_stderr = run_main(monkeypatch, capsys, "accounts", "-f", "json")

    assert status == json_status == 1
    message = " ".join(stderr.split())  # the console wraps long lines
    assert message.startswith("Error") and "formato inesperado en Order (shares)" in message
    error = json.loads(json_stderr)
    assert (error["error"], error["exit_code"]) == ("unexpected_response", 1)
    assert "SECRETO" not in stderr + json_stderr and "12345678Z" not in stderr + json_stderr


def test_expired_session_exits_11(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], store: MemorySessionStore
) -> None:
    old = make_jwt(timedelta(minutes=-5))
    store.session = Session(device_id=DEVICE, access_token=old, refresh_token=old)
    status, stderr = run_main(monkeypatch, capsys, "accounts")
    assert status == 11 and "caducado" in stderr


def test_usage_error_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    status, _ = run_main(monkeypatch, capsys, "accounts", "--no-such-option")
    assert status == 2


def test_success_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mock_api: respx.MockRouter, store: Any
) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([]))
    status, _ = run_main(monkeypatch, capsys, "accounts", "-f", "json")
    assert status == 0


def test_json_format_reports_error_as_json_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exc = SignatureRequired(["OTP_SMS"])
    failing_client(monkeypatch, exc)
    status, stderr = run_main(monkeypatch, capsys, "accounts", "-f", "json")
    assert status == 13
    assert json.loads(stderr) == {"error": "signature_required", "exit_code": 13, "message": str(exc)}


def test_json_format_reports_network_error_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_client(monkeypatch, httpx.ReadTimeout("slow"))
    status, stderr = run_main(monkeypatch, capsys, "accounts", "--format=json")
    body = json.loads(stderr)
    assert status == 12 and body["error"] == "network" and body["exit_code"] == 12
    assert "ReadTimeout" in body["message"]


def test_help_documents_exit_codes() -> None:
    text = " ".join(runner.invoke(app, ["--help"]).output.split())
    assert "Códigos de salida" in text
    for code in (10, 11, 12, 13, 14):
        assert f" {code} " in text


@pytest.mark.usefixtures("store")
def test_funds_carry_the_fund_currency_and_the_table_uses_it(
    mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    usd = {"isin": "US0000000009", "name": "Fondo en dólares", "currency": "USD", "liquidationValue": 29.88}
    eur = {"isin": "IE0000000001", "name": "Fondo en euros", "currency": "EUR", "liquidationValue": 10.5}
    mock_api.get(f"{CPERF}/v2/funds").respond(200, json=ok([usd, eur]))
    monkeypatch.setenv("COLUMNS", "200")

    rows = json.loads(invoke("funds", "-f", "json").stdout)
    table = invoke("funds").stdout

    assert [(r["isin"], r["moneda"]) for r in rows] == [("US0000000009", "USD"), ("IE0000000001", "EUR")]
    assert list(rows[0])[-1] == "moneda"  # appended: CSV columns keep their positions
    assert "29,88 USD" in table and "10,50 €" in table
