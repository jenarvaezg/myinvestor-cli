"""#11: the SMS code of `login --direct` without a terminal (MYINVESTOR_OTP, --code-stdin/-fd, /dev/tty, two steps)."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import keyring
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_auth, cli_common
from myinvestor.auth import LOGIN_PATH, Authenticator
from myinvestor.cli import app
from myinvestor.code_input import CodeSourcesUI, preset_code
from myinvestor.errors import (
    InputError,
    PartialResultError,
    SignatureCodeRequired,
    SignatureError,
    SignatureRequired,
    WrongCodeError,
)
from myinvestor.session import Session
from myinvestor.signature import SignatureRequest
from myinvestor.stores import FileSessionStore, KeyringSessionStore, MemorySessionStore, PendingLogin
from tests.conftest import API, CPERF, DEVICE, cash_account, make_jwt, ok, signature_pending, tokens
from tests.test_signature import BASE, FakeApi, FakeUI, signer, status

runner = CliRunner()
PUBLIC = f"{API}/signature/api/v3/public/signature"
USER = "00000000T"
MASKED = "+34 6** *** *12"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemorySessionStore:
    memory = MemorySessionStore(Session(device_id=DEVICE))
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: False)
    monkeypatch.setattr(cli_auth, "stdin_is_tty", lambda: False)
    return memory


class SmsLogin:
    """A login from a new device that asks for an SMS: the respx routes of the whole dance."""

    def __init__(self, router: respx.MockRouter, *, validate: list[httpx.Response] | None = None) -> None:
        self.login = router.post(f"{API}{LOGIN_PATH}")
        self.login.side_effect = self._login
        self.start = router.post(f"{PUBLIC}/r9").respond(
            200, json=ok({"signatureId": "s9", "type": "OTP", "channel": "SMS", "destination": MASKED})
        )
        self.validate = router.post(f"{PUBLIC}/r9/validate/s9")
        self.validate.side_effect = validate or [httpx.Response(200, json=ok(complete()))]

    @staticmethod
    def _login(request: httpx.Request) -> httpx.Response:
        if "x-signature-id" in request.headers:
            return httpx.Response(200, json=tokens())
        return httpx.Response(202, json=signature_pending("r9", [["OTP_SMS"]]))

    def codes(self) -> list[str]:
        return [json.loads(call.request.content)["code"] for call in self.validate.calls]


def complete() -> dict[str, Any]:
    return {"signatureRequestId": "r9", "status": "COMPLETE", "pendingSignatureMethods": []}


def wrong_code() -> httpx.Response:
    return httpx.Response(
        200, json=ok({"signatureRequestId": "r9", "status": "PENDING", "pendingSignatureMethods": [["OTP_SMS"]]})
    )


def pending_login(**overrides: Any) -> PendingLogin:
    base: dict[str, Any] = {
        "customer_id": USER,
        "device_id": DEVICE,
        "request_id": "r9",
        "signature_id": "s9",
        "method": "OTP_SMS",
        "destination": MASKED,
        "started_at": datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
    }
    return PendingLogin(**{**base, **overrides})


def login(*args: str, stdin: str | None = None) -> Any:
    return runner.invoke(app, ["login", "--direct", *args], input=stdin)


# --- MYINVESTOR_OTP ---------------------------------------------------------------------------------


def test_preset_code_answers_the_first_ask_only_once() -> None:
    api = FakeApi(
        {
            ("POST", f"{BASE}/r1"): [{"signatureId": "s1", "destination": MASKED}],
            ("POST", f"{BASE}/r1/validate/s1"): [status("PENDING", [["OTP_SMS"]])],
        }
    )
    announced: list[str] = []
    ui = CodeSourcesUI([preset_code("111111", "MYINVESTOR_OTP")], None, announced.append)

    with pytest.raises(SignatureError, match="no tengo otro código"):
        signer(api).complete(SignatureRequest("r1", "PENDING", [["OTP_SMS"]]), ui)
    assert [c[3] for c in api.calls if c[1].endswith("/validate/s1")] == [{"code": "111111"}]  # never re-sent
    assert MASKED in announced[0] and "MYINVESTOR_OTP" in announced[0]


def test_preset_code_then_falls_back_to_the_terminal() -> None:
    api = FakeApi(
        {
            ("POST", f"{BASE}/r1"): [{"signatureId": "s1"}],
            ("POST", f"{BASE}/r1/validate/s1"): [status("PENDING", [["OTP_SMS"]]), status("COMPLETE")],
        }
    )
    terminal = FakeUI(["222222"])
    ui = CodeSourcesUI([preset_code("111111", "MYINVESTOR_OTP")], terminal, lambda _m: None)

    assert signer(api).complete(SignatureRequest("r1", "PENDING", [["OTP_SMS"]]), ui).status == "COMPLETE"
    assert [attempt for _, attempt in terminal.asked] == [2]


def test_codes_only_ui_never_starts_an_app_approval() -> None:
    """With only a code at hand, an INAPP request must not ping the phone of someone who cannot approve it."""
    api = FakeApi({})
    ui = CodeSourcesUI([preset_code("111111", "MYINVESTOR_OTP")], None, lambda _m: None)
    with pytest.raises(SignatureRequired, match="INAPP"):
        signer(api).complete(SignatureRequest("r1", "PENDING", [["INAPP"]]), ui)
    assert api.calls == []


def test_codes_only_ui_picks_the_code_alternative() -> None:
    api = FakeApi(
        {("POST", f"{BASE}/r1"): [{"signatureId": "s1"}], ("POST", f"{BASE}/r1/validate/s1"): [status("COMPLETE")]}
    )
    ui = CodeSourcesUI([preset_code("111111", "MYINVESTOR_OTP")], None, lambda _m: None)
    signer(api).complete(SignatureRequest("r1", "PENDING", [["INAPP", "OTP_SMS"]]), ui)
    assert api.calls[0][2] == {"method": "OTP_SMS"}


def test_login_with_myinvestor_otp(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYINVESTOR_OTP", "123456")
    sms = SmsLogin(mock_api)

    result = login("-u", USER, "--password-stdin", stdin="pw\n")

    assert result.exit_code == 0, result.output
    assert sms.codes() == ["123456"]
    assert sms.login.calls.last.request.headers["x-signature-id"] == "r9"
    assert MASKED in result.stderr and "MYINVESTOR_OTP" in result.stderr
    assert store.session is not None and store.session.access_token


def test_wrong_myinvestor_otp_is_not_retried(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYINVESTOR_OTP", "000000")
    sms = SmsLogin(mock_api, validate=[wrong_code()])

    result = login("--remember", "-u", USER, "--password-stdin", stdin="pw\n")

    assert isinstance(result.exception, SignatureError) and result.exception.exit_code == 13
    assert sms.codes() == ["000000"]
    assert store.load_password(USER) is None


def test_cron_relogin_uses_myinvestor_otp(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = make_jwt(timedelta(minutes=-5))
    store.save(Session(device_id=DEVICE, customer_id=USER, access_token=dead, refresh_token=dead))
    store.save_password(USER, "pw")
    monkeypatch.setenv("MYINVESTOR_OTP", "123456")
    sms = SmsLogin(mock_api)
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([]))

    result = runner.invoke(app, ["accounts", "-f", "json"])

    assert result.exit_code == 0, result.output
    assert sms.codes() == ["123456"]


def test_cron_relogin_without_otp_sends_no_sms(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    dead = make_jwt(timedelta(minutes=-5))
    store.save(Session(device_id=DEVICE, customer_id=USER, access_token=dead, refresh_token=dead))
    store.save_password(USER, "pw")
    sms = SmsLogin(mock_api)

    result = runner.invoke(app, ["accounts", "-f", "json"])

    assert isinstance(result.exception, SignatureRequired) and "--start" in str(result.exception)
    assert not sms.start.called
    assert store.load_password(USER) == "pw"


# --- --code-stdin, --code-fd, /dev/tty --------------------------------------------------------------


def test_code_from_the_next_stdin_line(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    sms = SmsLogin(mock_api)

    result = login("-u", USER, "--password-stdin", "--code-stdin", stdin="pw\n654321\n")

    assert result.exit_code == 0, result.output
    assert json.loads(sms.login.calls[0].request.content)["password"] == "pw"
    assert sms.codes() == ["654321"]
    assert "leo el código de stdin" in result.stderr and MASKED in result.stderr


def test_code_stdin_reads_another_line_after_a_wrong_code(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    sms = SmsLogin(mock_api, validate=[wrong_code(), httpx.Response(200, json=ok(complete()))])

    result = login("-u", USER, "--password-stdin", "--code-stdin", stdin="pw\n000000\n654321\n")

    assert result.exit_code == 0, result.output
    assert sms.codes() == ["000000", "654321"]


def test_code_stdin_at_end_of_input_fails_cleanly(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    SmsLogin(mock_api)
    result = login("-u", USER, "--password-stdin", "--code-stdin", stdin="pw\n")
    assert isinstance(result.exception, SignatureError) and "no tengo código" in str(result.exception)


class FakeTerminal:
    """/dev/tty: each `open()` is a new handle; typed lines are shared, writes kept in `written`."""

    def __init__(self, *typed: str) -> None:
        self.typed = list(typed)
        self.written = ""
        self.handles: list[_TtyHandle] = []

    def open(self) -> io.StringIO:
        handle = _TtyHandle(self)
        self.handles.append(handle)
        return handle


class _TtyHandle(io.StringIO):
    def __init__(self, terminal: FakeTerminal) -> None:
        super().__init__()
        self._terminal = terminal

    def write(self, text: str) -> int:
        self._terminal.written += text
        return len(text)

    def readline(self, size: int | None = -1) -> str:
        return self._terminal.typed.pop(0) if self._terminal.typed else ""


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> FakeTerminal:
    fake = FakeTerminal("135790\n")
    monkeypatch.setattr(cli_common, "open_tty", fake.open)
    return fake


@pytest.fixture
def code_pipe() -> Iterator[int]:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"777777\n")
    os.close(write_fd)
    yield read_fd
    os.close(read_fd)


def test_code_from_a_file_descriptor(mock_api: respx.MockRouter, store: MemorySessionStore, code_pipe: int) -> None:
    sms = SmsLogin(mock_api)

    result = login("-u", USER, "--password-stdin", "--code-fd", str(code_pipe), stdin="pw\n")

    assert result.exit_code == 0, result.output
    assert sms.codes() == ["777777"]


def test_code_fd_that_is_not_open(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    SmsLogin(mock_api)
    result = login("-u", USER, "--password-stdin", "--code-fd", "987", stdin="pw\n")
    assert isinstance(result.exception, InputError) and "--code-fd 987" in str(result.exception)


def test_code_asked_on_dev_tty_when_stdin_is_a_pipe(
    mock_api: respx.MockRouter, store: MemorySessionStore, terminal: FakeTerminal
) -> None:
    sms = SmsLogin(mock_api)

    result = login("-u", USER, "--password-stdin", stdin="pw\n")

    assert result.exit_code == 0, result.output
    assert sms.codes() == ["135790"]
    assert terminal.written == f"Código OTP_SMS enviado a {MASKED}: "
    assert terminal.handles and all(h.closed for h in terminal.handles)  # opened only for the code, then closed


def test_dev_tty_is_not_opened_without_a_second_factor(
    mock_api: respx.MockRouter, store: MemorySessionStore, terminal: FakeTerminal
) -> None:
    mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens())
    assert login("-u", USER, "--password-stdin", stdin="pw\n").exit_code == 0
    assert terminal.handles == []


def test_dev_tty_never_approves_in_the_app(
    mock_api: respx.MockRouter, store: MemorySessionStore, terminal: FakeTerminal
) -> None:
    """/dev/tty is for typing codes: an app approval still needs stdin to be a terminal or --approve."""
    mock_api.post(f"{API}{LOGIN_PATH}").respond(202, json=signature_pending("r9", [["INAPP"]]))
    start = mock_api.post(f"{PUBLIC}/r9")

    result = login("-u", USER, "--password-stdin", stdin="pw\n")

    assert isinstance(result.exception, SignatureRequired) and result.exception.exit_code == 13
    assert not start.called


def test_dev_tty_does_not_ask_for_old_movements(
    mock_api: respx.MockRouter, store: MemorySessionStore, terminal: FakeTerminal, session: Session
) -> None:
    """`movements` only asks for the older span when someone can approve it in the app."""
    store.save(session)
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account()]))
    flows = mock_api.get(url__regex=r".*/account/api/v3/account/.*/flows").respond(200, json=ok({"flowList": []}))

    result = runner.invoke(app, ["movements", "--from", "2024-01-01", "-f", "json"])

    assert isinstance(result.exception, PartialResultError)
    assert flows.call_count == 1  # only the free span: no signature request was created


def test_no_signature_beats_every_code_source(
    mock_api: respx.MockRouter, store: MemorySessionStore, terminal: FakeTerminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYINVESTOR_OTP", "123456")
    sms = SmsLogin(mock_api)

    result = runner.invoke(
        app, ["--no-signature", "login", "--direct", "-u", USER, "--password-stdin", "--code-stdin"], input="pw\n1\n"
    )

    assert isinstance(result.exception, SignatureRequired) and result.exception.exit_code == 13
    assert not sms.start.called and terminal.handles == []


def test_no_signature_start_sends_no_sms(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    sms = SmsLogin(mock_api)

    result = runner.invoke(
        app, ["--no-signature", "login", "--direct", "--start", "-u", USER, "--password-stdin"], input="pw\n"
    )

    assert isinstance(result.exception, SignatureRequired) and result.exception.exit_code == 13
    assert not sms.start.called and store.load_pending_login() is None


def test_code_options_need_direct_and_one_source(store: MemorySessionStore) -> None:
    for args in (
        ["login", "--code-stdin"],
        ["login", "--start"],
        ["login", "--direct", "--code-stdin", "--code-fd", "3"],
    ):
        assert isinstance(runner.invoke(app, args).exception, InputError), args
    assert isinstance(login("--start", "--code", "1").exception, InputError)
    assert isinstance(login("--code", "1", "--code-stdin").exception, InputError)


# --- two steps: --start, then --code ----------------------------------------------------------------


def test_start_sends_the_sms_and_saves_the_pending_login(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    sms = SmsLogin(mock_api)

    result = login("--start", "-u", USER, "--password-stdin", stdin="pw\n")

    assert isinstance(result.exception, SignatureCodeRequired) and result.exception.exit_code == 13
    assert MASKED in str(result.exception) and "--code" in str(result.exception)
    assert sms.start.calls.last.request.url.params["method"] == "OTP_SMS"
    assert not sms.validate.called
    pending = store.load_pending_login()
    assert pending is not None and (pending.request_id, pending.signature_id, pending.device_id) == ("r9", "s9", DEVICE)
    assert pending.password is None  # without --remember the password is not kept
    assert store.load_password(USER) is None


def test_code_completes_the_pending_login(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    store.save_pending_login(pending_login(device_id="dev-start"))
    store.save(Session(device_id="dev-other"))
    sms = SmsLogin(mock_api)

    result = login("--code", "123456", "--password-stdin", stdin="pw\n")

    assert result.exit_code == 0, result.output
    assert sms.codes() == ["123456"]
    signed = sms.login.calls.last.request
    assert signed.headers["x-signature-id"] == "r9" and signed.headers["x-device-id"] == "dev-start"
    assert json.loads(signed.content) == {"customerId": USER, "password": "pw"}
    assert sms.validate.calls.last.request.headers["x-device-id"] == "dev-start"
    assert store.load_pending_login() is None
    assert store.session is not None and store.session.customer_id == USER and store.session.device_id == "dev-start"
    assert store.load_password(USER) is None


def test_two_steps_with_remember_need_no_password_the_second_time(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    SmsLogin(mock_api)
    login("--start", "--remember", "-u", USER, "--password-stdin", stdin="pw\n")
    pending = store.load_pending_login()
    assert pending is not None and pending.password == "pw" and "pw" not in repr(pending)
    assert store.load_password(USER) is None  # saved only once MyInvestor accepts the login

    result = login("--code", "123456")

    assert result.exit_code == 0, result.output
    assert store.load_password(USER) == "pw"


def test_start_and_code_use_the_saved_password(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    """Cron failed with 13: the agent restarts the login without knowing the password."""
    store.save_password(USER, "pw")
    sms = SmsLogin(mock_api)

    assert isinstance(login("--start", "-u", USER).exception, SignatureCodeRequired)
    result = login("--code", "123456")

    assert result.exit_code == 0, result.output
    assert [json.loads(c.request.content)["password"] for c in sms.login.calls] == ["pw", "pw"]


def test_wrong_code_keeps_the_pending_login_until_attempts_run_out(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    store.save_pending_login(pending_login())
    SmsLogin(
        mock_api, validate=[wrong_code(), httpx.Response(400, json={"status": {"code": "BAD_REQUEST"}}), wrong_code()]
    )

    first = login("--code", "000001", "--password-stdin", stdin="pw\n")
    assert isinstance(first.exception, WrongCodeError) and "te quedan 2" in str(first.exception)
    assert first.exception.exit_code == 13
    assert store.load_pending_login() == pending_login(wrong_codes=1)

    login("--code", "000002", "--password-stdin", stdin="pw\n")
    last = login("--code", "000003", "--password-stdin", stdin="pw\n")
    assert isinstance(last.exception, WrongCodeError) and "--start" in str(last.exception)
    assert store.load_pending_login() is None


def test_code_without_pending_login(store: MemorySessionStore) -> None:
    result = login("--code", "123456")
    assert isinstance(result.exception, InputError) and "--start" in str(result.exception)


def test_code_for_another_user(store: MemorySessionStore) -> None:
    store.save_pending_login(pending_login())
    assert isinstance(login("--code", "1", "-u", "11111111H").exception, InputError)


def test_expired_signature_clears_the_pending_login(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    store.save_pending_login(pending_login())
    expired = {"signatureRequestId": "r9", "status": "EXPIRED", "pendingSignatureMethods": []}
    sms = SmsLogin(mock_api, validate=[httpx.Response(200, json=ok(expired))])

    result = login("--code", "123456", "--password-stdin", stdin="pw\n")

    assert isinstance(result.exception, SignatureError) and "EXPIRED" in str(result.exception)
    assert store.load_pending_login() is None
    assert sms.login.call_count == 0


def test_network_error_keeps_the_pending_login(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    store.save_pending_login(pending_login())
    SmsLogin(mock_api, validate=[httpx.Response(503, json={"status": {"code": "ERROR"}})])

    result = login("--code", "123456", "--password-stdin", stdin="pw\n")

    assert result.exception is not None and getattr(result.exception, "exit_code", None) == 12
    assert store.load_pending_login() == pending_login()


def test_start_without_second_factor_just_logs_in(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens())
    result = login("--start", "-u", USER, "--password-stdin", stdin="pw\n")
    assert result.exit_code == 0, result.output
    assert store.load_pending_login() is None and store.session is not None and store.session.access_token


def test_start_when_only_the_app_can_approve(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.post(f"{API}{LOGIN_PATH}").respond(202, json=signature_pending("r9", [["INAPP"]]))
    start = mock_api.post(f"{PUBLIC}/r9")

    result = login("--start", "-u", USER, "--password-stdin", stdin="pw\n")

    assert isinstance(result.exception, SignatureError) and "--approve" in str(result.exception)
    assert not start.called and store.load_pending_login() is None


def test_login_and_logout_clear_a_stale_pending_login(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    store.save_pending_login(pending_login(password="pw"))
    mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens())
    login("-u", USER, "--password-stdin", stdin="pw\n")
    assert store.load_pending_login() is None

    store.save_pending_login(pending_login(password="pw"))
    runner.invoke(app, ["logout"])
    assert store.load_pending_login() is None


def test_finish_login_reports_attempts_left(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    SmsLogin(mock_api, validate=[httpx.Response(422, json={"status": {"code": "BAD_REQUEST"}})])
    with pytest.raises(WrongCodeError) as exc:
        Authenticator(http).finish_login(pending_login(wrong_codes=1), "pw", "1", None)
    assert exc.value.attempts_left == 1


# --- stores -----------------------------------------------------------------------------------------


def test_file_store_pending_login_roundtrip(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "session.json")
    store.save(Session(device_id=DEVICE))
    pending = pending_login(password="pw", wrong_codes=2)
    store.save_pending_login(pending)

    assert FileSessionStore(tmp_path / "session.json").load_pending_login() == pending
    store.clear_pending_login()
    assert store.load_pending_login() is None and store.load() == Session(device_id=DEVICE)


def test_keyring_store_pending_login_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    entries: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, key: entries.get((service, key)))
    monkeypatch.setattr(keyring, "set_password", lambda service, key, value: entries.update({(service, key): value}))
    monkeypatch.setattr(keyring, "delete_password", lambda service, key: entries.pop((service, key)))
    store = KeyringSessionStore("test-service")

    store.save_pending_login(pending_login())
    assert store.load_pending_login() == pending_login()
    store.clear_pending_login()
    assert entries == {}
