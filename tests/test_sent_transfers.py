"""Sent transfers: models, schedule, `upcoming` items and the link to movements (pure, synthetic data)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from myinvestor.models import CashAccount, Remuneration, SentTransfer
from myinvestor.sent_transfers import (
    execution_dates,
    is_scheduled,
    next_execution,
    transfer_items,
    transfer_row,
    with_beneficiaries,
)
from myinvestor.transfers_match import operation_id
from myinvestor.upcoming import Kind
from tests.conftest import cash_account
from tests.test_transfers_match import op_ref, sepa_ref

TODAY = date(2026, 11, 3)
REF = "120261102758295"
IBAN = "ES0000000000000000000001"


def sent(**overrides: Any) -> dict[str, Any]:
    """A sent transfer as the API returns it (synthetic), sender and its ID card included."""
    base = {
        "accountAlias": "Cuenta 1",
        "accountId": "101",
        "amount": 50.0,
        "bank": "BANCO SINTETICO",
        "bicSwift": "SINTESMMXXX",
        "cancelable": False,
        "reusable": True,
        "concept": "Regalo",
        "countryIdBeneficiary": "ES",
        "countryIdDestination": "ES",
        "currency": "EUR",
        "emissionDate": "2026-11-02T00:00:00.000+0000",
        "startDate": None,
        "endDate": None,
        "periodicity": None,
        "ibanDestination": IBAN,
        "nameDestination": "Beneficiaria Ejemplo",
        "reference": REF,
        "status": "COMMITED",
        "transferType": "PUNCTUAL",
        "executionType": "STANDARD",
        "sender": {"id": "s1", "name": "Ana", "surname1": "Ejemplo", "documentNumber": "00000000T", "me": True},
    }
    return {**base, **overrides}


def periodic(**overrides: Any) -> dict[str, Any]:
    base = {
        "reference": "120261001000001",
        "status": "ACTIVE",
        "periodicity": "MONTHLY",
        "startDate": "2026-10-05",
        "emissionDate": None,
        "nameDestination": "Casero",
        "amount": 700.0,
    }
    return sent(**{**base, **overrides})


def model(**overrides: Any) -> SentTransfer:
    return SentTransfer.model_validate(sent(**overrides))


ACCOUNT = CashAccount.model_validate(cash_account(1))


def test_sent_transfer_never_keeps_the_sender() -> None:
    t = model()
    assert t.amount == Decimal("50.0")
    assert t.emission_date == date(2026, 11, 2)
    assert "00000000T" not in t.model_dump_json() and not hasattr(t, "sender")


def test_empty_date_strings_are_none() -> None:
    t = model(startDate="", endDate=" ")
    assert t.start_date is None and t.end_date is None
    r = Remuneration.model_validate({"remunerationDate": "2026-09-01", "paymentDate": "", "endDateRemuneration": ""})
    assert r.remuneration_date == date(2026, 9, 1) and r.payment_date is None and r.end_date_remuneration is None


def test_row_of_a_sent_transfer() -> None:
    row = transfer_row(ACCOUNT, model(), TODAY)
    assert row == {
        "fecha": "2026-11-02",
        "cuenta": "Cuenta 1",
        "cuenta_id": "101",
        "importe": Decimal("-50.0"),
        "concepto": "Regalo",
        "beneficiario": "Beneficiaria Ejemplo",
        "iban_destino": IBAN,
        "banco": "BANCO SINTETICO",
        "bic": "SINTESMMXXX",
        "tipo": "PUNCTUAL",
        "ejecucion": "STANDARD",
        "estado": "COMMITED",
        "ref": REF,
        "periodicidad": None,
        "inicio": None,
        "fin": None,
        "proxima": None,
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, False),  # sent
        ({"status": "ACTIVE", "periodicity": "MONTHLY", "startDate": "2026-01-05"}, True),
        ({"status": "ACTIVE", "periodicity": "MONTHLY", "startDate": "2026-01-05", "endDate": "2026-11-02"}, False),
        ({"status": "CANCELED", "periodicity": "MONTHLY", "startDate": "2026-01-05"}, False),
        ({"status": "PENDING", "startDate": "2026-11-10"}, True),  # a one-off dated in the future
        ({"status": "PENDING", "startDate": "2026-11-01"}, False),
    ],
)
def test_is_scheduled(overrides: dict[str, Any], expected: bool) -> None:
    assert is_scheduled(model(**overrides), TODAY) is expected


def test_execution_dates_step_by_the_periodicity_up_to_the_end() -> None:
    t = model(periodicity="QUATERLY", startDate="2026-01-31", endDate="2027-05-01", status="ACTIVE")
    assert list(execution_dates(t, TODAY, date(2028, 1, 1))) == [date(2027, 1, 31), date(2027, 4, 30)]
    assert next_execution(t, TODAY) == date(2027, 1, 31)


def test_contribution_style_periodicity_and_unknown_ones() -> None:
    weekly = model(periodicity="ONE_WEEK", startDate="2026-11-01", status="ACTIVE")
    assert next_execution(weekly, TODAY) == date(2026, 11, 8)
    odd = model(periodicity="SOMETIMES", startDate="2026-11-20", status="ACTIVE")
    assert list(execution_dates(odd, TODAY, date(2027, 12, 31))) == [date(2026, 11, 20)]


def test_upcoming_items_of_the_scheduled_transfers() -> None:
    transfers = {"101": [SentTransfer.model_validate(periodic()), model()]}
    items = transfer_items(transfers, TODAY, date(2026, 12, 31))
    assert [(i.day, i.kind, i.product, i.amount, i.account_id, i.pending) for i in items] == [
        (date(2026, 11, 5), Kind.transfer, "Casero", Decimal("-700.0"), "101", True),
        (date(2026, 12, 5), Kind.transfer, "Casero", Decimal("-700.0"), "101", True),
    ]


def test_operation_id_reads_internal_and_external_charges() -> None:
    assert operation_id(op_ref(REF, "000259")) == REF
    assert operation_id(op_ref(REF, "000259", gap="1234567890")) == REF  # another bank: 18-27 filled
    assert operation_id(sepa_ref("000261")) is None  # free text where the id goes
    assert operation_id("short") is None and operation_id(None) is None
    assert operation_id(op_ref("120261399000001", "000259")) is None  # not a date inside


def movement(amount: str, raw: str | None, account_id: str = "101") -> dict[str, Any]:
    return {"id": f"x:{amount}", "cuenta_id": account_id, "importe": Decimal(amount), "operacion_ref": raw}


def test_with_beneficiaries_links_each_charge_to_its_sent_transfer() -> None:
    rows = [
        movement("-50", op_ref(REF, "000259", gap="1234567890")),
        movement("50", op_ref(REF, "000032")),  # a credit is never a sent transfer
        movement("-50", op_ref(REF, "000259"), account_id="102"),  # same id, another account
        movement("-12.3", None),
    ]
    out = with_beneficiaries(rows, {"101": [model(ibanDestination="ES00 0000 0000 0000 0000 0001")]})
    assert [(r["beneficiario"], r["iban_destino"]) for r in out] == [
        ("Beneficiaria Ejemplo", IBAN),
        (None, None),
        (None, None),
        (None, None),
    ]
    assert "beneficiario" not in rows[0]  # new rows, the input is untouched
