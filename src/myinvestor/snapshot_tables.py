"""`summary`: the tables shown for a snapshot document (pure: document in, `(title, rows)` out).

Sections that failed (None) or are empty are left out; `summary` prints the errors apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from myinvestor.cli_accounts import MOVEMENT_TABLE_COLUMNS
from myinvestor.cli_upcoming import _table_row

Row = dict[str, Any]
Table = tuple[str, list[Row]]

TOTAL_LABEL = "TOTAL"
POSITION_COLUMNS = ("producto", "id", "nombre", "valor", "invertido", "ganancia", "rentab", "fecha_valor")
DELTA_COLUMNS = ("producto", "id", "nombre", "valor", "aportado", "mercado")
HOLDING_COLUMNS = ("cuenta", "nombre", "participaciones", "vl", "fecha_vl", "invertido", "valor", "rentab", "peso")
PLAN_COLUMNS = ("cuenta", "nombre", "participaciones", "vl", "fecha_vl", "invertido", "valor", "rentab")
ORDER_COLUMNS = ("fecha", "cuenta", "ref", "nombre", "operacion", "estado", "importe")


def summary_tables(doc: Mapping[str, Any]) -> list[Table]:
    pending = doc.get("pending") or {}
    new = doc.get("new") or {}
    tables = [
        (f"Posición a {_moment(doc.get('as_of'))}", position_rows(doc)),
        (f"Variación desde {_moment((doc.get('delta') or {}).get('since'))}", delta_rows(doc.get("delta"))),
        (
            "Cartera",
            _pick([p for s in doc.get("securities") or [] for p in s.get("posiciones") or []], HOLDING_COLUMNS),
        ),
        (
            "Planes de pensiones",
            _pick([p for s in doc.get("pensions") or [] for p in s.get("planes") or []], PLAN_COLUMNS),
        ),
        ("Órdenes en curso", _pick(pending.get("orders") or [], ORDER_COLUMNS)),
        ("Retenciones", pending.get("holds") or []),
        ("Lo que viene", [_table_row(r) for r in doc.get("upcoming") or []]),
        ("Movimientos nuevos", _pick(new.get("movements") or [], MOVEMENT_TABLE_COLUMNS)),
        ("Órdenes nuevas o con cambio de estado", _pick(new.get("orders") or [], ORDER_COLUMNS)),
        ("Órdenes de planes nuevas o con cambio de estado", _pick(new.get("pension_orders") or [], ORDER_COLUMNS)),
    ]
    return [(title, rows) for title, rows in tables if rows]


def position_rows(doc: Mapping[str, Any]) -> list[Row]:
    """Like `position`: a row per cash, securities and pension account, and the total."""
    cash = [
        {
            "producto": "efectivo",
            "id": r["id"],
            "nombre": r.get("alias"),
            "valor": r.get("saldo"),
            "invertido": None,
            "ganancia": None,
            "rentab": None,
            "fecha_valor": r.get("fecha_valor"),
        }
        for r in doc.get("cash") or []
    ]
    others = _pick([*(doc.get("securities") or []), *(doc.get("pensions") or [])], POSITION_COLUMNS)
    rows = cash + others
    if rows:
        rows.append({**dict.fromkeys(POSITION_COLUMNS), "producto": TOTAL_LABEL, "valor": doc.get("total")})
    return rows


def delta_rows(delta: Mapping[str, Any] | None) -> list[Row]:
    if not delta:
        return []
    total = {
        **dict.fromkeys(DELTA_COLUMNS),
        "producto": TOTAL_LABEL,
        "valor": delta.get("total"),
        "aportado": delta.get("aportado"),
        "mercado": delta.get("mercado"),
    }
    return [*_pick(delta.get("by_product") or [], DELTA_COLUMNS), total]


def _pick(rows: list[Mapping[str, Any]], columns: tuple[str, ...]) -> list[Row]:
    return [{c: r.get(c) for c in columns} for r in rows]


def _moment(value: Any) -> str:
    """`2026-11-04T09:00:05+01:00` → `2026-11-04 09:00`."""
    if not isinstance(value, str):
        return "?"
    try:
        return f"{datetime.fromisoformat(value):%Y-%m-%d %H:%M}"
    except ValueError:
        return value
