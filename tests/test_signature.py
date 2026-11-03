from __future__ import annotations

from typing import Any

import pytest

from myinvestor.errors import SignatureError, SignatureRequired
from myinvestor.signature import Challenge, SignatureRequest, Signer, parse_signature_request


class FakeApi:
    """Scripted signature endpoints: records calls and answers from per-path queues."""

    def __init__(self, answers: dict[tuple[str, str], list[Any]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, Any, Any]] = []

    def __call__(self, method: str, path: str, *, params: Any = None, json: Any = None) -> Any:
        self.calls.append((method, path, params, json))
        queue = self.answers[(method, path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class FakeUI:
    def __init__(self, codes: list[str | None] | None = None) -> None:
        self.codes = list(codes or [])
        self.asked: list[tuple[Challenge, int]] = []
        self.approvals: list[SignatureRequest] = []

    def ask_code(self, challenge: Challenge, attempt: int) -> str | None:
        self.asked.append((challenge, attempt))
        return self.codes.pop(0) if self.codes else None

    def approve_in_app(self, request: SignatureRequest) -> None:
        self.approvals.append(request)


BASE = "/signature/api/v3/signature"


def status(state: str, methods: list[list[str]] | None = None) -> dict[str, Any]:
    return {"signatureRequestId": "r1", "status": state, "pendingSignatureMethods": methods or []}


def signer(api: FakeApi, **kwargs: Any) -> Signer:
    clock = iter(range(0, 10_000, 3))
    return Signer(api, sleep=lambda _s: None, clock=lambda: next(clock), **kwargs)


def test_parse_signature_request() -> None:
    request = parse_signature_request(status("PENDING", [["INAPP"], ["OTP_SMS", "OTP_EMAIL"]]))
    assert request == SignatureRequest("r1", "PENDING", [["INAPP"], ["OTP_SMS", "OTP_EMAIL"]])
    assert request.is_waiting_for("OTP_EMAIL")
    assert parse_signature_request({"accessToken": "x"}) is None
    assert parse_signature_request([status("COMPLETE")]) == SignatureRequest("r1", "COMPLETE", [])


def test_inapp_polls_until_approved() -> None:
    api = FakeApi(
        {
            ("POST", f"{BASE}/r1"): [{"signatureId": "s1", "type": "INAPP"}],
            ("GET", BASE): [status("PENDING", [["INAPP"]]), status("COMPLETE")],
        }
    )
    ui = FakeUI()
    result = signer(api).complete(SignatureRequest("r1", "PENDING", [["INAPP"]]), ui)

    assert result.status == "COMPLETE"
    assert len(ui.approvals) == 1
    assert api.calls[0] == ("POST", f"{BASE}/r1", {"method": "INAPP"}, None)
    assert [c[2] for c in api.calls[1:]] == [{"signatureRequestId": "r1"}] * 2


def test_inapp_rejected_in_app() -> None:
    api = FakeApi({("POST", f"{BASE}/r1"): [{"signatureId": "s1"}], ("GET", BASE): [status("CANCELLED", [["INAPP"]])]})
    with pytest.raises(SignatureError, match="rechazado"):
        signer(api).complete(SignatureRequest("r1", "PENDING", [["INAPP"]]), FakeUI())


def test_inapp_times_out() -> None:
    api = FakeApi({("POST", f"{BASE}/r1"): [{"signatureId": "s1"}], ("GET", BASE): [status("PENDING", [["INAPP"]])]})
    with pytest.raises(SignatureError, match="10 s"):
        signer(api, timeout=10).complete(SignatureRequest("r1", "PENDING", [["INAPP"]]), FakeUI())


def test_otp_wrong_then_right_code() -> None:
    api = FakeApi(
        {
            ("POST", f"{BASE}/r1"): [{"signatureId": "s1", "type": "OTP", "channel": "SMS", "destination": "***12"}],
            ("POST", f"{BASE}/r1/validate/s1"): [status("PENDING", [["OTP_SMS"]]), status("COMPLETE")],
        }
    )
    ui = FakeUI(["000000", " 123456 "])
    result = signer(api).complete(SignatureRequest("r1", "PENDING", [["OTP_SMS"]]), ui)

    assert result.status == "COMPLETE"
    assert [attempt for _, attempt in ui.asked] == [1, 2]
    assert ui.asked[0][0].destination == "***12"
    assert api.calls[-1][3] == {"code": "123456"}


def test_otp_cancelled_by_user() -> None:
    api = FakeApi({("POST", f"{BASE}/r1"): [{"signatureId": "s1"}]})
    with pytest.raises(SignatureError, match="cancelada"):
        signer(api).complete(SignatureRequest("r1", "PENDING", [["OTP_SMS"]]), FakeUI([None]))


def test_unsupported_method() -> None:
    with pytest.raises(SignatureError, match="FACE_AUTH"):
        signer(FakeApi({})).complete(SignatureRequest("r1", "PENDING", [["FACE_AUTH"]]), FakeUI())


def test_without_ui_reports_what_is_needed() -> None:
    with pytest.raises(SignatureRequired, match="INAPP"):
        signer(FakeApi({})).complete(SignatureRequest("r1", "PENDING", [["INAPP"]]), None)


def test_public_endpoints_before_login() -> None:
    public = "/signature/api/v3/public/signature"
    api = FakeApi(
        {
            ("POST", f"{public}/r1"): [{"signatureId": "s1"}],
            ("POST", f"{public}/r1/validate/s1"): [status("COMPLETE")],
        }
    )
    signer(api, public=True).complete(SignatureRequest("r1", "PENDING", [["OTP_SMS"]]), FakeUI(["1"]))
    assert {c[1] for c in api.calls} == {f"{public}/r1", f"{public}/r1/validate/s1"}
