"""Request pacing, per-client GET cache and the optional retry after an Imperva block (#32)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from myinvestor.auth import REFRESH_PATH
from myinvestor.client import BLOCKED_BACKOFF, MyInvestorClient
from myinvestor.errors import NetworkBlockedError
from myinvestor.http import Pacer, build_http_client
from myinvestor.session import Session
from tests.conftest import API, CPERF, DEVICE, cash_account, make_jwt, ok, signature_pending, tokens
from tests.test_signature import FakeUI

CASH = f"{CPERF}/v2/cash-accounts/self"
BLOCK = httpx.Response(403, headers={"content-type": "text/html"}, text="<html>Incapsula</html>")


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# ---- --min-interval ------------------------------------------------------------


def test_pacer_waits_only_the_missing_time() -> None:
    clock = FakeClock()
    pacer = Pacer(1.0, clock=clock, sleep=clock.sleep)

    pacer.wait()  # first request: no wait
    clock.now += 0.25
    pacer.wait()
    clock.now += 5
    pacer.wait()  # already more than 1 s since the last one

    assert clock.sleeps == [0.75]


def test_pacer_disabled_by_default() -> None:
    clock = FakeClock()
    pacer = Pacer(0, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        pacer.wait()
    assert clock.sleeps == []


def test_pacer_rejects_negative_interval() -> None:
    with pytest.raises(ValueError):
        Pacer(-1)


def test_http_client_paces_every_request(mock_api: respx.MockRouter, session: Session) -> None:
    clock = FakeClock()
    mock_api.get(CASH).respond(200, json=ok([]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([]))

    with build_http_client(retries=0, pacer=Pacer(0.5, clock=clock, sleep=clock.sleep)) as http:
        client = MyInvestorClient(session, http=http)
        client.cash_accounts()
        client.pension_accounts()

    assert clock.sleeps == [0.5]


# ---- --retry-blocked -----------------------------------------------------------


def _client(session: Session, http: httpx.Client, clock: FakeClock, retries: int, **kwargs: object) -> MyInvestorClient:
    return MyInvestorClient(session, http=http, retry_blocked=retries, sleep=clock.sleep, **kwargs)  # type: ignore[arg-type]


def test_blocked_get_is_not_retried_by_default(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    route = mock_api.get(CASH).mock(return_value=BLOCK)

    with pytest.raises(NetworkBlockedError):
        MyInvestorClient(session, http=http).cash_accounts()
    assert route.call_count == 1


def test_blocked_get_waits_and_retries(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    clock = FakeClock()
    route = mock_api.get(CASH)
    route.side_effect = [BLOCK, BLOCK, httpx.Response(200, json=ok([cash_account()]))]

    accounts = _client(session, http, clock, 2).cash_accounts()

    assert len(accounts) == 1
    assert clock.sleeps == list(BLOCKED_BACKOFF[:2])
    assert BLOCKED_BACKOFF[0] >= 30


def test_blocked_get_gives_up_after_n_retries(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    clock = FakeClock()
    route = mock_api.get(CASH).mock(return_value=BLOCK)

    with pytest.raises(NetworkBlockedError):
        _client(session, http, clock, 1).cash_accounts()
    assert route.call_count == 2 and len(clock.sleeps) == 1


def test_backoff_keeps_the_last_wait_for_extra_retries(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    clock = FakeClock()
    mock_api.get(CASH).mock(return_value=BLOCK)

    with pytest.raises(NetworkBlockedError):
        _client(session, http, clock, len(BLOCKED_BACKOFF) + 1).cash_accounts()
    assert clock.sleeps == [*BLOCKED_BACKOFF, BLOCKED_BACKOFF[-1]]


def test_blocked_refresh_is_never_retried(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    clock = FakeClock()
    expired = Session(device_id=DEVICE, access_token=make_jwt(timedelta(minutes=-1)), refresh_token=make_jwt())
    refresh = mock_api.post(f"{API}{REFRESH_PATH}").mock(return_value=BLOCK)
    data = mock_api.get(CASH).respond(200, json=ok([]))

    with pytest.raises(NetworkBlockedError):
        _client(expired, http, clock, 3).cash_accounts()
    assert refresh.call_count == 1 and data.call_count == 0 and clock.sleeps == []


def test_blocked_signature_calls_are_never_retried(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    clock = FakeClock()
    flows = mock_api.get(url__regex=r".*/flows").respond(202, json=signature_pending("r1"))
    start = mock_api.post(f"{API}/signature/api/v3/signature/r1").mock(return_value=BLOCK)

    with pytest.raises(NetworkBlockedError):
        _client(session, http, clock, 3, signature_ui=FakeUI()).get("/account/api/v3/account/x/flows")
    assert flows.call_count == 1 and start.call_count == 1 and clock.sleeps == []


def test_blocked_raw_get_on_signature_path_is_not_retried(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    clock = FakeClock()
    route = mock_api.get(f"{API}/signature/api/v3/signature").mock(return_value=BLOCK)

    with pytest.raises(NetworkBlockedError):
        _client(session, http, clock, 3).get("/signature/api/v3/signature")
    assert route.call_count == 1


# ---- per-client cache ----------------------------------------------------------


def test_identical_gets_hit_the_api_once(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(CASH).respond(200, json=ok([cash_account()]))
    client = MyInvestorClient(session, http=http, cache=True)

    first, second = client.cash_accounts(), client.cash_accounts()

    assert first == second and route.call_count == 1


def test_cache_key_includes_params(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{CPERF}/v2/funds").respond(200, json=ok([]))
    client = MyInvestorClient(session, http=http, cache=True)

    client.funds("a")
    client.funds("b")
    client.funds("a")

    assert route.call_count == 2


def test_cached_bodies_are_copies(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{CPERF}/v2/funds/IE0000000001").respond(200, json=ok({"name": "Fondo"}))
    client = MyInvestorClient(session, http=http, cache=True)

    client.fund("IE0000000001")["name"] = "cambiado"

    assert client.fund("IE0000000001")["name"] == "Fondo"


def test_cache_is_per_client_and_off_by_default(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    route = mock_api.get(CASH).respond(200, json=ok([]))

    plain = MyInvestorClient(session, http=http)
    plain.cash_accounts()
    plain.cash_accounts()
    MyInvestorClient(session, http=http, cache=True).cash_accounts()
    MyInvestorClient(session, http=http, cache=True).cash_accounts()

    assert route.call_count == 4


def test_signed_responses_are_not_cached(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    flows = mock_api.get(url__regex=r".*/flows")
    flows.side_effect = [
        httpx.Response(202, json=signature_pending("r1", [["OTP_SMS"]])),
        httpx.Response(200, json=ok({"flowList": []})),
        httpx.Response(202, json=signature_pending("r2", [["OTP_SMS"]])),
        httpx.Response(200, json=ok({"flowList": []})),
    ]
    mock_api.post(url__regex=r".*/signature/api/v3/signature/r\d\?method=OTP_SMS$").respond(
        200, json=ok({"signatureId": "s"})
    )
    mock_api.post(url__regex=r".*/validate/s$").respond(
        200, json=ok({"signatureRequestId": "r", "status": "COMPLETE", "pendingSignatureMethods": []})
    )
    client = MyInvestorClient(session, http=http, cache=True, signature_ui=FakeUI(codes=["1", "2"]))

    client.get("/account/api/v3/account/x/flows")
    client.get("/account/api/v3/account/x/flows")

    assert flows.call_count == 4


def test_cache_survives_token_refresh(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    expired = Session(device_id=DEVICE, access_token=make_jwt(timedelta(minutes=-1)), refresh_token=make_jwt())
    mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens())
    route = mock_api.get(CASH).respond(200, json=ok([]))
    client = MyInvestorClient(expired, http=http, cache=True)

    client.cash_accounts()
    client.cash_accounts()

    assert route.call_count == 1
