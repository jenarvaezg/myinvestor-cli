"""Interest settlements of the movements: which credits count and the period their concept names."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from myinvestor.interest_settlements import credited_on, settlement_period, settlements
from myinvestor.models import Flow
from tests.conftest import flow


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("LIQUIDAC. INTERESES PERIODO 18/09/2026 18/10/2026",), (date(2026, 9, 18), date(2026, 10, 18))),
        (("Liquidación intereses período  22/09/2026   22/10/2026",), (date(2026, 9, 22), date(2026, 10, 22))),
        ((None, "LIQUIDAC. INTERESES PERIODO 01/09/2026 30/09/2026"), (date(2026, 9, 1), date(2026, 9, 30))),
        (("LIQUIDAC. INTERESES",), None),
        (("PERIODO 31/02/2026 18/10/2026",), None),  # not a date
        (("PERIODO 18/10/2026 18/09/2026",), None),  # backwards
    ],
)
def test_settlement_period(texts: tuple[str | None, ...], expected: tuple[date, date] | None) -> None:
    assert settlement_period(*texts) == expected


def test_settlements_are_the_interest_credits_newest_first() -> None:
    flows = [
        Flow.model_validate(f)
        for f in (
            flow(1, 0.04, operationType="LIQUIDAC. INTERESES", concept="PERIODO 18/08/2026 18/09/2026"),
            flow(3, 0.05, operationType="LIQUIDAC. INTERESES", concept="PERIODO 18/09/2026 18/10/2026"),
            flow(2, -0.01, operationType="LIQUIDAC. INTERESES"),  # a charge is not a credit
            flow(4, 300.0, operationType="ABONO POR TRANSFERENCIA"),
        )
    ]

    found = settlements(flows)

    assert [(s.day, s.amount, s.start) for s in found] == [
        (date(2026, 10, 13), Decimal("0.05"), date(2026, 9, 18)),
        (date(2026, 10, 11), Decimal("0.04"), date(2026, 8, 18)),
    ]
    assert credited_on(found, date(2026, 10, 11)) == found[1]
    assert credited_on(found, date(2026, 10, 12)) is None
