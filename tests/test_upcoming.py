"""`upcoming` rows: contributions due, orders in progress and holds, with the charge account's balance."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from myinvestor.models import CashAccount, Contribution, Hold, Order, Ownership
from myinvestor.upcoming import (
    Kind,
    Shortfall,
    Upcoming,
    contribution_dates,
    shortfalls,
    upcoming_rows,
)
from tests.conftest import cash_account, order

TODAY = date(2026, 11, 3)


def account(i: int = 1, enabled: float = 100.0, **overrides: Any) -> CashAccount:
    return CashAccount.model_validate(cash_account(i, enabledBalance=enabled, withheldBalance=0, **overrides))


def contribution(cid: str, amount: float, next_day: str, **overrides: Any) -> Contribution:
    base: dict[str, Any] = {
        "contributionId": cid,
        "amount": amount,
        "status": "ACTIVE",
        "isin": "IE0000000001",
        "fundName": f"Fondo {cid}",
        "fromAccountId": "101",
        "fromAccountAlias": "Cuenta 1",
        "contributionTimeFrame": {"recurrence": "ONE_MONTH", "nextExecutionDate": f"{next_day}T00:00:00.000+0000"},
    }
    return Contribution.model_validate({**base, **overrides})


def hold(hid: str, amount: float) -> Hold:
    return Hold.model_validate({"id": hid, "type": "FUND_ORDER", "amount": amount})


def an_order(ref: str, status: str, **overrides: Any) -> Order:
    return Order.model_validate(order(ref, "2026-11-02", status=status, **overrides))


def rows_for(**kwargs: Any) -> list[dict[str, Any]]:
    base: dict[str, Any] = {"today": TODAY, "days": 14, "accounts": [account()], "contributions": []}
    return upcoming_rows(**{**base, **kwargs})


def test_contribution_within_window_charges_its_account() -> None:
    rows = rows_for(
        contributions=[
            contribution("c1", 30, "2026-11-05"),
            contribution("c2", 50, "2026-11-20"),  # beyond 14 days
            contribution("c3", 70, "2026-11-04", status="PAUSED"),
            contribution("c4", 70, "2026-11-04", status="CANCELED"),
        ]
    )

    assert rows == [
        {
            "fecha": "2026-11-05",
            "tipo": "aportacion",
            "producto": "Fondo c1",
            "importe": Decimal("-30"),
            "cuenta_cargo": "Cuenta 1",
            "disponible": Decimal("100"),
            "disponible_tras": Decimal("70"),
            "falta": None,
        }
    ]


def test_window_includes_today_and_the_last_day() -> None:
    rows = rows_for(
        days=2,
        contributions=[
            contribution("past", 1, "2026-11-02"),
            contribution("today", 2, "2026-11-03"),
            contribution("last", 3, "2026-11-05"),
            contribution("after", 4, "2026-11-06"),
        ],
    )
    assert [r["producto"] for r in rows] == ["Fondo today", "Fondo last"]


def test_shortfall_accumulates_across_contributions_of_the_same_account() -> None:
    rows = rows_for(
        accounts=[account(1, 100), account(2, 500)],
        contributions=[
            contribution("c1", 60, "2026-11-04"),
            contribution("other", 400, "2026-11-05", fromAccountId="102", fromAccountAlias="Cuenta 2"),
            contribution("c2", 60, "2026-11-06"),
        ],
    )

    assert [(r["cuenta_cargo"], r["disponible_tras"], r["falta"]) for r in rows] == [
        ("Cuenta 1", Decimal(40), None),
        ("Cuenta 2", Decimal(100), None),
        ("Cuenta 1", Decimal(-20), Decimal(20)),
    ]
    assert shortfalls(rows) == [
        Shortfall(account="Cuenta 1", since="2026-11-06", missing=Decimal(20), available=Decimal(100))
    ]


def test_holds_are_shown_but_not_subtracted_again() -> None:
    """`enabledBalance` is already net of holds: a hold must not lower `disponible_tras`."""
    rows = rows_for(
        holds={"101": [hold("h1", 50)]},
        contributions=[contribution("c1", 80, "2026-11-05")],
    )

    assert [(r["tipo"], r["fecha"], r["importe"], r["disponible_tras"], r["falta"]) for r in rows] == [
        ("retencion", "2026-11-03", Decimal(-50), Decimal(100), None),
        ("aportacion", "2026-11-05", Decimal(-80), Decimal(20), None),
    ]
    assert rows[0]["producto"] == "FUND_ORDER"


def test_in_progress_orders_are_listed_with_their_cash_account_but_not_subtracted() -> None:
    orders = [
        (an_order("o1", "PENDING"), "101"),
        (an_order("o2", "IN_PROGRESS", operationType="INVESTMENT_FUND_REIMBURSEMENT", cash=40.0), "101"),
        (an_order("o3", "INITIAL", effectiveDate="2026-11-06T00:00:00.000+0000"), "101"),
        (an_order("o4", "COMPLETE"), "101"),
        (an_order("o5", "EXECUTED"), "101"),
        (an_order("o6", "REJECTED"), "101"),
    ]

    rows = rows_for(orders=orders)

    assert [(r["fecha"], r["tipo"], r["importe"], r["disponible_tras"]) for r in rows] == [
        ("2026-11-02", "orden", Decimal(-150), Decimal(100)),
        ("2026-11-02", "orden", Decimal(40), Decimal(100)),
        ("2026-11-06", "orden", Decimal(-150), Decimal(100)),
    ]
    assert rows[0]["producto"] == "Fondo indexado"


def test_order_without_cash_amount_or_known_account() -> None:
    rows = rows_for(orders=[(an_order("o1", "PENDING", cash=None), None)])
    assert rows[0]["importe"] is None
    assert rows[0]["cuenta_cargo"] is None and rows[0]["disponible"] is None


def test_contribution_matches_the_account_by_id_before_the_alias() -> None:
    accounts = [account(1, 100, alias="Común"), account(2, 300, alias="Ahorro")]
    by_id = contribution("c1", 10, "2026-11-04", fromAccountId="102", fromAccountAlias="Común")
    by_alias = contribution("c2", 10, "2026-11-05", fromAccountId=None, fromAccountAlias="común")
    unknown = contribution("c3", 10, "2026-11-06", fromAccountId="999", fromAccountAlias="Otra")

    rows = rows_for(accounts=accounts, contributions=[by_id, by_alias, unknown])

    assert [(r["cuenta_cargo"], r["disponible"], r["disponible_tras"]) for r in rows] == [
        ("Ahorro", Decimal(300), Decimal(290)),
        ("Común", Decimal(100), Decimal(90)),
        ("Otra", None, None),
    ]


def test_owner_keeps_only_rows_charged_to_those_accounts() -> None:
    junior = account(2, 50, holders=[{"me": False, "participantType": "JUNIOR"}])
    rows = rows_for(
        accounts=[account(1), junior],
        owner=Ownership.junior,
        holds={"101": [hold("h1", 5)], "102": [hold("h2", 6)]},
        contributions=[
            contribution("mine", 10, "2026-11-04"),
            contribution("kid", 10, "2026-11-04", fromAccountId="102"),
            contribution("unknown", 10, "2026-11-04", fromAccountId="999"),
        ],
    )
    assert [(r["tipo"], r["cuenta_cargo"]) for r in rows] == [("retencion", "Cuenta 2"), ("aportacion", "Cuenta 2")]


def test_transfers_extension_point_is_subtracted() -> None:
    transfer = Upcoming(
        day=date(2026, 11, 4), kind=Kind.transfer, product="Alquiler", amount=Decimal(-90), account_id="101"
    )
    rows = rows_for(transfers=[transfer])
    assert rows[0]["tipo"] == "transferencia" and rows[0]["disponible_tras"] == Decimal(10)


def test_rows_are_sorted_by_date_then_kind() -> None:
    rows = rows_for(
        holds={"101": [hold("h1", 1)]},
        orders=[(an_order("o1", "PENDING", orderDate="2026-11-03T00:00:00.000+0000"), "101")],
        contributions=[contribution("c1", 1, "2026-11-10"), contribution("c0", 1, "2026-11-03")],
    )
    assert [(r["fecha"], r["tipo"]) for r in rows] == [
        ("2026-11-03", "retencion"),
        ("2026-11-03", "orden"),
        ("2026-11-03", "aportacion"),
        ("2026-11-10", "aportacion"),
    ]


def test_weekly_contribution_repeats_inside_the_window() -> None:
    weekly = contribution(
        "w", 25, "2026-11-04", contributionTimeFrame={"recurrence": "ONE_WEEK", "nextExecutionDate": "2026-11-04"}
    )
    rows = rows_for(days=14, contributions=[weekly])
    assert [(r["fecha"], r["disponible_tras"]) for r in rows] == [
        ("2026-11-04", Decimal(75)),
        ("2026-11-11", Decimal(50)),
    ]


def test_contribution_dates_follow_the_recurrence_and_stop_at_the_end_date() -> None:
    def dates(recurrence: str, nxt: str, end: date, end_date: str | None = None) -> list[str]:
        frame = contribution(
            "x",
            1,
            nxt,
            contributionTimeFrame={"recurrence": recurrence, "nextExecutionDate": nxt, "endDate": end_date},
        ).contribution_time_frame
        assert frame is not None
        return [d.isoformat() for d in contribution_dates(frame, TODAY, end)]

    assert dates("ONE_MONTH", "2027-01-31", date(2027, 5, 1)) == [
        "2027-01-31",
        "2027-02-28",
        "2027-03-31",
        "2027-04-30",
    ]
    assert dates("TWO_WEEKS", "2026-11-03", date(2026, 12, 1)) == ["2026-11-03", "2026-11-17", "2026-12-01"]
    assert dates("THREE_MONTHS", "2026-11-10", date(2027, 6, 1)) == ["2026-11-10", "2027-02-10", "2027-05-10"]
    assert dates("ONE_WEEK", "2026-11-04", date(2026, 12, 31), "2026-11-18") == [
        "2026-11-04",
        "2026-11-11",
        "2026-11-18",
    ]
    assert dates("UNKNOWN", "2026-11-04", date(2026, 12, 31)) == ["2026-11-04"]
    assert dates("ONE_MONTH", "2026-10-20", date(2026, 12, 31)) == ["2026-11-20", "2026-12-20"]
