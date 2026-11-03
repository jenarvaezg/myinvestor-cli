"""`export`: an append-only local history (CSV or JSON Lines) and its `manifest.json`.

Pure parts: row keys, cell text, the coverage of each cash account (covered intervals and holes) and
the balance check of each span. IO: `ExportDir`, which appends rows atomically (old bytes copied as
they are, new rows after them, then one rename) under a `flock`, so old rows are never rewritten.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from myinvestor.atomicfile import ensure_private_dir, locked, write_atomic
from myinvestor.errors import MyInvestorError
from myinvestor.json_output import decimal_literal, dumps, dumps_line
from myinvestor.output import normalize_row

MANIFEST_VERSION = 1
MANIFEST = "manifest.json"
# Days before the last covered day that the next export asks again (movements can show up late).
EXPORT_MARGIN_DAYS = 3

Row = dict[str, Any]
Key = tuple[str, ...]


class ExportFormat(StrEnum):
    csv = "csv"
    json = "json"  # JSON Lines: one object per line, so rows can be appended

    @property
    def suffix(self) -> str:
        return ".csv" if self is ExportFormat.csv else ".jsonl"


class CorruptExportError(MyInvestorError):
    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"No se puede leer {path} ({detail}): no se toca nada. Revísalo o exporta a otro directorio.")


def cell(value: Any) -> str:
    """Text of a value as it is written (and read back): what row keys compare."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return decimal_literal(value)
    return str(value)


def _day_of(moment: str) -> str:
    return moment[:10]


@dataclass(frozen=True)
class Dataset:
    """A file of the export and what identifies one of its rows."""

    name: str
    key: Callable[[Mapping[str, str]], Key]


DATASETS = (
    Dataset("movements", lambda r: (r["id"],)),
    # One row per state seen of each order or contribution: the last one is the current one.
    Dataset("orders", lambda r: (r["ref"], r["estado"])),
    Dataset("pension_orders", lambda r: (r["ref"], r["estado"])),
    Dataset("contributions", lambda r: (r["id"], r["estado"], r["proxima"], r["importe"], r["frecuencia"])),
    # Snapshots: one per day.
    Dataset("holdings", lambda r: (_day_of(r["consultado_en"]), r["cuenta"], r["isin"] or r["nombre"])),
    Dataset("accounts", lambda r: (_day_of(r["consultado_en"]), r["id"])),
)
DATASET = {d.name: d for d in DATASETS}


def row_key(dataset: Dataset, row: Mapping[str, Any]) -> Key:
    return dataset.key({k: cell(v) for k, v in row.items()})


def fresh_rows(dataset: Dataset, rows: Iterable[Row], known: set[Key]) -> list[Row]:
    """Rows whose key is not in `known` nor repeated earlier in `rows`."""
    seen = set(known)
    out: list[Row] = []
    for row in rows:
        key = row_key(dataset, row)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---- coverage of cash-account movements -------------------------------------------------------


@dataclass(frozen=True, order=True)
class Interval:
    start: date
    end: date

    def to_dict(self) -> dict[str, str]:
        return {"desde": self.start.isoformat(), "hasta": self.end.isoformat()}


class HoleReason(StrEnum):
    signature = "firma"  # needs a second factor that was not given: retried on the next run
    mismatch = "descuadre"  # startBalance + Σ amounts != endBalance: movements may be missing


@dataclass(frozen=True, order=True)
class Hole:
    interval: Interval
    reason: HoleReason

    def to_dict(self) -> dict[str, str]:
        return {**self.interval.to_dict(), "motivo": self.reason.value}


@dataclass(frozen=True)
class SpanCheck:
    """Balance check of one fetched span: `ok` is None when MyInvestor gave no balances."""

    interval: Interval
    start_balance: Decimal | None
    end_balance: Decimal | None
    total: Decimal
    movements: int

    @property
    def ok(self) -> bool | None:
        if self.start_balance is None or self.end_balance is None:
            return None
        return self.start_balance + self.total == self.end_balance

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.interval.to_dict(),
            "movimientos": self.movements,
            "saldo_inicial": self.start_balance,
            "suma_importes": self.total,
            "saldo_final": self.end_balance,
            "cuadra": self.ok,
        }


def check_span(interval: Interval, start: Decimal | None, end: Decimal | None, amounts: Sequence[Decimal]) -> SpanCheck:
    return SpanCheck(interval, start, end, sum(amounts, Decimal(0)), len(amounts))


@dataclass(frozen=True)
class Coverage:
    """What the export holds of one cash account."""

    covered: tuple[Interval, ...] = ()
    holes: tuple[Hole, ...] = ()
    last_checks: tuple[SpanCheck, ...] = field(default=(), compare=False)


def merge_intervals(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Union of whole-day intervals, overlapping or adjacent ones joined."""
    merged: list[Interval] = []
    for iv in sorted(intervals):
        if merged and iv.start <= merged[-1].end + timedelta(days=1):
            merged[-1] = Interval(merged[-1].start, max(merged[-1].end, iv.end))
        else:
            merged.append(iv)
    return tuple(merged)


def _minus(iv: Interval, cut: Interval) -> list[Interval]:
    if cut.end < iv.start or cut.start > iv.end:
        return [iv]
    parts = []
    if cut.start > iv.start:
        parts.append(Interval(iv.start, cut.start - timedelta(days=1)))
    if cut.end < iv.end:
        parts.append(Interval(cut.end + timedelta(days=1), iv.end))
    return parts


def subtract(holes: Iterable[Hole], covered: Iterable[Interval]) -> list[Hole]:
    """The parts of `holes` outside `covered`."""
    cuts = list(covered)
    out: list[Hole] = []
    for hole in holes:
        parts = [hole.interval]
        for cut in cuts:
            parts = [p for part in parts for p in _minus(part, cut)]
        out += [Hole(p, hole.reason) for p in parts]
    return out


def update_coverage(old: Coverage | None, checks: Sequence[SpanCheck], gaps: Sequence[Interval]) -> Coverage:
    """Add a run: spans that balance (or can't be checked) become covered; missing and mismatched ones holes."""
    old = old or Coverage()
    good = [c.interval for c in checks if c.ok is not False]
    covered = merge_intervals([*old.covered, *good])
    signature = [h for h in old.holes if h.reason is HoleReason.signature]
    signature += [Hole(g, HoleReason.signature) for g in gaps]
    mismatch = [h for h in old.holes if h.reason is HoleReason.mismatch]
    mismatch += [Hole(c.interval, HoleReason.mismatch) for c in checks if c.ok is False]
    holes = subtract(signature, covered) + subtract(mismatch, good)
    return Coverage(covered, tuple(sorted(set(holes))), tuple(checks))


def next_start(old: Coverage | None, since: date | None, default: date, margin: int = EXPORT_MARGIN_DAYS) -> date:
    """Where the next export of an account starts.

    First time: `--since` or `default`. Then the last covered day minus the margin, or earlier to retry
    a hole that needed a signature, or to reach back to `--since`.
    """
    if old is None or not old.covered:
        return since or default
    candidates = [old.covered[-1].end - timedelta(days=margin)]
    candidates += [h.interval.start for h in old.holes if h.reason is HoleReason.signature]
    if since:
        candidates.append(since)
    return min(candidates)


def coverage_to_dict(cov: Coverage, account: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **account,
        "cubierto": [iv.to_dict() for iv in cov.covered],
        "huecos": [h.to_dict() for h in cov.holes],
        "ultimos_tramos": [c.to_dict() for c in cov.last_checks],
    }


def _interval(raw: Mapping[str, Any]) -> Interval:
    return Interval(date.fromisoformat(raw["desde"]), date.fromisoformat(raw["hasta"]))


def coverage_from_dict(raw: Mapping[str, Any]) -> Coverage:
    """Raises ValueError/TypeError/KeyError on malformed data."""
    return Coverage(
        covered=tuple(_interval(c) for c in raw.get("cubierto", [])),
        holes=tuple(Hole(_interval(h), HoleReason(h["motivo"])) for h in raw.get("huecos", [])),
    )


# ---- files ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Appended:
    file: Path
    new: int
    total: int
    dropped_columns: tuple[str, ...] = ()  # in the new rows but not in the existing CSV header


class ExportDir:
    """The export directory (created 0700): one file per dataset (0600) and `manifest.json`."""

    def __init__(self, path: Path, fmt: ExportFormat) -> None:
        self.path = path
        self.fmt = fmt

    def locked(self) -> contextlib.AbstractContextManager[None]:
        ensure_private_dir(self.path)
        return locked(self.path / ".export.lock")

    def file(self, dataset: Dataset) -> Path:
        return self.path / f"{dataset.name}{self.fmt.suffix}"

    def _text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeDecodeError) as exc:
            raise CorruptExportError(path, type(exc).__name__) from exc

    def _existing(self, path: Path, text: str) -> tuple[list[str] | None, list[dict[str, str]]]:
        """Header (CSV) and rows as text."""
        if not text:
            return None, []
        if self.fmt is ExportFormat.csv:
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            return list(reader.fieldnames or []), rows
        try:
            parsed = [json.loads(line, parse_float=Decimal) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise CorruptExportError(path, f"línea {exc.lineno}") from exc
        return None, [{k: cell(v) for k, v in obj.items()} for obj in parsed]

    def append(self, dataset: Dataset, rows: Sequence[Row]) -> Appended:
        path = self.file(dataset)
        text = self._text(path)
        header, existing = self._existing(path, text)
        try:
            known = {dataset.key(r) for r in existing}
        except KeyError as exc:
            raise CorruptExportError(path, f"falta la columna {exc}") from exc
        new = fresh_rows(dataset, [normalize_row(r) for r in rows], known)
        appended = Appended(path, len(new), len(existing) + len(new))
        if not new:
            return appended
        if text and not text.endswith("\n"):
            text += "\n"
        if self.fmt is ExportFormat.json:
            text += "".join(dumps_line(r) + "\n" for r in new)
            write_atomic(path, text, prefix=f".{dataset.name}-")
            return appended
        columns = header or list(new[0])
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        if header is None:
            writer.writeheader()
        writer.writerows({k: cell(v) for k, v in r.items()} for r in new)
        write_atomic(path, text + buffer.getvalue(), prefix=f".{dataset.name}-")
        dropped = tuple(dict.fromkeys(k for r in new for k in r if k not in columns))
        return Appended(path, appended.new, appended.total, dropped)

    def load_manifest(self) -> dict[str, Any] | None:
        path = self.path / MANIFEST
        text = self._text(path)
        if not text:
            return None
        try:
            data = json.loads(text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise CorruptExportError(path, "JSON no válido") from exc
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            raise CorruptExportError(path, "versión desconocida")
        return data

    def write_manifest(self, manifest: Mapping[str, Any]) -> None:
        write_atomic(self.path / MANIFEST, dumps(manifest) + "\n", prefix=".manifest-")
