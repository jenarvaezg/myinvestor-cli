from __future__ import annotations

from decimal import Decimal

import pytest

from myinvestor.models import CashAccount, Flow, FundSheet, Ownership, stable_ids
from tests.conftest import CASH_UUID, cash_account, flow


def holder(kind: str, name: str, *, me: bool = False) -> dict[str, object]:
    return {
        "id": f"h-{name}",
        "userId": f"u-{name}",
        "name": name,
        "surname1": "Pérez",
        "surname2": "García",
        "documentNumber": "00000000T",
        "me": me,
        "participantType": kind,
    }


def test_flow_parses_class_file_ref_and_migration_flag() -> None:
    f = Flow.model_validate(flow(1, -50.0, fileRef="F-1", isMigrated=False, operationRef="OP-1"))
    assert (f.operation_class, f.file_ref, f.is_migrated, f.operation_ref) == ("ME", "F-1", False, "OP-1")


def test_counterparty_is_target_on_charges_and_origin_on_credits() -> None:
    labels = {"operationOriginLabel": "Ordenante", "operationTargetLabel": "Beneficiario"}
    assert Flow.model_validate(flow(1, -50.0, **labels)).counterparty == "Beneficiario"
    assert Flow.model_validate(flow(1, 50.0, **labels)).counterparty == "Ordenante"
    assert Flow.model_validate(flow(1, 50.0, operationOriginLabel="  ")).counterparty is None
    assert Flow.model_validate(flow(1, 50.0)).counterparty is None


def test_counterparty_falls_back_to_amount_sign_without_flow_type() -> None:
    f = Flow.model_validate(flow(1, -5.0, flowType=None, operationTargetLabel="Beneficiario"))
    assert f.counterparty == "Beneficiario"


def test_stable_id_uses_reference_when_present() -> None:
    f = Flow.model_validate(flow(1, -5.0, reference="000259"))
    assert f.stable_id(CASH_UUID) == f"{CASH_UUID}:000259"


def test_stable_id_without_reference_is_a_hash_independent_of_balance() -> None:
    a = Flow.model_validate(flow(1, -5.0, reference=None, totalBalance=10.0))
    b = Flow.model_validate(flow(1, -5.0, reference=None, totalBalance=99.0))
    other = Flow.model_validate(flow(1, -5.01, reference=None))
    assert a.stable_id(CASH_UUID) == b.stable_id(CASH_UUID)
    assert a.stable_id(CASH_UUID) != other.stable_id(CASH_UUID)
    assert a.stable_id(CASH_UUID) != a.stable_id("otra-cuenta")
    assert a.stable_id(CASH_UUID).startswith(f"{CASH_UUID}:h")


def test_stable_id_ignores_amount_formatting() -> None:
    a = Flow.model_validate(flow(1, 0, reference=None) | {"amount": Decimal("-12.50")})
    b = Flow.model_validate(flow(1, 0, reference=None) | {"amount": Decimal("-12.5")})
    assert a.stable_id(CASH_UUID) == b.stable_id(CASH_UUID)


def test_identical_movements_get_suffixes_that_do_not_depend_on_list_order() -> None:
    first = Flow.model_validate(flow(1, -5.0, reference=None, totalBalance=95.0))
    second = Flow.model_validate(flow(1, -5.0, reference=None, totalBalance=90.0))
    unrelated = Flow.model_validate(flow(2, 7.0))

    forward = dict(zip([first, second, unrelated], stable_ids(CASH_UUID, [first, second, unrelated]), strict=True))
    backward = dict(zip([unrelated, second, first], stable_ids(CASH_UUID, [unrelated, second, first]), strict=True))

    assert forward == backward
    base = first.stable_id(CASH_UUID)
    assert sorted([forward[first], forward[second]]) == [base, f"{base}#2"]
    assert forward[unrelated] == f"{CASH_UUID}:ref2"
    assert len(set(forward.values())) == 3


def test_holders_expose_names_but_not_the_document_number() -> None:
    account = CashAccount.model_validate(cash_account(1, holders=[holder("OWNER", "Ana", me=True)]))
    [h] = account.holders
    assert h.full_name == "Ana Pérez García"
    assert "document_number" not in h.model_dump() and "00000000T" not in repr(h)


def test_full_name_skips_missing_parts() -> None:
    account = CashAccount.model_validate(cash_account(1, holders=[{"name": "Ana", "surname1": None}]))
    assert account.holders[0].full_name == "Ana"
    assert CashAccount.model_validate(cash_account(1, holders=[{}])).holders[0].full_name is None


def test_own_account_role_holder_and_ownership() -> None:
    account = CashAccount.model_validate(cash_account(1, holders=[holder("OWNER", "Ana", me=True)]))
    assert (account.my_role, account.ownership) == ("OWNER", Ownership.me)
    assert account.main_holder is not None and account.main_holder.name == "Ana"


def test_junior_account_names_the_minor_and_my_role_is_representative() -> None:
    holders = [holder("REPRESENTATIVE", "Ana", me=True), holder("JUNIOR", "Leo"), holder("REPRESENTATIVE", "Luis")]
    account = CashAccount.model_validate(cash_account(1, holders=holders))
    assert (account.my_role, account.ownership) == ("REPRESENTATIVE", Ownership.junior)
    assert account.main_holder is not None and account.main_holder.name == "Leo"


def test_account_of_someone_else_is_other() -> None:
    holders = [holder("OWNER", "Luis"), holder("REPRESENTATIVE", "Ana", me=True)]
    account = CashAccount.model_validate(cash_account(1, holders=holders))
    assert account.ownership is Ownership.other
    assert account.main_holder is not None and account.main_holder.name == "Luis"
    assert CashAccount.model_validate(cash_account(1, holders=[])).ownership is Ownership.other


@pytest.mark.parametrize(
    ("currency", "expected"),
    [
        ({"code": "USD", "symbol": "$"}, "USD"),  # the real shape of the fund detail
        ("EUR", "EUR"),
        ({"symbol": "€"}, None),
        ({"code": ""}, None),
        (None, None),
        (7, None),
    ],
)
def test_fund_sheet_currency_is_the_code_of_the_currency_object(currency: object, expected: str | None) -> None:
    sheet = FundSheet.model_validate({"isinCode": "IE0000000001", "ter": 0.12, "currency": currency})

    assert (sheet.currency, sheet.ter) == (expected, Decimal("0.12"))
