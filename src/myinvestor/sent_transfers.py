"""Transfers sent from the cash accounts: rows of `transfers`, their schedule and the link to movements.

Pure (no I/O). Values the web knows (bundle), most never seen in a real response:

- `status`: `COMMITED` (sic; «Enviada», the only one seen), `ACTIVE` («Programada»: a periodic transfer
  still running), `PENDING`, `ACCEPTED_SETTLEMENT_IN_PROGRESS` («Enviando»), `CANCELED`, `REJECTED`.
- `periodicity`: `WEEKLY`, `BIWEEKLY`, `MONTHLY`, `BIMONTHLY`, `QUARTERLY` (the web also accepts the typo
  `QUATERLY`), `BIANNUAL`, `ANNUAL`; and the contribution style `ONE_WEEK`…`ONE_YEAR`, which it maps to those.
- `transferType`: only `PUNCTUAL` seen; the web ignores it (it tells periodic ones by `periodicity`).
- `executionType`: `STANDARD` («Ordinaria») or `INSTANT` («Inmediata»).

A movement is the charge of a sent transfer when the operation id inside its `operationRef`
(positions 28-42, `transfers_match.operation_id`) equals the transfer's `reference`, in the same account.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from myinvestor.models import CashAccount, SentTransfer
from myinvestor.transfers_match import operation_id
from myinvestor.upcoming import Kind, Upcoming, recurrence_dates

# A transfer in one of these states will not run again.
DONE_STATUSES = frozenset({"COMMITED", "COMMITTED", "CANCELED", "CANCELLED", "REJECTED"})
# `periodicity` → the recurrence keys of `upcoming` (`RECURRENCE_DAYS`/`RECURRENCE_MONTHS`).
PERIODICITY_RECURRENCE = {
    "WEEKLY": "ONE_WEEK",
    "BIWEEKLY": "TWO_WEEKS",
    "MONTHLY": "ONE_MONTH",
    "BIMONTHLY": "TWO_MONTHS",
    "QUARTERLY": "THREE_MONTHS",
    "QUATERLY": "THREE_MONTHS",
    "BIANNUAL": "SIX_MONTHS",
    "ANNUAL": "ONE_YEAR",
}
FAR_FUTURE = date(9999, 12, 31)


def recurrence(t: SentTransfer) -> str | None:
    """The `upcoming` recurrence key of a periodic transfer (an unknown periodicity is passed as is)."""
    if not t.periodicity:
        return None
    key = t.periodicity.strip().upper()
    return PERIODICITY_RECURRENCE.get(key, key)


def is_scheduled(t: SentTransfer, today: date) -> bool:
    """Still to run: periodic and not ended, or dated from today on; never once sent, cancelled or rejected."""
    if (t.status or "").upper() in DONE_STATUSES:
        return False
    if t.periodicity:
        return t.end_date is None or t.end_date >= today
    return t.start_date is not None and t.start_date >= today


def execution_dates(t: SentTransfer, start: date, end: date) -> Iterable[date]:
    """Executions of a scheduled transfer in [start, end]: from `startDate`, every periodicity step, up to
    `endDate`. Estimates (no bank holidays); an unknown periodicity only gives `startDate`."""
    if t.start_date is None:
        return ()
    return recurrence_dates(t.start_date, recurrence(t), t.end_date, start, end)


def next_execution(t: SentTransfer, today: date) -> date | None:
    if not is_scheduled(t, today):
        return None
    return next(iter(execution_dates(t, today, FAR_FUTURE)), None)


def transfer_row(account: CashAccount, t: SentTransfer, today: date) -> dict[str, Any]:
    """`importe` is negative, like the charge it makes; the sender (with the ID card) is never exported."""
    return {
        "fecha": t.emission_date.isoformat() if t.emission_date else None,
        "cuenta": account.alias or t.account_alias or account.account_id,
        "cuenta_id": account.account_id,
        "importe": -abs(t.amount),
        "concepto": t.concept,
        "beneficiario": t.name_destination,
        "iban_destino": _iban(t),
        "banco": t.bank,
        "bic": t.bic_swift,
        "tipo": t.transfer_type,
        "ejecucion": t.execution_type,
        "estado": t.status,
        "ref": t.reference,
        "periodicidad": t.periodicity,
        "inicio": t.start_date.isoformat() if t.start_date else None,
        "fin": t.end_date.isoformat() if t.end_date else None,
        "proxima": _iso(next_execution(t, today)),
    }


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def transfer_items(transfers: Mapping[str, Sequence[SentTransfer]], start: date, end: date) -> list[Upcoming]:
    """`upcoming` items of the scheduled transfers in [start, end], by the `account_id` they were read from."""
    return [
        Upcoming(day, Kind.transfer, t.name_destination or t.concept, -abs(t.amount), account_id, t.account_alias)
        for account_id, found in transfers.items()
        for t in found
        if is_scheduled(t, start)
        for day in execution_dates(t, start, end)
    ]


def with_beneficiaries(
    rows: Sequence[Mapping[str, Any]], transfers: Mapping[str, Sequence[SentTransfer]]
) -> list[dict[str, Any]]:
    """New `movements` rows with `beneficiario` and `iban_destino` of the sent transfer each charge is.

    `transfers` maps a cash account's `account_id` to the transfers sent from it; rows that are not the
    charge of one of them get `None`.
    """
    by_ref = {(account_id, t.reference): t for account_id, found in transfers.items() for t in found}
    out: list[dict[str, Any]] = []
    for row in rows:
        hit = _sent_transfer(row, by_ref)
        out.append(
            {**row, "beneficiario": hit.name_destination if hit else None, "iban_destino": _iban(hit) if hit else None}
        )
    return out


def _sent_transfer(row: Mapping[str, Any], by_ref: Mapping[tuple[str, str], SentTransfer]) -> SentTransfer | None:
    if row["importe"] >= 0:
        return None
    op_id = operation_id(row.get("operacion_ref"))
    return by_ref.get((row["cuenta_id"], op_id)) if op_id else None


def _iban(t: SentTransfer) -> str | None:
    return t.iban_destination.replace(" ", "") if t.iban_destination else None
