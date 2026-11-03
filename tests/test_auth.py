from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx

from myinvestor.auth import LOGIN_PATH, REFRESH_PATH, Authenticator
from myinvestor.errors import (
    InvalidCredentialsError,
    LoginRejectedError,
    NetworkBlockedError,
    PasswordChangeRequired,
    SessionExpiredError,
)
from myinvestor.session import Session
from tests.conftest import API, DEVICE, make_jwt, ok, signature_pending, tokens
from tests.test_signature import FakeUI


def test_refresh_rotates_tokens(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    new_access, new_refresh = make_jwt(), make_jwt(timedelta(minutes=30))
    route = mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens(new_access, new_refresh))

    refreshed = Authenticator(http).refresh(session)

    assert (refreshed.access_token, refreshed.refresh_token) == (new_access, new_refresh)
    assert refreshed.device_id == DEVICE
    request = route.calls.last.request
    assert json.loads(request.content) == {"refreshToken": session.refresh_token}
    assert request.headers["x-device-id"] == DEVICE
    assert "authorization" not in request.headers


def test_refresh_rejected_means_expired(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.post(f"{API}{REFRESH_PATH}").respond(400, json={"status": {"code": "BAD_REQUEST"}})
    with pytest.raises(SessionExpiredError):
        Authenticator(http).refresh(session)


def test_refresh_token_past_expiry_is_not_sent(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.post(f"{API}{REFRESH_PATH}")
    stale = Session(device_id=DEVICE, refresh_token=make_jwt(timedelta(minutes=-1)))
    with pytest.raises(SessionExpiredError):
        Authenticator(http).refresh(stale)
    assert not route.called


def test_waf_block_is_not_reported_as_expired(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.post(f"{API}{REFRESH_PATH}").respond(403, html="<html>_Incapsula_Resource</html>")
    with pytest.raises(NetworkBlockedError):
        Authenticator(http).refresh(session)


def test_login_direct(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=tokens())

    session = Authenticator(http).login_direct("00000000T", "secret", Session(device_id=DEVICE), None)

    assert session.customer_id == "00000000T"
    assert session.access_token and session.refresh_token
    request = route.calls.last.request
    assert json.loads(request.content) == {"customerId": "00000000T", "password": "secret"}
    assert request.headers["x-device-id"] == DEVICE
    assert "x-recaptcha-token" not in request.headers


def test_login_new_device_asks_sms_and_retries_signed(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    login = mock_api.post(f"{API}{LOGIN_PATH}")
    login.side_effect = [
        httpx.Response(202, json=signature_pending("r9", [["OTP_SMS"]])),
        httpx.Response(200, json=tokens()),
    ]
    mock_api.post(f"{API}/signature/api/v3/public/signature/r9").respond(
        200, json=ok({"signatureId": "s9", "type": "OTP", "channel": "SMS"})
    )
    validate = mock_api.post(f"{API}/signature/api/v3/public/signature/r9/validate/s9").respond(
        200, json=ok({"signatureRequestId": "r9", "status": "COMPLETE", "pendingSignatureMethods": []})
    )

    session = Authenticator(http).login_direct("00000000T", "secret", Session(device_id=DEVICE), FakeUI(["123456"]))

    assert session.access_token
    assert json.loads(validate.calls.last.request.content) == {"code": "123456"}
    assert login.calls.last.request.headers["x-signature-id"] == "r9"


def rejection(err_code: str | None, status: int = 400) -> httpx.Response:
    data = {"payload": {"data": {"errCode": err_code}}} if err_code else {}
    return httpx.Response(status, json={**data, "status": {"code": "BAD_REQUEST"}})


def test_login_rejected_with_bad_password(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.post(f"{API}{LOGIN_PATH}").mock(return_value=rejection("UNAUTHORIZED", 401))
    with pytest.raises(InvalidCredentialsError, match="UNAUTHORIZED"):
        Authenticator(http).login_direct("00000000T", "bad", Session(device_id=DEVICE), None)


def test_login_rejected_with_password_change_code(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.post(f"{API}{LOGIN_PATH}").mock(return_value=rejection("UPDATE_PASSWORD_REQUIRED"))
    with pytest.raises(PasswordChangeRequired):
        Authenticator(http).login_direct("00000000T", "x", Session(device_id=DEVICE), None)


@pytest.mark.parametrize("err_code", ["TOO_MANY_REQUESTS", None])
def test_login_rejected_for_other_reasons_is_not_bad_credentials(
    mock_api: respx.MockRouter, http: httpx.Client, err_code: str | None
) -> None:
    mock_api.post(f"{API}{LOGIN_PATH}").mock(return_value=rejection(err_code, 403))
    with pytest.raises(LoginRejectedError, match="reCAPTCHA") as exc:
        Authenticator(http).login_direct("00000000T", "x", Session(device_id=DEVICE), None)
    assert not isinstance(exc.value, InvalidCredentialsError)
    assert (exc.value.status, exc.value.err_code) == (403, err_code)
    assert exc.value.summary == (f"errCode {err_code}, HTTP 403" if err_code else "sin errCode, HTTP 403")


def test_login_password_change_required(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    body = tokens()
    body["payload"]["data"]["isPasswordChangeRequired"] = True
    mock_api.post(f"{API}{LOGIN_PATH}").respond(200, json=body)
    with pytest.raises(PasswordChangeRequired):
        Authenticator(http).login_direct("00000000T", "x", Session(device_id=DEVICE), None)


def test_login_is_never_retried(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.post(f"{API}{LOGIN_PATH}").respond(503, json={"status": {"code": "ERROR"}})
    with pytest.raises(Exception, match="503"):
        Authenticator(http).login_direct("00000000T", "x", Session(device_id=DEVICE), None)
    assert route.call_count == 1
