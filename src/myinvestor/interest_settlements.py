"""Interest settlements («LIQUIDAC. INTERESES») of a cash account's movements, crossed with `interest`.

A settlement's concept names the period it pays: «LIQUIDAC. INTERESES PERIODO 18/09/2026 18/10/2026».
Without a promotion the period runs from the account's opening day of one month to the same day of
the next (18 to 18, 22 to 22…), not by calendar month, and it is credited the day after it ends.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from myinvestor.classify import Category, classify, normalize
from myinvestor.models import Flow

_PERIOD = re.compile(r"PERIODO\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})")


@dataclass(frozen=True)
class Settlement:
    """One interest credit: when it was booked, how much, and the period its concept names (if any)."""

    day: date
    value_day: date | None
    amount: Decimal
    start: date | None
    end: date | None


def _day(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        return None


def settlement_period(*texts: str | None) -> tuple[date, date] | None:
    """The `PERIODO dd/mm/aaaa dd/mm/aaaa` of the first text that names one, as two dates."""
    for text in texts:
        match = _PERIOD.search(normalize(text))
        if match:
            start, end = _day(match[1]), _day(match[2])
            if start and end and start <= end:
                return start, end
    return None


def settlements(flows: Iterable[Flow]) -> list[Settlement]:
    """The interest credits among `flows` (classified as in `income`), newest first."""
    found: list[Settlement] = []
    for f in flows:
        if f.amount <= 0 or classify(f.operation_type, f.operation_class).category is not Category.intereses:
            continue
        period = settlement_period(f.concept, f.operation_type)
        start, end = period if period else (None, None)
        found.append(Settlement(f.operation_date, f.value_date, f.amount, start, end))
    return sorted(found, key=lambda s: s.day, reverse=True)


def credited_on(found: list[Settlement], day: date) -> Settlement | None:
    """The settlement booked (or valued) on `day`, the newest if several."""
    return next((s for s in found if day in (s.day, s.value_day)), None)
