"""Pair both legs of a transfer between the user's own accounts (charge in one, credit in the other).

Pure logic, no I/O. Two legs are the same transfer when:

1. **key** — both carry an `operationRef` in the internal-transfer layout (see `parse_operation_ref`) with
   the same operation id and the same numeric block, opposite amounts and different accounts. A leg whose
   `operationRef` parses only ever pairs by key: a credit from another MyInvestor client looks just like
   mine, but its operation id appears in none of my accounts.
2. **heuristic** — only between legs whose `operationRef` does not parse: a «CARGO POR TRANSFERENCIA» and
   an «ABONO POR TRANSFERENCIA» with opposite amounts, in different accounts, at most `MAX_HEURISTIC_DAYS`
   apart and with «Transferencia desde MyInvestor» in the concept of either. When in doubt (a leg with
   more than one candidate) nothing is paired.

Each movement ends up in at most one pair.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

# Observed `operationRef` layout of an internal transfer (0-based, end excluded); see README «Traspasos internos».
REF_LENGTH = 93
REF_ACCOUNT = slice(0, 18)  # own account number, zero-padded
REF_GAP = slice(18, 28)  # blank on internal transfers; digits on SEPA, card payments, instant transfers
REF_OPERATION_ID = slice(28, 43)  # "1" + YYYYMMDD + 6 digits, the same on both legs: the key
REF_SEPARATOR = 43
REF_ENTITY = slice(44, 68)
REF_BLOCK = slice(68, 78)  # 10 digits, also the same on both legs (and different between transfers)
REF_COUNTER = slice(78, 84)  # the leg's own `reference`
REF_DAY = slice(84, 92)
REF_CHECK = 92  # 9 on charges and 7 on internal credits in the samples; meaning unknown
OP_ID_DAY = slice(1, 9)
COUNTER_WIDTH = REF_COUNTER.stop - REF_COUNTER.start

MAX_HEURISTIC_DAYS = 1
INTERNAL_CONCEPT = "transferencia desde myinvestor"
CHARGE_TYPE = "CARGO POR TRANSFERENCIA"
CREDIT_TYPE = "ABONO POR TRANSFERENCIA"
TRANSFER_WORD = "TRANSFERENCIA"


class MatchKind(StrEnum):
    key = "key"
    heuristic = "heuristic"


@dataclass(frozen=True)
class OperationRef:
    """The parts of an internal-transfer `operationRef` that the CLI understands."""

    account: str
    operation_id: str  # the pairing key
    operation_day: date
    entity: str
    block: str  # corroborates the key; meaning unknown
    counter: str
    booked_day: date
    check: str

    @property
    def pairing_key(self) -> tuple[str, str]:
        return self.operation_id, self.block


def _digits(text: str) -> bool:
    return text.isascii() and text.isdigit()


def _yyyymmdd(text: str) -> date | None:
    if len(text) != 8 or not _digits(text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _same_counter(counter: str, reference: str | None) -> bool:
    ref = (reference or "").strip()
    return not ref or ref.zfill(COUNTER_WIDTH) == counter


def parse_operation_ref(raw: str | None, reference: str | None = None) -> OperationRef | None:
    """The fields of `raw`, or None unless it is exactly the internal-transfer layout.

    With `reference` (the movement's own counter), it must also match the counter inside `raw`.
    """
    if not raw or len(raw) < REF_LENGTH or raw[REF_LENGTH:].strip():
        return None
    account, op_id, block, counter = raw[REF_ACCOUNT], raw[REF_OPERATION_ID], raw[REF_BLOCK], raw[REF_COUNTER]
    layout = (
        _digits(account)
        and not raw[REF_GAP].strip()
        and _digits(op_id)
        and raw[REF_SEPARATOR] == " "
        and _digits(block)
        and _digits(counter)
        and _digits(raw[REF_CHECK])
        and _same_counter(counter, reference)
    )
    op_day, booked = _yyyymmdd(op_id[OP_ID_DAY]), _yyyymmdd(raw[REF_DAY])
    if not layout or op_day is None or booked is None:
        return None
    return OperationRef(
        account=account,
        operation_id=op_id,
        operation_day=op_day,
        entity=raw[REF_ENTITY].strip(),
        block=block,
        counter=counter,
        booked_day=booked,
        check=raw[REF_CHECK],
    )


def operation_id(raw: str | None) -> str | None:
    """Just the operation id (positions 28-42) of a charge's `operationRef`, or None if it is not one.

    Looser than `parse_operation_ref`: a transfer to another bank fills positions 18-27, but the id is
    still there, and it is the `reference` of the sent transfer (`…/cash-accounts/{id}/transfers`).
    """
    op_id = (raw or "")[REF_OPERATION_ID]
    if len(op_id) != REF_OPERATION_ID.stop - REF_OPERATION_ID.start or not _digits(op_id):
        return None
    return op_id if _yyyymmdd(op_id[OP_ID_DAY]) else None


@dataclass(frozen=True)
class Leg:
    """One side of a possible transfer: a movement of one of the user's accounts."""

    id: str
    account: str  # any stable account identity (the CLI uses the uuid)
    day: date
    amount: Decimal  # signed: charges are negative
    concept: str | None = None
    operation_ref: str | None = None
    reference: str | None = None
    kind: str | None = None  # `operationType`, e.g. «ABONO POR TRANSFERENCIA»

    @property
    def parsed_ref(self) -> OperationRef | None:
        return parse_operation_ref(self.operation_ref, self.reference)

    @property
    def key(self) -> str | None:
        parsed = self.parsed_ref
        return parsed.operation_id if parsed else None

    @property
    def internal_concept(self) -> bool:
        return INTERNAL_CONCEPT in " ".join((self.concept or "").casefold().split())

    @property
    def transfer_like(self) -> bool:
        """Worth listing when unpaired: a readable key, the internal concept or a transfer type."""
        return self.parsed_ref is not None or self.internal_concept or TRANSFER_WORD in (self.kind or "").upper()


@dataclass(frozen=True)
class Transfer:
    source: Leg  # the charge
    target: Leg  # the credit
    key: str | None
    match: MatchKind


@dataclass(frozen=True)
class Matching:
    pairs: tuple[Transfer, ...]
    unmatched: tuple[Leg, ...]  # transfer-like legs left without partner

    def partners(self) -> dict[str, str]:
        """Movement id -> id of the other leg, for every paired movement."""
        return {a.id: b.id for p in self.pairs for a, b in ((p.source, p.target), (p.target, p.source))}


def _order(leg: Leg) -> tuple[date, str]:
    return leg.day, leg.id


def _counterparts(source: Leg, target: Leg) -> bool:
    return source.amount < 0 and target.amount == -source.amount and source.account != target.account


def _key_pairs(legs: Sequence[Leg], parsed: Mapping[str, OperationRef | None]) -> list[Transfer]:
    """Within each (operation id, block) group, each charge (oldest first) takes the oldest free credit."""
    groups: defaultdict[tuple[str, str], list[Leg]] = defaultdict(list)
    for leg in legs:
        if (ref := parsed[leg.id]) is not None:
            groups[ref.pairing_key].append(leg)
    pairs: list[Transfer] = []
    for key in sorted(groups):
        group = sorted(groups[key], key=_order)
        free = [leg for leg in group if leg.amount > 0]
        for source in (leg for leg in group if leg.amount < 0):
            target = next((t for t in free if _counterparts(source, t)), None)
            if target is not None:
                free.remove(target)
                pairs.append(Transfer(source, target, key[0], MatchKind.key))
    return pairs


def _heuristic_fit(source: Leg, target: Leg) -> bool:
    return (
        source.kind == CHARGE_TYPE
        and target.kind == CREDIT_TYPE
        and _counterparts(source, target)
        and abs((target.day - source.day).days) <= MAX_HEURISTIC_DAYS
        and (source.internal_concept or target.internal_concept)
    )


def _heuristic_pairs(keyless: Sequence[Leg]) -> list[Transfer]:
    """Only pairs where the charge has a single candidate credit and that credit a single candidate charge."""
    sources = [leg for leg in keyless if leg.kind == CHARGE_TYPE]
    targets = [leg for leg in keyless if leg.kind == CREDIT_TYPE]
    fits = [(s, t) for s in sources for t in targets if _heuristic_fit(s, t)]
    charges: defaultdict[str, int] = defaultdict(int)
    credits: defaultdict[str, int] = defaultdict(int)
    for s, t in fits:
        charges[s.id] += 1
        credits[t.id] += 1
    return [Transfer(s, t, None, MatchKind.heuristic) for s, t in fits if charges[s.id] == 1 and credits[t.id] == 1]


def match_transfers(legs: Sequence[Leg]) -> Matching:
    """Pair charges with credits: by key first, then the heuristic among the legs without a readable key."""
    parsed = {leg.id: leg.parsed_ref for leg in legs}
    pairs = _key_pairs(legs, parsed)
    pairs += _heuristic_pairs([leg for leg in legs if parsed[leg.id] is None])
    used = {leg.id for p in pairs for leg in (p.source, p.target)}
    unmatched = tuple(leg for leg in sorted(legs, key=_order) if leg.id not in used and leg.transfer_like)
    return Matching(tuple(sorted(pairs, key=lambda p: _order(p.source))), unmatched)


# ---- movement rows (`movements` / `internal-transfers`) --------------------------------------


def movement_leg(row: Mapping[str, Any]) -> Leg:
    """A `movements` row as a `Leg`."""
    return Leg(
        id=row["id"],
        account=row["cuenta_uuid"],
        day=date.fromisoformat(row["fecha"]),
        amount=Decimal(row["importe"]),
        concept=row.get("concepto"),
        operation_ref=row.get("operacion_ref"),
        reference=row.get("ref"),
        kind=row.get("tipo"),
    )


def match_movements(rows: Sequence[Mapping[str, Any]]) -> Matching:
    return match_transfers([movement_leg(r) for r in rows])


def with_transfer_fields(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """New `movements` rows with `pareja_id` (the other leg's id) and `traspaso_interno`."""
    partners = match_movements(rows).partners()
    return [{**r, "pareja_id": partners.get(r["id"]), "traspaso_interno": r["id"] in partners} for r in rows]


def _transfer_row(
    source: Leg | None, target: Leg | None, key: str | None, match: MatchKind | None, labels: Mapping[str, Any]
) -> dict[str, Any]:
    """`fecha` and `importe` come from the charge, or from the credit when there is no charge."""
    legs = [leg for leg in (source, target) if leg is not None]
    return {
        "fecha": legs[0].day.isoformat(),
        "importe": abs(legs[0].amount),
        "desde_cuenta": labels[source.id] if source else None,
        "desde_id": source.id if source else None,
        "hacia_cuenta": labels[target.id] if target else None,
        "hacia_id": target.id if target else None,
        "concepto": next((leg.concept for leg in legs if leg.concept), None),
        "clave": key,
        "match": match.value if match else None,
    }


def transfer_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flat rows of the pairs and of the transfer-like movements left unpaired (`match: None`)."""
    matching = match_movements(rows)
    labels = {r["id"]: r["cuenta"] for r in rows}
    pairs = [_transfer_row(p.source, p.target, p.key, p.match, labels) for p in matching.pairs]
    lonely = [
        _transfer_row(leg, None, leg.key, None, labels)
        if leg.amount < 0
        else _transfer_row(None, leg, leg.key, None, labels)
        for leg in matching.unmatched
    ]
    return pairs, lonely
