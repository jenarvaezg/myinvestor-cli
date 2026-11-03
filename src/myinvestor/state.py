"""Local state of `--new`: what `movements`, `orders` and `contributions` have already shown.

The model is pure (frozen dataclasses, functions that return new values); `StateFile` is the only IO:
a JSON file (0600, dir 0700) replaced atomically under a `flock`, like the session store.

- Movements: per cash account, the last day asked for (`last_day`) and the ids already shown, each with
  its operation date. The next run asks from `last_day - MOVEMENT_MARGIN_DAYS` (movements can show up a
  few days late) and only emits the ids it has not seen. Ids older than that start are never asked for
  again, so they are pruned.
- Orders, pension orders and contributions: the last value shown per key (`reference → estado`,
  `contributionId → {estado, proxima}`); an item is new when its key is unknown or its value changed.

`--no-mark` puts what would have been marked in `pending` instead; `state mark` confirms it later, so a
routine that fails halfway loses nothing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from myinvestor.atomicfile import locked, write_atomic
from myinvestor.errors import MyInvestorError

STATE_VERSION = 1
# Days before the last day asked for that the next `movements --new` asks again.
MOVEMENT_MARGIN_DAYS = 3


class StateKind(StrEnum):
    movements = "movements"
    orders = "orders"
    pension_orders = "pension_orders"
    contributions = "contributions"


SNAPSHOT_KINDS = (StateKind.orders, StateKind.pension_orders, StateKind.contributions)


class CorruptStateError(MyInvestorError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            f"El fichero de estado {path} está corrupto. Bórralo o ejecuta `myinvestor state reset` "
            "(la próxima vez con --new todo saldrá como nuevo)."
        )


@dataclass(frozen=True)
class AccountCursor:
    """What `movements --new` knows of one cash account."""

    last_day: date
    seen: Mapping[str, date] = field(default_factory=dict)  # movement id → operation date

    def next_start(self, margin: int = MOVEMENT_MARGIN_DAYS) -> date:
        return self.last_day - timedelta(days=margin)


@dataclass(frozen=True)
class Marks:
    """Everything shown: movement cursors per account uuid and, per snapshot kind, the last value per key."""

    movements: Mapping[str, AccountCursor] = field(default_factory=dict)
    snapshots: Mapping[StateKind, Mapping[str, Any]] = field(default_factory=dict)

    def snapshot(self, kind: StateKind) -> Mapping[str, Any]:
        return self.snapshots.get(kind, {})

    def is_empty(self) -> bool:
        return not self.movements and not any(self.snapshots.values())

    def only(self, kind: StateKind | None) -> Marks:
        if kind is None:
            return self
        if kind is StateKind.movements:
            return Marks(movements=self.movements)
        return Marks(snapshots={kind: self.snapshot(kind)} if kind in self.snapshots else {})

    def without(self, kind: StateKind | None) -> Marks:
        if kind is None:
            return Marks()
        if kind is StateKind.movements:
            return Marks(snapshots=self.snapshots)
        return Marks(movements=self.movements, snapshots={k: v for k, v in self.snapshots.items() if k is not kind})


@dataclass(frozen=True)
class SeenState:
    marked: Marks = field(default_factory=Marks)
    pending: Marks = field(default_factory=Marks)  # shown with --no-mark, waiting for `state mark`

    def mark(self, marks: Marks) -> SeenState:
        return SeenState(marked=merge_marks(self.marked, marks), pending=self.pending)

    def defer(self, marks: Marks) -> SeenState:
        return SeenState(marked=self.marked, pending=merge_marks(self.pending, marks))

    def commit(self, kind: StateKind | None = None) -> SeenState:
        """Confirm what is pending (of one kind, or all)."""
        return SeenState(marked=merge_marks(self.marked, self.pending.only(kind)), pending=self.pending.without(kind))

    def reset(self, kind: StateKind | None = None) -> SeenState:
        return SeenState(marked=self.marked.without(kind), pending=self.pending.without(kind))


# ---- pure operations ------------------------------------------------------------------------


def merge_cursor(old: AccountCursor | None, new: AccountCursor, margin: int = MOVEMENT_MARGIN_DAYS) -> AccountCursor:
    """Union of the seen ids, the later `last_day`, and ids older than the next start pruned."""
    last_day = max(old.last_day, new.last_day) if old else new.last_day
    cutoff = AccountCursor(last_day).next_start(margin)
    seen = {**(old.seen if old else {}), **new.seen}
    return AccountCursor(last_day, {i: d for i, d in seen.items() if d >= cutoff})


def merge_marks(old: Marks, new: Marks) -> Marks:
    movements = {**old.movements, **{u: merge_cursor(old.movements.get(u), c) for u, c in new.movements.items()}}
    kinds = set(old.snapshots) | set(new.snapshots)
    snapshots = {k: {**old.snapshot(k), **new.snapshot(k)} for k in kinds}
    return Marks(movements=movements, snapshots=snapshots)


def unseen_movements(rows: Iterable[dict[str, Any]], cursors: Mapping[str, AccountCursor]) -> list[dict[str, Any]]:
    """Movement rows (`cuenta_uuid`, `id`) not shown yet."""
    return [r for r in rows if r["cuenta_uuid"] not in cursors or r["id"] not in cursors[r["cuenta_uuid"]].seen]


def movement_marks(rows: Iterable[dict[str, Any]], account_uuids: Iterable[str], last_day: date) -> Marks:
    """Marks for every account asked for (also those without movements), up to `last_day`."""
    seen: dict[str, dict[str, date]] = {u: {} for u in account_uuids}
    for r in rows:
        seen.setdefault(r["cuenta_uuid"], {})[r["id"]] = date.fromisoformat(r["fecha"])
    return Marks(movements={u: AccountCursor(last_day, ids) for u, ids in seen.items()})


def changed(items: Iterable[tuple[str, Any]], known: Mapping[str, Any]) -> list[str]:
    """Keys whose value is new or differs from the one shown last time."""
    return [key for key, value in items if key not in known or known[key] != value]


def snapshot_marks(kind: StateKind, items: Iterable[tuple[str, Any]]) -> Marks:
    return Marks(snapshots={kind: dict(items)})


# ---- (de)serialization ------------------------------------------------------------------------


def _marks_to_dict(marks: Marks) -> dict[str, Any]:
    data: dict[str, Any] = {
        StateKind.movements.value: {
            uuid: {"last_day": c.last_day.isoformat(), "seen": {i: d.isoformat() for i, d in sorted(c.seen.items())}}
            for uuid, c in sorted(marks.movements.items())
        }
    }
    for kind in SNAPSHOT_KINDS:
        data[kind.value] = dict(sorted(marks.snapshot(kind).items()))
    return data


def _marks_from_dict(raw: Any) -> Marks:
    """Raises ValueError/TypeError/KeyError/AttributeError on malformed data."""
    movements = {
        str(uuid): AccountCursor(
            date.fromisoformat(c["last_day"]), {str(i): date.fromisoformat(d) for i, d in c.get("seen", {}).items()}
        )
        for uuid, c in raw.get(StateKind.movements.value, {}).items()
    }
    snapshots: dict[StateKind, Mapping[str, Any]] = {}
    for kind in SNAPSHOT_KINDS:
        values = raw.get(kind.value, {})
        if not isinstance(values, dict):
            raise TypeError(kind.value)
        if values:
            snapshots[kind] = dict(values)
    return Marks(movements=movements, snapshots=snapshots)


def state_to_dict(state: SeenState) -> dict[str, Any]:
    return {"version": STATE_VERSION, **_marks_to_dict(state.marked), "pending": _marks_to_dict(state.pending)}


def state_from_dict(raw: Any) -> SeenState:
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        raise ValueError("unknown state version")
    return SeenState(marked=_marks_from_dict(raw), pending=_marks_from_dict(raw.get("pending", {})))


# ---- file ----------------------------------------------------------------------------------------


def default_state_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state_home) / "myinvestor" / "seen.json"


class StateFile:
    """`seen.json`: read freely, changed only under its lock (read-modify-write)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SeenState:
        if not self.path.exists():
            return SeenState()
        try:
            return state_from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CorruptStateError(self.path) from exc

    def update(self, change: Callable[[SeenState], SeenState]) -> SeenState:
        with locked(self.path.with_suffix(".lock")):
            new = change(self.load())
            write_atomic(self.path, json.dumps(state_to_dict(new), indent=2, ensure_ascii=False), prefix=".seen-")
        return new

    def clear(self) -> None:
        """Forget everything, without reading the old file (it may be corrupt)."""
        with locked(self.path.with_suffix(".lock")):
            write_atomic(self.path, json.dumps(state_to_dict(SeenState()), indent=2), prefix=".seen-")
