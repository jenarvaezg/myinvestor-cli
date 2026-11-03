"""#24: the PSD2 window of `movements` (clip, split into free + signed spans, partial results)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli, cli_common
from myinvestor.cli import app
from myinvestor.errors import InputError, PartialResultError
from myinvestor.models import Flow, stable_ids
from myinvestor.psd2 import Span, free_start, split_range
from myinvestor.session import Session
from myinvestor.signature import SIGNATURE_HEADER
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CASH_UUID, CPERF, cash_account, flow, ok, signature_pending

runner = CliRunner()
SECOND_UUID = "aaaaaaaa-0000-0000-0000-000000000002"
FLOWS = r".*/account/api/v3/account/(?P<uuid>[^/]+)/flows"
SIGNATURE = f"{API}/signature/api/v3/signature"
TODAY = date.today()
FREE = free_start(TODAY)
DAY = timedelta(days=1)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def set_tty(value: bool) -> None:
        monkeypatch.setattr(cli_common, "stdin_is_tty", lambda: value)

    set_tty(False)
    monkeypatch.setattr("myinvestor.signature.time.sleep", lambda _s: None)
    return set_tty


def on(day: date, i: int, amount: float, **overrides: Any) -> dict[str, Any]:
    stamp = f"{day.isoformat()}T00:00:00.000+0000"
    return flow(i, amount, operationDate=stamp, valueDate=stamp, **overrides)


class FakeFlows:
    """The flows endpoint: filters by operation date; ranges older than the window answer 202 unless signed."""

    def __init__(self, by_account: dict[str, list[dict[str, Any]]], *, always_202: frozenset[str] = frozenset()):
        self.by_account = by_account
        self.always_202 = always_202
        self.requests: list[tuple[str, str, str, bool]] = []

    def __call__(self, request: httpx.Request, uuid: str) -> httpx.Response:
        start, end = request.url.params["dateFrom"], request.url.params["dateTo"]
        signed = SIGNATURE_HEADER in request.headers
        self.requests.append((uuid, start, end, signed))
        if not signed and (uuid in self.always_202 or date.fromisoformat(start) < FREE):
            return httpx.Response(202, json=signature_pending(f"sig-{uuid[-1]}"))
        rows = [f for f in self.by_account.get(uuid, []) if start <= f["operationDate"][:10] <= end]
        return httpx.Response(200, json=ok({"flowList": rows}))


def mock_api_with(mock_api: respx.MockRouter, fake: FakeFlows, accounts: int = 2) -> respx.Route:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(
        200, json=ok([cash_account(i) for i in range(1, accounts + 1)])
    )
    mock_api.get(url__regex=FLOWS).mock(side_effect=fake)
    mock_api.get(SIGNATURE).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=ok(
                {
                    "signatureRequestId": request.url.params["signatureRequestId"],
                    "status": "COMPLETE",
                    "pendingSignatureMethods": [],
                }
            ),
        )
    )
    return mock_api.post(url__regex=f"{SIGNATURE}/sig-.*").respond(200, json=ok({"signatureId": "s1"}))


def run_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[Any, str, str]:
    monkeypatch.setattr(sys, "argv", ["myinvestor", *args])
    try:
        cli.main()
        status: Any = 0
    except SystemExit as exc:
        status = exc.code or 0
    out = capsys.readouterr()
    return status, out.out, out.err


def two_accounts() -> dict[str, list[dict[str, Any]]]:
    return {
        CASH_UUID: [on(FREE - 30 * DAY, 1, -10.0), on(TODAY - DAY, 2, 20.0)],
        SECOND_UUID: [on(FREE - 5 * DAY, 3, -30.0), on(FREE, 4, 40.0)],
    }


# ---- split -----------------------------------------------------------------------


def test_free_window_is_the_last_90_days_including_today() -> None:
    assert free_start(date(2026, 11, 3)) == date(2026, 8, 6)
    assert (date(2026, 11, 3) - free_start(date(2026, 11, 3))).days == 89


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (FREE, TODAY, [Span(FREE, TODAY, free=True)]),
        (FREE - 10 * DAY, FREE - DAY, [Span(FREE - 10 * DAY, FREE - DAY, free=False)]),
        (
            FREE - 10 * DAY,
            TODAY,
            [Span(FREE, TODAY, free=True), Span(FREE - 10 * DAY, FREE - DAY, free=False)],
        ),
        (FREE - DAY, FREE, [Span(FREE, FREE, free=True), Span(FREE - DAY, FREE - DAY, free=False)]),
    ],
)
def test_split_range_cuts_whole_days_free_span_first(start: date, end: date, expected: list[Span]) -> None:
    assert split_range(start, end, TODAY) == expected


# ---- --free-only / --clip -----------------------------------------------------------


@pytest.mark.parametrize("flag", ["--free-only", "--clip"])
def test_free_only_clips_from_and_warns(
    mock_api: respx.MockRouter, store: MemorySessionStore, tty: Callable[[bool], None], flag: str
) -> None:
    fake = FakeFlows(two_accounts())
    mock_api_with(mock_api, fake, accounts=1)

    result = runner.invoke(app, ["movements", flag, "--from", (TODAY - 200 * DAY).isoformat(), "-f", "json"])

    assert result.exit_code == 0, result.output
    assert [r["importe"] for r in json.loads(result.stdout)] == [20.0]  # still a plain list of rows
    assert fake.requests == [(CASH_UUID, FREE.isoformat(), TODAY.isoformat(), False)]
    assert f"--from {FREE}" in " ".join(result.stderr.split())


def test_free_only_with_a_range_entirely_out_of_the_window_is_an_input_error(
    mock_api: respx.MockRouter, store: MemorySessionStore, tty: Callable[[bool], None]
) -> None:
    fake = FakeFlows(two_accounts())
    mock_api_with(mock_api, fake)
    old = (FREE - DAY).isoformat()

    result = runner.invoke(app, ["movements", "--free-only", "--from", "2024-01-01", "--to", old])

    assert isinstance(result.exception, InputError)
    assert fake.requests == []


# ---- split without UI ------------------------------------------------------------------


def test_long_range_without_terminal_returns_the_free_part_and_exits_13(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_api: respx.MockRouter,
    store: MemorySessionStore,
    tty: Callable[[bool], None],
) -> None:
    fake = FakeFlows(two_accounts())
    start = mock_api_with(mock_api, fake)

    status, out, err = run_main(monkeypatch, capsys, "movements", "--from", (FREE - 60 * DAY).isoformat(), "-f", "json")

    assert status == 13
    assert sorted(r["importe"] for r in json.loads(out)) == [20.0, 40.0]
    assert {s for _, s, _, _ in fake.requests} == {FREE.isoformat()}  # the old span is never asked
    assert not start.called
    error = json.loads(err.strip().splitlines()[-1])
    assert error["error"] == "partial_result" and error["exit_code"] == 13
    assert "Cuenta 1" in error["message"] and "Cuenta 2" in error["message"]


def test_allow_partial_exits_0_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_api: respx.MockRouter,
    store: MemorySessionStore,
    tty: Callable[[bool], None],
) -> None:
    mock_api_with(mock_api, FakeFlows(two_accounts()))

    status, out, err = run_main(
        monkeypatch, capsys, "movements", "--allow-partial", "--from", (FREE - 60 * DAY).isoformat(), "-f", "json"
    )

    assert status == 0
    assert len(json.loads(out)) == 2
    assert "parcial" in err.lower()


def test_202_on_the_second_account_keeps_the_first_one(
    mock_api: respx.MockRouter, store: MemorySessionStore, tty: Callable[[bool], None]
) -> None:
    fake = FakeFlows(two_accounts(), always_202=frozenset({SECOND_UUID}))
    start = mock_api_with(mock_api, fake)

    result = runner.invoke(app, ["movements", "--from", FREE.isoformat(), "-f", "json"])

    assert [r["cuenta"] for r in json.loads(result.stdout)] == ["Cuenta 1"]
    assert isinstance(result.exception, PartialResultError)
    assert "Cuenta 2" in str(result.exception) and "Cuenta 1" not in str(result.exception)
    assert not start.called


# ---- split with UI -------------------------------------------------------------------


def test_approve_asks_free_spans_first_then_one_signature_per_account(
    mock_api: respx.MockRouter, store: MemorySessionStore, tty: Callable[[bool], None]
) -> None:
    fake = FakeFlows(two_accounts())
    start = mock_api_with(mock_api, fake)
    since = FREE - 60 * DAY

    result = runner.invoke(app, ["--approve", "movements", "--from", since.isoformat(), "-f", "json"])

    assert result.exit_code == 0, result.output
    assert sorted(r["importe"] for r in json.loads(result.stdout)) == [-30.0, -10.0, 20.0, 40.0]
    free, old = (FREE.isoformat(), TODAY.isoformat()), (since.isoformat(), (FREE - DAY).isoformat())
    assert fake.requests == [
        (CASH_UUID, *free, False),
        (SECOND_UUID, *free, False),
        (CASH_UUID, *old, False),
        (CASH_UUID, *old, True),
        (SECOND_UUID, *old, False),
        (SECOND_UUID, *old, True),
    ]
    assert start.call_count == 2


def test_a_rejected_signature_keeps_what_was_fetched_and_asks_no_more(
    mock_api: respx.MockRouter, store: MemorySessionStore, tty: Callable[[bool], None]
) -> None:
    fake = FakeFlows(two_accounts())
    start = mock_api_with(mock_api, fake)
    mock_api.get(SIGNATURE).respond(
        200, json=ok({"signatureRequestId": "sig-1", "status": "CANCELLED", "pendingSignatureMethods": []})
    )

    result = runner.invoke(app, ["--approve", "movements", "--from", (FREE - 60 * DAY).isoformat(), "-f", "json"])

    assert sorted(r["importe"] for r in json.loads(result.stdout)) == [20.0, 40.0]
    assert isinstance(result.exception, PartialResultError)
    assert start.call_count == 1  # the second account's old span is not asked after the first was rejected


def test_no_signature_never_starts_one_even_in_a_terminal(
    mock_api: respx.MockRouter, store: MemorySessionStore, tty: Callable[[bool], None]
) -> None:
    tty(True)
    fake = FakeFlows(two_accounts())
    start = mock_api_with(mock_api, fake)

    result = runner.invoke(
        app, ["--no-signature", "--approve", "movements", "--from", (FREE - 60 * DAY).isoformat(), "-f", "json"]
    )

    assert isinstance(result.exception, PartialResultError)
    assert len(json.loads(result.stdout)) == 2
    assert all(not signed for *_, signed in fake.requests)
    assert {s for _, s, _, _ in fake.requests} == {FREE.isoformat()}
    assert not start.called


def test_no_signature_disables_every_signature_ui(monkeypatch: pytest.MonkeyPatch, tty: Callable[[bool], None]) -> None:
    tty(True)
    monkeypatch.setattr(cli_common.options, "approve", True)
    monkeypatch.setattr(cli_common.options, "no_signature", True)
    assert cli_common.signature_ui() is None


# ---- stable ids across spans -----------------------------------------------------------


def test_splitting_does_not_change_ids_on_the_boundary_days(
    mock_api: respx.MockRouter, store: MemorySessionStore, tty: Callable[[bool], None]
) -> None:
    """Twins without `reference` on both sides of the cut keep the ids of a single unsplit request."""
    twins = {"reference": None, "concept": "Cuota", "operationType": "RECIBO"}
    flows = [
        on(FREE, 1, -5.0, totalBalance=95.0, **twins),
        on(FREE, 1, -5.0, totalBalance=90.0, **twins),
        on(FREE - DAY, 1, -5.0, totalBalance=105.0, **twins),
        on(FREE - DAY, 1, -5.0, totalBalance=100.0, **twins),
        on(FREE + DAY, 2, 7.0),
    ]
    fake = FakeFlows({CASH_UUID: flows})
    mock_api_with(mock_api, fake, accounts=1)

    result = runner.invoke(app, ["--approve", "movements", "--from", (FREE - DAY).isoformat(), "-f", "json"])

    assert result.exit_code == 0, result.output
    expected = stable_ids(CASH_UUID, [Flow.model_validate(f) for f in flows])
    assert sorted(r["id"] for r in json.loads(result.stdout)) == sorted(expected)
    assert len(set(expected)) == len(expected) and any(i.endswith("#2") for i in expected)
    spans = {(s, e) for _, s, e, _ in fake.requests}
    assert spans == {(FREE.isoformat(), TODAY.isoformat()), ((FREE - DAY).isoformat(), (FREE - DAY).isoformat())}


def test_movement_id_not_found_in_a_partial_result_is_a_signature_error(
    mock_api: respx.MockRouter, store: MemorySessionStore, tty: Callable[[bool], None]
) -> None:
    mock_api_with(mock_api, FakeFlows(two_accounts()), accounts=1)
    since = (FREE - 60 * DAY).isoformat()

    missing = runner.invoke(app, ["movements", "--id", f"{CASH_UUID}:ref1", "--from", since])
    found = runner.invoke(app, ["movements", "--id", f"{CASH_UUID}:ref2", "--from", since, "-f", "json"])

    assert isinstance(missing.exception, PartialResultError)
    assert found.exit_code == 0 and json.loads(found.stdout)["importe"] == 20.0
