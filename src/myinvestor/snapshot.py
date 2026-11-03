"""`snapshot`: one JSON document with everything a daily routine reads, and its delta against the previous one.

Pure (no API calls): the CLI fetches the sections and this module checks, compares and stores the documents.
The full schema and the delta formula are documented in `docs/snapshot.md`.

Delta per product (`efectivo`, `valores`, `pensiones` row of the same `id` in both snapshots; a product
missing on one side counts as 0 there):

- `valor` = value now - value before (`saldo` for cash, `valor` for securities and pension accounts).
- `aportado`, the net money that went into the product:
  - securities and pension accounts: `invertido` now - `invertido` before;
  - cash accounts: the sum of the account's movements booked since the previous snapshot, except
    interest, dividends, withholding and fees (`classify.INCOME_CATEGORIES`), which are return. A
    movement is "since the previous snapshot" when its `id` is not among the previous snapshot's
    `movements` and its date is not older than the oldest day that snapshot looked at.
- `mercado` = `valor` - `aportado`.

The total is `total` now - `total` before, with `aportado` the sum of the products'. Money moved between
the user's own products (an internal transfer, a fund bought with cash, a pension contribution) leaves
one product and enters another, so it cancels out there and only money from or to outside MyInvestor
remains. Whatever cannot be computed (a section missing on either side, a gap in the movements) is None.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from myinvestor.atomicfile import ensure_private_dir, locked, write_atomic
from myinvestor.classify import INCOME_CATEGORIES, classify
from myinvestor.errors import (
    EXIT_CORRUPT_STORE,
    EXIT_CREDENTIALS,
    EXIT_ERROR,
    EXIT_NETWORK,
    EXIT_NO_SESSION,
    EXIT_SIGNATURE,
    EXIT_USAGE,
    InputError,
    MyInvestorError,
)
from myinvestor.json_output import dumps
from myinvestor.output import normalize_row

SCHEMA = "myinvestor.snapshot/1"
LOCK_NAME = ".snapshot.lock"
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")

# Exit code of a snapshot with failed sections: the first of these that any section has. Session and
# second-factor problems need a person, so they outrank an unexpected error; a network error is the
# mildest, since retrying later usually fixes it.
SEVERITY = (EXIT_CORRUPT_STORE, EXIT_CREDENTIALS, EXIT_NO_SESSION, EXIT_SIGNATURE, EXIT_ERROR, EXIT_USAGE, EXIT_NETWORK)

ZERO = Decimal(0)


class Section(StrEnum):
    """`section` of an entry in `errors`."""

    cash = "cash"
    securities = "securities"
    pensions = "pensions"
    pending = "pending"
    upcoming = "upcoming"
    movements = "movements"
    new_movements = "new.movements"
    new_orders = "new.orders"
    new_pension_orders = "new.pension_orders"
    delta = "delta"


class Product(StrEnum):
    """`producto` of a delta row, and the section its rows come from."""

    cash = "efectivo"
    securities = "valores"
    pensions = "pensiones"


PRODUCT_SECTIONS = {Product.cash: "cash", Product.securities: "securities", Product.pensions: "pensions"}


@dataclass(frozen=True)
class SectionError:
    section: str
    error: str
    exit_code: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"section": self.section, "error": self.error, "exit_code": self.exit_code, "message": self.message}


class InvalidSnapshotError(InputError):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path} no es un snapshot válido de myinvestor ({reason}).")


class IncompleteSnapshotError(MyInvestorError):
    """Some sections failed; the snapshot was still printed (and saved) with them in `errors`."""

    def __init__(self, errors: Sequence[SectionError]) -> None:
        self.errors = tuple(errors)
        self.exit_code = worst_exit_code(e.exit_code for e in errors)
        failed = ", ".join(f"{e.section} ({e.exit_code}: {e.error})" for e in errors)
        super().__init__(f"Snapshot incompleto, han fallado: {failed}. El detalle va en `errors`.")


def worst_exit_code(codes: Iterable[int]) -> int:
    """0 if there is none; else the first of `SEVERITY` present (an unknown code counts as 1)."""
    present = set(codes)
    if not present:
        return 0
    return next((code for code in SEVERITY if code in present), EXIT_ERROR)


def normalized(value: Any) -> Any:
    """Amounts rounded to cents in every row, nested ones included (`posiciones`, `planes`…)."""
    if isinstance(value, Mapping):
        return {key: normalized(item) for key, item in normalize_row(dict(value)).items()}
    if isinstance(value, list | tuple):
        return [normalized(item) for item in value]
    return value


def snapshot_total(snapshot: Mapping[str, Any]) -> Decimal | None:
    """Cash balances + securities + pension accounts, like `position`; None if any of them is missing."""
    cash, securities, pensions = (snapshot.get(s) for s in ("cash", "securities", "pensions"))
    if cash is None or securities is None or pensions is None:
        return None
    values = [r["saldo"] for r in cash] + [r["valor"] for r in [*securities, *pensions]]
    return sum((_decimal(v) or ZERO for v in values), ZERO)


# ---- delta -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Holding:
    id: str
    name: str | None
    value: Decimal
    invested: Decimal | None


def compute_delta(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    """`delta` of `current` against `previous` (see the module docstring for the formula)."""
    flows = cash_flows(current, previous)
    rows: list[dict[str, Any]] = []
    complete = True
    for product, section in PRODUCT_SECTIONS.items():
        now, before = _holdings(current, product, section), _holdings(previous, product, section)
        if now is None or before is None:
            complete = False
            continue
        rows += [_delta_row(product, pid, now.get(pid), before.get(pid), flows) for pid in {**now, **before}]
    total = _difference(current.get("total"), previous.get("total"))
    contributed: Decimal | None = None
    if complete and total is not None and all(r["aportado"] is not None for r in rows):
        contributed = sum((r["aportado"] for r in rows), ZERO)
    return {
        "since": previous.get("as_of"),
        "total": total,
        "aportado": contributed,
        "mercado": total - contributed if total is not None and contributed is not None else None,
        "by_product": rows,
    }


def _holdings(snapshot: Mapping[str, Any], product: Product, section: str) -> dict[str, _Holding] | None:
    rows = snapshot.get(section)
    if rows is None:
        return None
    if product is Product.cash:
        return {str(r["id"]): _Holding(str(r["id"]), r.get("alias"), _decimal(r["saldo"]) or ZERO, None) for r in rows}
    return {
        str(r["id"]): _Holding(str(r["id"]), r.get("nombre"), _decimal(r["valor"]) or ZERO, _decimal(r["invertido"]))
        for r in rows
    }


def _delta_row(
    product: Product,
    pid: str,
    now: _Holding | None,
    before: _Holding | None,
    flows: CashFlows | None,
) -> dict[str, Any]:
    value = _value(now) - _value(before)
    contributed: Decimal | None
    if product is Product.cash:
        contributed = flows.of(pid) if flows else None
    else:
        invested_now, invested_before = _invested(now), _invested(before)
        contributed = None if invested_now is None or invested_before is None else invested_now - invested_before
    named = now or before
    return {
        "producto": product.value,
        "id": pid,
        "nombre": named.name if named else None,
        "valor": value,
        "aportado": contributed,
        "mercado": value - contributed if contributed is not None else None,
    }


def _value(holding: _Holding | None) -> Decimal:
    """A product missing from a snapshot was worth 0 there."""
    return holding.value if holding else ZERO


def _invested(holding: _Holding | None) -> Decimal | None:
    """0 for a product missing from a snapshot; None if the API did not say."""
    return ZERO if holding is None else holding.invested


@dataclass(frozen=True)
class CashFlows:
    """Net money moved in or out of each cash account since the previous snapshot (by `cuenta_id`)."""

    sums: Mapping[str, Decimal]
    unknown: frozenset[str]  # accounts with a gap in either snapshot's movements

    def of(self, account_id: str) -> Decimal | None:
        return None if account_id in self.unknown else self.sums.get(account_id, ZERO)


def cash_flows(current: Mapping[str, Any], previous: Mapping[str, Any]) -> CashFlows | None:
    """None when it cannot be known: either snapshot lacks `movements`, or the current one does not reach
    back to the previous snapshot's day."""
    now, before = current.get("movements"), previous.get("movements")
    previous_day = snapshot_day(previous)
    if now is None or before is None or previous_day is None:
        return None
    if date.fromisoformat(now["desde"]) > previous_day:
        return None
    oldest = max(date.fromisoformat(before["desde"]), date.fromisoformat(now["desde"]))
    seen = {r["id"] for r in before["movimientos"]}
    sums: dict[str, Decimal] = {}
    for r in now["movimientos"]:
        if r["id"] in seen or date.fromisoformat(r["fecha"]) < oldest or _is_income(r):
            continue
        account = str(r["cuenta_id"])
        sums[account] = sums.get(account, ZERO) + (_decimal(r["importe"]) or ZERO)
    unknown = frozenset(str(g["cuenta_id"]) for m in (now, before) for g in m.get("huecos", []))
    return CashFlows(sums, unknown)


def _is_income(row: Mapping[str, Any]) -> bool:
    kind = classify(row.get("tipo"), row.get("clase"), internal_transfer=bool(row.get("traspaso_interno")))
    return kind.category in INCOME_CATEGORIES


def _difference(now: Any, before: Any) -> Decimal | None:
    a, b = _decimal(now), _decimal(before)
    return a - b if a is not None and b is not None else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"importe no numérico: {value!r}") from exc


def snapshot_day(snapshot: Mapping[str, Any]) -> date | None:
    """The local day of `as_of`."""
    as_of = snapshot.get("as_of")
    return datetime.fromisoformat(as_of).date() if isinstance(as_of, str) else None


# ---- files -------------------------------------------------------------------------------------


def snapshot_path(directory: Path, day: date) -> Path:
    return directory / f"{day.isoformat()}.json"


def latest_before(directory: Path, day: date) -> Path | None:
    """The newest `AAAA-MM-DD.json` in `directory` dated before `day`."""
    if not directory.is_dir():
        return None
    days = []
    for path in directory.iterdir():
        match = _DAY_FILE.match(path.name)
        if match:
            try:
                found = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if found < day:
                days.append(found)
    return snapshot_path(directory, max(days)) if days else None


def load_snapshot(path: Path) -> dict[str, Any]:
    """A saved snapshot, amounts as `Decimal`; `InvalidSnapshotError` if it is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except FileNotFoundError as exc:
        raise InvalidSnapshotError(path, "no existe") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidSnapshotError(path, "no es JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise InvalidSnapshotError(path, f'falta "schema": "{SCHEMA}"')
    try:
        if snapshot_day(data) is None:
            raise ValueError
    except ValueError as exc:
        raise InvalidSnapshotError(path, 'falta o no vale "as_of"') from exc
    return data


def save_snapshot(directory: Path, day: date, snapshot: Mapping[str, Any]) -> Path:
    """Write `directory/AAAA-MM-DD.json` (0600, dir 0700) atomically, replacing that day's one."""
    ensure_private_dir(directory)
    path = snapshot_path(directory, day)
    with locked(directory / LOCK_NAME):
        write_atomic(path, dumps(snapshot) + "\n", prefix=".snapshot-")
    return path
