from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from functools import partial

import httpx
import pytest
import respx

from myinvestor.auth import REFRESH_PATH
from myinvestor.client import MyInvestorClient, Period
from myinvestor.errors import ApiError, NotLoggedInError, SessionExpiredError, SignatureRequired
from myinvestor.session import Session
from myinvestor.signature import Signer
from myinvestor.stores import MemorySessionStore
from tests.conftest import (
    API,
    CASH_UUID,
    CPERF,
    DEVICE,
    cash_account,
    flow,
    make_jwt,
    ok,
    order,
    securities_account,
    signature_pending,
    tokens,
)
from tests.test_signature import FakeUI

FLOWS = f"{API}/account/api/v3/account/{CASH_UUID}/flows"


def client_for(session: Session, http: httpx.Client, **kwargs: object) -> MyInvestorClient:
    return MyInvestorClient(session, http=http, **kwargs)  # type: ignore[arg-type]


def test_requires_a_token(http: httpx.Client) -> None:
    with pytest.raises(NotLoggedInError):
        MyInvestorClient(Session(device_id=DEVICE), http=http)


def test_sends_bearer_device_and_app_headers(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account()]))

    [account] = client_for(session, http).cash_accounts()

    headers = route.calls.last.request.headers
    assert headers["authorization"] == f"Bearer {session.access_token}"
    assert headers["x-device-id"] == DEVICE
    assert headers["x-myinvestor-app"].startswith("version=")
    assert account.balance == Decimal("1000.30")  # parsed as Decimal: no float rounding


def test_refreshes_expired_access_token_and_saves_it(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    expired = Session(
        device_id=DEVICE, access_token=make_jwt(timedelta(minutes=-1)), refresh_token=session.refresh_token
    )
    new_access = make_jwt()
    mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens(new_access))
    route = mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([]))
    store = MemorySessionStore(expired)

    client_for(expired, http, store=store).cash_accounts()

    assert route.calls.last.request.headers["authorization"] == f"Bearer {new_access}"
    assert store.session is not None and store.session.access_token == new_access


def test_401_refreshes_once_and_retries(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens())
    route = mock_api.get(f"{CPERF}/v2/cash-accounts/self")
    route.side_effect = [httpx.Response(401), httpx.Response(200, json=ok([]))]

    assert client_for(session, http).cash_accounts() == []
    assert route.call_count == 2


def test_dead_refresh_token_means_login_again(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    dead = Session(
        device_id=DEVICE, access_token=make_jwt(timedelta(minutes=-1)), refresh_token=make_jwt(timedelta(minutes=-1))
    )
    with pytest.raises(SessionExpiredError):
        client_for(dead, http).cash_accounts()


def test_statement(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(FLOWS).respond(
        200,
        json=ok(
            {"flowList": [flow(1, -12.34), flow(2, 50.0)], "iban": "ES00", "startBalance": 10.0, "endBalance": 47.66}
        ),
    )

    statement = client_for(session, http).statement(CASH_UUID, date(2026, 10, 1), date(2026, 10, 31))

    assert route.calls.last.request.url.params["dateFrom"] == "2026-10-01"
    assert route.calls.last.request.url.params["dateTo"] == "2026-10-31"
    assert [f.amount for f in statement.flows] == [Decimal("-12.34"), Decimal("50.0")]
    assert statement.flows[0].operation_date == date(2026, 10, 11)
    assert statement.start_balance + sum(f.amount for f in statement.flows) == statement.end_balance


def test_old_movements_wait_for_app_approval_then_retry_signed(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    flows = mock_api.get(FLOWS)
    flows.side_effect = [
        httpx.Response(202, json=signature_pending("r1")),
        httpx.Response(200, json=ok({"flowList": [flow(1, 5.0)]})),
    ]
    mock_api.post(f"{API}/signature/api/v3/signature/r1").respond(200, json=ok({"signatureId": "s1", "type": "INAPP"}))
    mock_api.get(f"{API}/signature/api/v3/signature").respond(
        200, json=ok({"signatureRequestId": "r1", "status": "COMPLETE", "pendingSignatureMethods": []})
    )
    ui = FakeUI()
    client = client_for(session, http, signature_ui=ui, signer_factory=partial(Signer, sleep=lambda _s: None))

    statement = client.statement(CASH_UUID, date(2024, 1, 1), date(2026, 10, 31))

    assert len(statement.flows) == 1
    assert len(ui.approvals) == 1
    assert flows.calls.last.request.headers["x-signature-id"] == "r1"


def test_old_movements_without_terminal_fail_clearly(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(FLOWS).respond(202, json=signature_pending("r1"))
    with pytest.raises(SignatureRequired, match="INAPP"):
        client_for(session, http).statement(CASH_UUID, date(2024, 1, 1), date(2026, 10, 31))


def test_securities_account_investments(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(
        200, json=ok([{"accountId": "201", "status": "ACTIVE"}, {"accountId": "202", "status": "CANCELLED"}])
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(securities_account()))

    [account] = client_for(session, http).securities_accounts()

    [inv] = account.investments
    assert inv.isin == "IE0000000001"
    assert inv.shares == Decimal("12.345678")
    assert inv.liquidation_value_date == date(2026, 11, 2)


def test_orders_with_range_and_sorted(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(
        200, json=ok([order("a", "2026-01-05"), order("b", "2026-10-01")])
    )

    found = client_for(session, http).orders("201", date(2025, 1, 1), date(2026, 11, 3))

    assert [o.reference for o in found] == ["b", "a"]
    assert route.calls.last.request.url.params["dateFrom"] == "2025-01-01"


def test_orders_without_range_sends_no_dates(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(200, json=ok([]))
    client_for(session, http).orders("201")
    assert "dateFrom" not in route.calls.last.request.url.params


def test_position_history(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    point = {"marketValue": 10.5, "investmentAddedValue": 10.0, "returnValue": 5.0, "totalShares": 1.0}
    route = mock_api.get(f"{CPERF}/v2/pension-accounts/301/positions").respond(
        200,
        json=ok(
            {
                "positions": {
                    "N0001": [{"date": "2026-10-02T00:00:00.000+0000", **point}, {"date": "2026-10-01", **point}]
                }
            }
        ),
    )

    series = client_for(session, http).position_history("301", Period.year, pension=True)

    assert [p.date for p in series["N0001"]] == [date(2026, 10, 1), date(2026, 10, 2)]
    assert route.calls.last.request.url.params["groupType"] == "ONE_YEAR"


def test_fund_search(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{CPERF}/v2/funds").respond(
        200,
        json=ok(
            [{"isin": "IE0000000001", "name": "Global", "ter": 0.12, "annualizedProfitability": {"ytd": 9.1}}],
            pagination={"last": False, "totalElements": 40, "currentPage": 0},
        ),
    )

    page = client_for(session, http).funds("global", limit=1)

    assert page.total == 40 and page.last is False
    assert page.funds[0].annualized_profitability.ytd == Decimal("9.1")
    params = route.calls.last.request.url.params
    assert (params["mainInputFilter"], params["limit"], params["sort"]) == ("global", "1", "ter,asc")


def test_feed_is_server_sent_event(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    body = '{"payload":{"data":{"feed":[{"type":"ORDER","date":"2026-11-01T09:00:00.000Z","title":"Compra"}]}}}'
    mock_api.get(f"{API}/feed/api/feed/self").respond(
        200, content=f"data:{body}\n\n".encode(), headers={"content-type": "text/event-stream"}
    )

    [item] = client_for(session, http).feed()

    assert (item.title, item.date) == ("Compra", date(2026, 11, 1))


def test_api_error_carries_status_code(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{CPERF}/v2/funds/XX").respond(
        500, json={"status": {"code": "ERROR", "message": "Unexpected server error."}}
    )
    with pytest.raises(ApiError) as excinfo:
        client_for(session, http).fund("XX")
    assert (excinfo.value.status, excinfo.value.code) == (500, "ERROR")
    assert "Unexpected server error." in str(excinfo.value)


def test_get_is_retried_on_503(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{CPERF}/v2/cash-accounts/self")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json=ok([]))]
    assert client_for(session, http).cash_accounts() == []
    assert route.call_count == 2
