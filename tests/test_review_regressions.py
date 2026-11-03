"""Regressions for the edge cases found in the first code review."""

from __future__ import annotations

import json
from datetime import timedelta
from functools import partial
from typing import Any

import httpx
import keyring
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import browser_login as bl
from myinvestor import cli_auth, cli_common
from myinvestor.auth import REFRESH_PATH
from myinvestor.cli import app
from myinvestor.client import MyInvestorClient
from myinvestor.errors import ApiError, AuthError, CorruptKeyringError, InputError, MyInvestorError
from myinvestor.session import Session
from myinvestor.signature import SignatureRequest, Signer
from myinvestor.stores import KeyringSessionStore, MemorySessionStore
from tests.conftest import API, CPERF, DEVICE, cash_account, make_jwt, ok, signature_pending, tokens
from tests.test_signature import BASE, FakeApi, FakeUI, status

runner = CliRunner()


# ---- client ------------------------------------------------------------------


def test_unexpected_202_is_an_error_not_empty_data(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(202, json=ok(None))
    with pytest.raises(ApiError, match="202"):
        MyInvestorClient(session, http=http).cash_accounts()


def test_client_refuses_writes(http: httpx.Client, session: Session) -> None:
    client = MyInvestorClient(session, http=http)
    with pytest.raises(MyInvestorError, match="solo lectura"):
        client._call("POST", "/cperf-server/api/v2/securities-accounts/1/funds/buy-orders")
    with pytest.raises(MyInvestorError, match="solo lectura"):
        client._signer_call("POST", "/cperf-server/api/v2/cash-accounts/1/transfers")
    with pytest.raises(MyInvestorError, match="solo lectura"):
        client._signer_call("DELETE", "/signature/api/v3/signature/x")


def test_path_segments_are_escaped(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(url__regex=r".*/funds/.*").respond(200, json=ok({}))
    MyInvestorClient(session, http=http).fund("../../cash-accounts/self")
    assert route.calls.last.request.url.raw_path.startswith(b"/cperf-server/api/v2/funds/..%2F..%2Fcash-accounts")


def test_refresh_race_adopts_tokens_saved_by_another_process(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    expired = Session(device_id=DEVICE, access_token=make_jwt(timedelta(minutes=-1)), refresh_token=make_jwt())
    winner = Session(device_id=DEVICE, access_token=make_jwt(), refresh_token=make_jwt(timedelta(minutes=29)))
    store = MemorySessionStore(winner)  # the other process already rotated and saved the pair
    mock_api.post(f"{API}{REFRESH_PATH}").respond(400, json={"status": {"code": "BAD_REQUEST"}})
    route = mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([]))

    MyInvestorClient(expired, http=http, store=store).cash_accounts()

    assert route.calls.last.request.headers["authorization"] == f"Bearer {winner.access_token}"


def test_refresh_save_failure_keeps_working(
    mock_api: respx.MockRouter, http: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenStore(MemorySessionStore):
        def save(self, session: Session) -> None:
            raise OSError("keychain locked")

    expired = Session(device_id=DEVICE, access_token=make_jwt(timedelta(minutes=-1)), refresh_token=make_jwt())
    mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens())
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([]))

    assert MyInvestorClient(expired, http=http, store=BrokenStore(expired)).cash_accounts() == []
    assert "keychain locked" in caplog.text


# ---- signature -----------------------------------------------------------------


def _signer(api: FakeApi) -> Signer:
    return Signer(api, sleep=lambda _s: None, clock=partial(next, iter(range(0, 10_000, 3))))


def test_pending_without_methods_settles() -> None:
    api = FakeApi({("GET", BASE): [status("PENDING"), status("COMPLETE")]})
    assert _signer(api).complete(SignatureRequest("r1", "PENDING", []), FakeUI()).status == "COMPLETE"


def test_wrong_code_as_http_400_allows_retry() -> None:
    calls: list[Any] = []

    def api(method: str, path: str, *, params: Any = None, json: Any = None) -> Any:
        calls.append(json)
        if path.endswith("/r1"):
            return {"signatureId": "s1"}
        if json == {"code": "bad"}:
            raise ApiError(400, "INVALID_OTP", path)
        return status("COMPLETE")

    signer = Signer(api, sleep=lambda _s: None)
    assert signer.complete(SignatureRequest("r1", "PENDING", [["OTP_SMS"]]), FakeUI(["bad", "good"])).status == (
        "COMPLETE"
    )
    assert calls[-1] == {"code": "good"}


def test_method_still_pending_after_completion_is_not_called_unsupported() -> None:
    api = FakeApi(
        {
            ("POST", f"{BASE}/r1"): [{"signatureId": "s1"}],
            ("POST", f"{BASE}/r1/validate/s1"): [status("PENDING", [["INAPP"]])],
            ("GET", BASE): [status("PENDING", [["OTP_SMS"]])],  # SMS asked again after the app approval
        }
    )
    with pytest.raises(MyInvestorError, match="sigue pidiendo OTP_SMS"):
        _signer(api).complete(SignatureRequest("r1", "PENDING", [["OTP_SMS"]]), FakeUI(["1"]))


# ---- browser capture -------------------------------------------------------------


class FakePage:
    """localStorage scripted per read; records drops."""

    def __init__(self, reads: list[dict[str, str | None] | Exception], *, closed_after: int | None = None) -> None:
        self.reads = reads
        self.drops = 0
        self.evaluations = 0
        self.closed_after = closed_after
        self.visited: list[str] = []

    def goto(self, url: str, **_: Any) -> None:
        self.visited.append(url)

    def wait_for_load_state(self, *_: Any) -> None:
        return None

    def is_closed(self) -> bool:
        return self.closed_after is not None and self.evaluations >= self.closed_after

    def evaluate(self, script: str, keys: list[str]) -> Any:
        if script == bl._DROP_TOKENS:
            self.drops += 1
            return None
        self.evaluations += 1
        item = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(item, Exception):
            raise item
        return item


def storage(access: str, refresh: str) -> dict[str, str | None]:
    return {bl.TOKEN_KEY: access, bl.REFRESH_KEY: refresh, bl.DEVICE_KEY: DEVICE, bl.FORM_KEY: None}


def capture(page: FakePage, timeout: float = 30) -> Session:
    return bl.capture_session(page, timeout=timeout, sleep=lambda _s: None, clock=partial(next, iter(range(10_000))))


def test_capture_ignores_tokens_left_in_profile() -> None:
    old = storage(make_jwt(), make_jwt(timedelta(minutes=20)))
    fresh = storage(make_jwt(), make_jwt(timedelta(minutes=30), jti="new"))
    page = FakePage([old, old, old, fresh])

    session = capture(page)

    assert session.refresh_token == fresh[bl.REFRESH_KEY]
    assert page.drops == 2  # before login and after capture
    assert page.visited == [bl.WEB_ORIGIN, bl.SIGNIN_URL]


def test_capture_rejects_dead_tokens_until_timeout() -> None:
    dead = storage(make_jwt(timedelta(minutes=-5)), make_jwt(timedelta(minutes=-5)))
    with pytest.raises(AuthError, match="5 s"):
        capture(FakePage([{}, dead]), timeout=5)


def test_capture_browser_closed() -> None:
    with pytest.raises(AuthError, match="cerrado"):
        capture(FakePage([{}], closed_after=1))


def test_capture_gives_up_on_a_dead_page() -> None:
    page = FakePage([{}, bl.PlaywrightError("Target crashed")])
    with pytest.raises(AuthError, match="No consigo leer"):
        capture(page, timeout=1_000)


# ---- stores ----------------------------------------------------------------------


def test_corrupt_keyring_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "get_password", lambda *_: "{not json")
    with pytest.raises(CorruptKeyringError):
        KeyringSessionStore().load()


# ---- CLI validation ----------------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_auth, "get_store", lambda: memory)
    return memory


@pytest.mark.parametrize(
    "args",
    [["holdings", "--kind", "etfs"], ["movements", "--days", "0"], ["history", "--period", "max"]],
)
def test_bad_option_values_are_rejected(store: MemorySessionStore, args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code != 0


def test_orders_inverted_range(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["orders", "--from", "2026-10-01", "--to", "2026-01-01"])
    assert isinstance(result.exception, InputError)


def test_raw_rejects_path_tricks(store: MemorySessionStore) -> None:
    for path in ["/cperf-server/../login", "/x?a=1"]:
        assert isinstance(runner.invoke(app, ["raw", path]).exception, InputError)


def test_history_isin_with_pension_account_is_an_error(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([{"accountId": "301"}]))
    result = runner.invoke(app, ["history", "IE0000000001", "-a", "301"])
    assert isinstance(result.exception, InputError)


def test_orders_pension_account_filter(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([{"accountId": "301"}]))
    result = runner.invoke(app, ["orders", "--pension", "-a", "999"])
    assert isinstance(result.exception, InputError) and "301" in str(result.exception)


def test_commands_only_send_gets(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    """Read-only invariant across the commands that touch every product family."""
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account()]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([]))
    mock_api.get(url__regex=r".*/account/api/v3/account/.*").respond(200, json=ok({"flowList": []}))
    mock_api.get(f"{CPERF}/v2/automatic-contributions/self").respond(200, json=ok([]))
    for args in (["position"], ["movements"], ["holds"], ["holdings"], ["orders", "--all"], ["contributions"]):
        assert runner.invoke(app, [*args, "-f", "json"]).exit_code == 0, args
    assert mock_api.calls.call_count > 0
    assert {call.request.method for call in mock_api.calls} == {"GET"}
    assert all(json.loads(c.response.content)["status"]["code"] == "OK" for c in mock_api.calls)


def test_approve_flag_waits_for_app_without_terminal(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    monkeypatch.setattr("myinvestor.signature.time.sleep", lambda _s: None)
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account()]))
    flows = mock_api.get(url__regex=r".*/account/api/v3/account/.*/flows")
    flows.side_effect = [  # the last 90 days go first and need no signature; the older span does
        httpx.Response(200, json=ok({"flowList": []})),
        httpx.Response(202, json=signature_pending("r1")),
        httpx.Response(200, json=ok({"flowList": []})),
    ]
    mock_api.post(f"{API}/signature/api/v3/signature/r1").respond(200, json=ok({"signatureId": "s1"}))
    mock_api.get(f"{API}/signature/api/v3/signature").respond(
        200, json=ok({"signatureRequestId": "r1", "status": "COMPLETE", "pendingSignatureMethods": []})
    )

    result = runner.invoke(app, ["--approve", "movements", "--from", "2024-01-01", "-f", "json"])

    assert result.exit_code == 0, result.output
    stderr = " ".join(result.stderr.split())  # rich wraps long lines
    assert "app del móvil" in stderr
    assert "«movimientos de Cuenta 1 desde 2024-01-01»" in stderr
    assert flows.calls.last.request.headers["x-signature-id"] == "r1"


def test_without_approve_flag_no_terminal_never_starts_the_push(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account()]))
    mock_api.get(url__regex=r".*/account/api/v3/account/.*/flows").respond(202, json=signature_pending("r1"))
    start = mock_api.post(f"{API}/signature/api/v3/signature/r1")

    result = runner.invoke(app, ["movements", "--from", "2024-01-01"])

    assert "--approve" in str(result.exception)
    assert not start.called
