"""`fund-transfers`: internal transfers rebuilt from orders and, for plans, deduced from the history (synthetic)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx

from myinvestor import cli_common
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import ok, order
from tests.test_cli_fund_transfers import (
    ORDERS,
    PENSIONS,
    SECURITIES,
    TODAY,
    fund_transfer,
    invoke,
    mock_pensions,
    mock_securities,
    rows,
)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def no_listed_transfers(mock_api: respx.MockRouter, base: str, *ids: str) -> None:
    for i in ids:
        mock_api.get(f"{base}/{i}/transfers").respond(200, json=ok([]))


def switch(out_ref: str, in_ref: str, day: str, **overrides: Any) -> list[dict[str, Any]]:
    """Both orders of a switch between two funds of account 201 (1000 € from fund 1 into fund 2)."""
    common = {"cash": 1000.0, "status": "EXECUTED", **overrides}
    into = {"isin": "IE0000000002", "fundName": "Fondo destino", "operationType": "IIC_SWITCH_REGISTRATION"}
    return [
        order(out_ref, day, operationType="IIC_SWITCH_DEREGISTRATION", shares=10.0, **common),
        order(in_ref, day, shares=20.0, **into, **common),
    ]


def plan_order(ref: str, day: str, operation: str, dgs: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "pensionAccountId": "301",
        "reference": ref,
        "orderDate": f"{day}T00:00:00.000+0000",
        "dgsCode": dgs,
        "fundName": f"Plan {dgs}",
        "operationType": operation,
        "status": "EXECUTED",
        "cash": 750.0,
        "shares": 7.5,
    }
    return {**base, **overrides}


def point(day: str, shares: float, value: float) -> dict[str, Any]:
    return {"date": day, "marketValue": value, "investmentAddedValue": value, "totalShares": shares}


# ---- funds --------------------------------------------------------------------------------------


def test_a_switch_between_two_funds_is_an_internal_transfer(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_securities(mock_api)
    no_listed_transfers(mock_api, SECURITIES, "201", "202")
    orders = mock_api.get(f"{ORDERS}/201/orders").respond(200, json=ok(switch("O1", "I1", "2026-06-10")))

    got = rows(invoke("fund-transfers", "--since", "2026-06-01", "--until", "2026-07-15", "-f", "json"))

    # a few days of margin on each side, for the other leg of a transfer at the edge of the range
    assert dict(orders.calls.last.request.url.params) == {"dateFrom": "2026-05-25", "dateTo": "2026-07-22"}
    assert got == [
        {
            "fecha": "2026-06-10",
            "tipo": "interno",
            "cuenta": "201",
            "origen": "IE0000000001",
            "origen_nombre": "Fondo indexado",
            "destino": "IE0000000002",
            "destino_nombre": "Fondo destino",
            "participaciones": Decimal("10.0"),
            "importe": Decimal("1000.00"),
            "moneda": "EUR",
            "estado": "EXECUTED",
            "cancelable": False,
            "ref": "O1",
            "fuente": "orders",
            "ref_destino": "I1",
        }
    ]


def test_legs_pair_by_date_and_amount_and_an_unmatched_leg_comes_alone(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_securities(mock_api)
    no_listed_transfers(mock_api, SECURITIES, "201", "202")
    legs = [
        order("R1", "2026-06-02", operationType="INTERNAL_TRANSFER_REIMBURSEMENT", cash=500.0),
        order("S1", "2026-06-04", operationType="INTERNAL_TRANSFER_SUBSCRIPTION", isin="IE0000000003", cash=500.0),
        order("R2", "2026-06-20", operationType="INTERNAL_TRANSFER_REIMBURSEMENT", cash=300.0),  # no partner
        order("X1", "2026-06-21", operationType="IIC_SWITCH_DEREGISTRATION", status="CANCELLED"),  # never executed
        order("B1", "2026-06-05", operationType="INVESTMENT_FUNDS_SUBSCRIPTION"),  # a plain buy
    ]
    mock_api.get(f"{ORDERS}/201/orders").respond(200, json=ok(legs))

    got = rows(invoke("fund-transfers", "--since", "2026-06-01", "--until", "2026-06-30", "-f", "json"))

    assert [(r["ref"], r["ref_destino"], r["origen"], r["destino"]) for r in got] == [
        ("R2", None, "IE0000000001", None),
        ("R1", "S1", "IE0000000001", "IE0000000003"),
    ]
    assert {r["tipo"] for r in got} == {"interno"}


def test_orders_outside_the_range_are_left_out(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_securities(mock_api)
    no_listed_transfers(mock_api, SECURITIES, "201", "202")
    mock_api.get(f"{ORDERS}/201/orders").respond(200, json=ok(switch("O1", "I1", "2026-05-28")))

    assert rows(invoke("fund-transfers", "--since", "2026-06-01", "--until", "2026-06-30", "-f", "json")) == []


def test_a_listed_transfer_that_matches_the_orders_is_one_internal_row(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_securities(mock_api)
    listed = fund_transfer("T1", "2026-06-10", status="EXECUTED", isCancelable=False)
    other = fund_transfer("T2", "2026-06-12", isinOrigin="LU0000000009")  # from another entity
    mock_api.get(f"{SECURITIES}/201/transfers").respond(200, json=ok([listed, other]))
    no_listed_transfers(mock_api, SECURITIES, "202")
    mock_api.get(f"{ORDERS}/201/orders").respond(200, json=ok(switch("O1", "I1", "2026-06-11")))

    got = rows(invoke("fund-transfers", "--since", "2026-06-01", "--until", "2026-06-30", "-f", "json"))

    assert [(r["ref"], r["tipo"], r["fuente"]) for r in got] == [("T2", "externo", "api"), ("T1", "interno", "api")]


def test_table_shows_the_kind_of_transfer(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "250")
    mock_securities(mock_api)
    no_listed_transfers(mock_api, SECURITIES, "201", "202")
    mock_api.get(f"{ORDERS}/201/orders").respond(200, json=ok(switch("O1", "I1", "2026-06-10")))

    result = invoke("--wide", "fund-transfers", "--since", "2026-06-01", "--until", "2026-06-30")

    assert result.exit_code == 0, result.output
    assert "interno" in result.stdout
    assert "IE0000000002 Fondo destino" in result.stdout


# ---- pension plans ------------------------------------------------------------------------------


def test_a_mobilisation_in_the_pension_orders_is_internal(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_pensions(mock_api)
    no_listed_transfers(mock_api, PENSIONS, "301", "302")
    legs = [
        plan_order("D1", "2026-06-10", "DEREGISTRATION_BY_INTERNAL_PLAN_TRANSFER", "N0001"),
        plan_order("A1", "2026-06-11", "REGISTRATION_BY_INTERNAL_PLAN_TRANSFER", "N0002", shares=75.0),
        plan_order("C1", "2026-06-12", "PLAN_CONTRIBUTION", "N0002"),
    ]
    mock_api.get(f"{PENSIONS}/301/orders").respond(200, json=ok(legs))
    series = {  # the history shows the same move: it is not repeated
        "N0001": [point("2026-06-09", 10, 1000), point("2026-06-10", 2.5, 250)],
        "N0002": [point("2026-06-10", 75, 750)],
    }
    mock_api.get(f"{PENSIONS}/301/positions").respond(200, json=ok({"positions": series}))

    got = rows(invoke("fund-transfers", "--pension", "--since", "2026-06-01", "--until", "2026-06-30", "-f", "json"))

    assert got == [
        {
            "fecha": "2026-06-10",
            "tipo": "interno",
            "cuenta": "301",
            "origen": "N0001",
            "origen_nombre": "Plan N0001",
            "destino": "N0002",
            "destino_nombre": "Plan N0002",
            "participaciones": Decimal("7.5"),
            "importe": Decimal("750.00"),
            "moneda": "EUR",
            "estado": "EXECUTED",
            "cancelable": False,
            "ref": "D1",
            "fuente": "orders",
            "ref_destino": "A1",
        }
    ]


def test_a_mobilisation_only_in_the_history_is_deduced(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_pensions(mock_api)
    no_listed_transfers(mock_api, PENSIONS, "301", "302")
    day = (TODAY - timedelta(days=10)).isoformat()
    before = (TODAY - timedelta(days=11)).isoformat()
    series = {
        "N0001": [point(before, 100, 2000), point(day, 25, 505)],  # loses 75 % (NAV 20 → 20.2)
        "N0002": [point(day, 151.5, 1515)],  # born that day with about that value
    }
    history = mock_api.get(f"{PENSIONS}/301/positions").respond(200, json=ok({"positions": series}))

    got = rows(invoke("fund-transfers", "--pension", "-f", "json"))

    assert dict(history.calls.last.request.url.params) == {"groupType": "THREE_MONTHS"}  # reaches 60 days back
    assert got == [
        {
            "fecha": day,
            "tipo": "interno",
            "cuenta": "301",
            "origen": "N0001",
            "origen_nombre": None,
            "destino": "N0002",
            "destino_nombre": None,
            "participaciones": Decimal("75"),
            "importe": Decimal("1515.00"),
            "moneda": "EUR",
            "estado": None,
            "cancelable": False,
            "ref": f"history:301:{day}:N0001>N0002",
            "fuente": "history",
            "ref_destino": None,
        }
    ]

    monkeypatch.setenv("COLUMNS", "250")
    table = invoke("--wide", "fund-transfers", "--pension")
    assert "interno (deducido)" in table.stdout


@pytest.mark.parametrize(
    ("days_back", "period"),
    [(20, "ONE_MONTH"), (60, "THREE_MONTHS"), (120, "SIX_MONTHS"), (300, "ONE_YEAR"), (900, "ONE_YEAR")],
)
def test_the_history_period_reaches_since(
    mock_api: respx.MockRouter, store: MemorySessionStore, days_back: int, period: str
) -> None:
    mock_pensions(mock_api)
    no_listed_transfers(mock_api, PENSIONS, "301", "302")
    history = mock_api.get(f"{PENSIONS}/301/positions").respond(200, json=ok({"positions": {}}))

    since = (TODAY - timedelta(days=days_back)).isoformat()
    rows(invoke("fund-transfers", "--pension", "--since", since, "-f", "json"))

    assert dict(history.calls.last.request.url.params) == {"groupType": period}
