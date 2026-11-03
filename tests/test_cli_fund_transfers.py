"""`fund-transfers`: fund transfers of securities accounts and pension-plan transfers (synthetic data).

The endpoints answered empty lists on the accounts observed: the item shapes come from the web bundle.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from pydantic import ValidationError
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.errors import ApiError, InputError
from myinvestor.models import FundTransfer, PensionTransfer
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import CPERF, ok, securities_account

runner = CliRunner()
TODAY = date.today()
SECURITIES = f"{CPERF}/v2/securities-accounts"
PENSIONS = f"{CPERF}/v2/pension-accounts"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def fund_transfer(ref: str, day: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "reference": ref,
        "isinOrigin": "IE0000000001",
        "isinDestination": "IE0000000002",
        "status": "IN_PROGRESS",
        "transferDate": f"{day}T00:00:00.000+0000",
        "shares": 12.345678,
        "amount": 1500.5,
        "currency": "EUR",
        "isCancelable": True,
    }
    return {**base, **overrides}


def pension_transfer(ref: str, day: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "reference": ref,
        "dgsCodeOrigin": "N0001",
        "fundNameOrigin": "Plan de origen",
        "dgsCodeDestination": "N0002",
        "pensionPlanNameDestination": "Plan de destino",
        "status": "COMPLETED",
        "transferDate": f"{day}T00:00:00.000+0000",
        "shares": 3.5,
        "amount": 700.0,
        "currency": "EUR",
        "isCancelable": False,
    }
    return {**base, **overrides}


def mock_securities(mock_api: respx.MockRouter) -> None:
    """A plain securities account and a robo-advisor portfolio (`SECURITIES_PORTFOLIO`)."""
    mock_api.get(f"{SECURITIES}/self-basic").respond(200, json=ok([{"accountId": "201"}, {"accountId": "202"}]))
    mock_api.get(f"{SECURITIES}/201").respond(200, json=ok(securities_account("201")))
    mock_api.get(f"{SECURITIES}/202").respond(
        200, json=ok(securities_account("202", accountType="SECURITIES_PORTFOLIO", codAccount="S202"))
    )


def mock_pensions(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{PENSIONS}/self").respond(200, json=ok([{"accountId": "301"}, {"accountId": "302"}]))


def rows(result: Any) -> list[dict[str, Any]]:
    assert result.exit_code == 0, result.output
    data: list[dict[str, Any]] = json.loads(result.stdout, parse_float=Decimal)
    return data


# ---- models -------------------------------------------------------------------------------------


def test_models_tolerate_missing_and_unknown_fields() -> None:
    bare = FundTransfer.model_validate({"reference": 123, "somethingNew": "x"})
    assert bare.reference == "123"
    assert (bare.isin_origin, bare.transfer_date, bare.amount, bare.is_cancelable) == (None, None, None, None)
    pension = PensionTransfer.model_validate({"reference": "P1", "transferDate": ""})
    assert pension.transfer_date is None


def test_models_need_a_reference() -> None:
    with pytest.raises(ValidationError):
        FundTransfer.model_validate({"isinOrigin": "IE0000000001"})
    with pytest.raises(ValidationError):
        PensionTransfer.model_validate({"dgsCodeOrigin": "N0001"})


# ---- securities accounts ------------------------------------------------------------------------


def test_default_asks_the_last_60_days_of_every_securities_account(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_securities(mock_api)
    older = (TODAY - timedelta(days=20)).isoformat()
    newer = (TODAY - timedelta(days=2)).isoformat()
    first = mock_api.get(f"{SECURITIES}/201/transfers").respond(200, json=ok([fund_transfer("T1", older)]))
    portfolio = mock_api.get(f"{SECURITIES}/202/transfers").respond(
        200, json=ok([fund_transfer("T2", newer, isCancelable=None, currency="USD")])
    )

    got = rows(invoke("fund-transfers", "-f", "json"))

    window = {"initDate": (TODAY - timedelta(days=60)).isoformat(), "endDate": TODAY.isoformat()}
    for route in (first, portfolio):
        assert route.call_count == 1
        assert dict(route.calls.last.request.url.params) == window
    assert [r["ref"] for r in got] == ["T2", "T1"]  # newest first
    assert got[1] == {
        "fecha": older,
        "cuenta": "201",
        "origen": "IE0000000001",
        "origen_nombre": None,
        "destino": "IE0000000002",
        "destino_nombre": None,
        "participaciones": Decimal("12.345678"),
        "importe": Decimal("1500.50"),
        "moneda": "EUR",
        "estado": "IN_PROGRESS",
        "cancelable": True,
        "ref": "T1",
    }
    assert got[0]["cancelable"] is False  # like the web: `!!isCancelable`
    assert got[0]["moneda"] == "USD"


def test_the_requested_range_goes_in_a_single_request(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_securities(mock_api)
    route = mock_api.get(f"{SECURITIES}/201/transfers").respond(200, json=ok([]))
    mock_api.get(f"{SECURITIES}/202/transfers").respond(200, json=ok([]))

    result = invoke("fund-transfers", "--since", "2020-01-01", "--until", "2026-06-30", "-f", "json")

    assert rows(result) == []
    assert route.call_count == 1
    assert dict(route.calls.last.request.url.params) == {"initDate": "2020-01-01", "endDate": "2026-06-30"}


def test_until_alone_counts_60_days_back(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_securities(mock_api)
    route = mock_api.get(f"{SECURITIES}/201/transfers").respond(200, json=ok([]))
    mock_api.get(f"{SECURITIES}/202/transfers").respond(200, json=ok([]))

    rows(invoke("fund-transfers", "--to", "2026-03-31", "-f", "json"))

    assert dict(route.calls.last.request.url.params) == {"initDate": "2026-01-30", "endDate": "2026-03-31"}


def test_account_option_picks_a_securities_account_by_id_or_code(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_securities(mock_api)
    plain = mock_api.get(f"{SECURITIES}/201/transfers").respond(200, json=ok([]))
    portfolio = mock_api.get(f"{SECURITIES}/202/transfers").respond(200, json=ok([fund_transfer("T2", "2026-10-01")]))

    got = rows(invoke("fund-transfers", "-a", "S202", "-f", "json"))

    assert [r["ref"] for r in got] == ["T2"]
    assert (plain.call_count, portfolio.call_count) == (0, 1)


def test_unknown_account_is_a_usage_error(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_securities(mock_api)
    with pytest.raises(InputError, match="Cuenta de valores desconocida"):
        invoke("fund-transfers", "-a", "999")


def test_since_after_until_is_a_usage_error(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    with pytest.raises(InputError, match="posterior"):
        invoke("fund-transfers", "--since", "2026-10-02", "--until", "2026-10-01")
    assert not mock_api.calls


def test_a_rejected_range_is_reported_not_retried(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    """If MyInvestor refused a long range (unknown: never seen), the CLI says so instead of guessing."""
    mock_securities(mock_api)
    route = mock_api.get(f"{SECURITIES}/201/transfers").respond(
        400, json={"status": {"code": "BAD_REQUEST", "message": "rango no válido"}}
    )

    with pytest.raises(ApiError) as error:
        invoke("fund-transfers", "--since", "2000-01-01")

    assert error.value.status == 400
    assert route.call_count == 1


def test_table_joins_code_and_name_and_mentions_the_window(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "250")
    mock_pensions(mock_api)
    mock_api.get(f"{PENSIONS}/301/transfers").respond(200, json=ok([pension_transfer("P1", "2026-10-01")]))
    mock_api.get(f"{PENSIONS}/302/transfers").respond(200, json=ok([]))

    result = invoke("--wide", "fund-transfers", "--pension", "--since", "2026-09-01", "--until", "2026-10-31")

    assert result.exit_code == 0, result.output
    assert "N0001 Plan de origen" in result.stdout
    assert "N0002 Plan de destino" in result.stdout
    assert "2026-09-01 → 2026-10-31" in result.stdout


# ---- pension accounts ---------------------------------------------------------------------------


def test_pension_transfers_of_every_pension_account(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_pensions(mock_api)
    first = mock_api.get(f"{PENSIONS}/301/transfers").respond(200, json=ok([pension_transfer("P1", "2026-10-01")]))
    mock_api.get(f"{PENSIONS}/302/transfers").respond(200, json=ok([pension_transfer("P2", "2026-10-15")]))
    securities = mock_api.get(f"{SECURITIES}/self-basic").respond(200, json=ok([]))

    got = rows(invoke("fund-transfers", "--pension", "-f", "json"))

    assert dict(first.calls.last.request.url.params) == {
        "initDate": (TODAY - timedelta(days=60)).isoformat(),
        "endDate": TODAY.isoformat(),
    }
    assert securities.call_count == 0
    assert [r["ref"] for r in got] == ["P2", "P1"]
    assert got[1] == {
        "fecha": "2026-10-01",
        "cuenta": "301",
        "origen": "N0001",
        "origen_nombre": "Plan de origen",
        "destino": "N0002",
        "destino_nombre": "Plan de destino",
        "participaciones": Decimal("3.5"),
        "importe": Decimal("700.00"),
        "moneda": "EUR",
        "estado": "COMPLETED",
        "cancelable": False,
        "ref": "P1",
    }


def test_pension_account_option(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_pensions(mock_api)
    chosen = mock_api.get(f"{PENSIONS}/302/transfers").respond(200, json=ok([]))
    other = mock_api.get(f"{PENSIONS}/301/transfers").respond(200, json=ok([]))

    assert rows(invoke("fund-transfers", "--pension", "-a", "302", "-f", "json")) == []
    assert (chosen.call_count, other.call_count) == (1, 0)


# ---- pending counter ----------------------------------------------------------------------------


def test_pending_counts_per_securities_account(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_securities(mock_api)
    mock_api.get(f"{SECURITIES}/201/transfers/pending").respond(200, json=ok({"pendingTransfers": 0}))
    mock_api.get(f"{SECURITIES}/202/transfers/pending").respond(200, json=ok({"pendingTransfers": 2}))
    listing = mock_api.get(f"{SECURITIES}/201/transfers").respond(200, json=ok([]))

    got = rows(invoke("fund-transfers", "--pending", "-f", "json"))

    assert got == [{"cuenta": "201", "pendientes": 0}, {"cuenta": "202", "pendientes": 2}]
    assert listing.call_count == 0


def test_pending_without_the_counter_is_an_unexpected_response(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_securities(mock_api)
    mock_api.get(f"{SECURITIES}/201/transfers/pending").respond(200, json=ok({}))
    with pytest.raises(ValidationError):
        invoke("fund-transfers", "--pending", "-a", "201")


@pytest.mark.parametrize("extra", [("--pension",), ("--since", "2026-01-01"), ("--until", "2026-01-01")])
def test_pending_does_not_combine_with_pension_nor_dates(
    mock_api: respx.MockRouter, store: MemorySessionStore, extra: tuple[str, ...]
) -> None:
    with pytest.raises(InputError, match="--pending"):
        invoke("fund-transfers", "--pending", *extra)
    assert not mock_api.calls


def test_help_points_to_orders_for_switches_inside_myinvestor() -> None:
    result = runner.invoke(app, ["fund-transfers", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "ALTA IIC SWITCH" in result.stdout
    assert "orders" in result.stdout
