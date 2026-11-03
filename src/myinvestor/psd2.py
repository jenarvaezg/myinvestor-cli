"""The PSD2 window of cash-account movements: what MyInvestor serves without a second factor.

Observed: a `dateFrom` up to 89 days before today is answered directly; from ~90 days on, the flows
endpoint answers 202 with a signature request. The exact threshold (89 or 90, counted from today or
from `dateTo`) is not verified, so the CLI stays on the safe side: the free window is the last
`FREE_FLOWS_DAYS` calendar days including today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from myinvestor.config import FREE_FLOWS_DAYS


@dataclass(frozen=True)
class Span:
    """An inclusive range of whole days; `free` if it lies inside the window served without signature."""

    start: date
    end: date
    free: bool


def free_start(today: date) -> date:
    """Oldest `dateFrom` served without a second factor."""
    return today - timedelta(days=FREE_FLOWS_DAYS - 1)


def split_range(start: date, end: date, today: date) -> list[Span]:
    """`[start, end]` as the free span first and the older span (needs a signature) second.

    Spans are cut on whole days and never overlap, so every movement falls in exactly one of them.
    """
    first_free = free_start(today)
    spans: list[Span] = []
    if end >= first_free:
        spans.append(Span(max(start, first_free), end, free=True))
    if start < first_free:
        spans.append(Span(start, min(end, first_free - timedelta(days=1)), free=False))
    return spans
