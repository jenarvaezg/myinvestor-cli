"""`--approval-timeout` also bounds the app approval of `login --direct` and of the unattended re-login."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_auth, cli_common
from myinvestor.auth import LOGIN_PATH, Authenticator
from myinvestor.cli import app
from myinvestor.errors import SignatureError
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CPERF, DEVICE, make_jwt, ok, signature_pending
from tests.test_signature import FakeUI

runner = CliRunner()
PUBLIC = f"{API}/signature/api/v3/public/signature"
USER = "00000000T"
DEAD = make_jwt(timedelta(minutes=-5))


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No real waits: every poll of the signature status advances the clock 3 s."""
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("myinvestor.signature.time", SimpleNamespace(sleep=sleep, monotonic=lambda: now[0]))
    yield


@pytest.fixture
def approval_never_given(mock_api: respx.MockRouter) -> respx.Route:
    mock_api.post(f"{API}{LOGIN_PATH}").respond(202, json=signature_pending("r9", [["INAPP"]]))
    mock_api.post(f"{PUBLIC}/r9").respond(200, json=ok({"signatureId": "s9", "type": "INAPP"}))
    return mock_api.get(PUBLIC).respond(200, json=signature_pending("r9", [["INAPP"]]))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemorySessionStore:
    memory = MemorySessionStore(Session(device_id=DEVICE))
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    for module in (cli_common, cli_auth):
        monkeypatch.setattr(module, "stdin_is_tty", lambda: False)
    return memory


def test_authenticator_waits_its_approval_timeout(approval_never_given: respx.Route, http: httpx.Client) -> None:
    with pytest.raises(SignatureError, match="en 9 s"):
        Authenticator(http, approval_timeout=9).login_direct(USER, "pw", Session(device_id=DEVICE), FakeUI())
    assert approval_never_given.call_count == 3  # polls every 3 s


def test_login_direct_uses_the_approval_timeout_option(
    approval_never_given: respx.Route, store: MemorySessionStore
) -> None:
    result = runner.invoke(
        app,
        ["--approve", "--approval-timeout", "9", "login", "--direct", "-u", USER, "--password-stdin"],
        input="pw\n",
    )

    assert isinstance(result.exception, SignatureError) and "en 9 s" in str(result.exception)
    assert result.exception.exit_code == 13
    assert approval_never_given.call_count == 3


def test_relogin_uses_the_approval_timeout_option(
    mock_api: respx.MockRouter, approval_never_given: respx.Route, store: MemorySessionStore
) -> None:
    store.save(Session(device_id=DEVICE, customer_id=USER, access_token=DEAD, refresh_token=DEAD))
    store.save_password(USER, "pw")
    accounts = mock_api.get(f"{CPERF}/v2/cash-accounts/self")

    result = runner.invoke(app, ["--approve", "--approval-timeout", "6", "accounts", "-f", "json"])

    assert isinstance(result.exception, SignatureError) and "en 6 s" in str(result.exception)
    assert approval_never_given.call_count == 2
    assert not accounts.called
    assert store.load_password(USER) == "pw"  # a timeout is not a rejection
