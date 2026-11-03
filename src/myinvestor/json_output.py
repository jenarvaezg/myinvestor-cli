"""JSON output with `Decimal` written as an exact number literal.

The stdlib encoder can only emit numbers through `float`, which would turn `-50.00` into `-50.0`
and could round long VL/participation values. Layout matches `json.dumps(indent=2, ensure_ascii=False)`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

INDENT = "  "


def dumps(value: Any) -> str:
    """Serialize to indented JSON; `Decimal` becomes a number, unknown types (dates…) their `str`."""
    return _encode(value, 0)


def dumps_line(value: Any) -> str:
    """Serialize to JSON on a single line (JSON Lines), with the same rules as `dumps`."""
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{dumps_line(str(k))}: {dumps_line(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(dumps_line(item) for item in value) + "]"
    return _encode(value, 0)


def decimal_literal(value: Decimal) -> str:
    """Fixed-point text of a finite Decimal (`1E+3` → `1000`), valid as a JSON number."""
    if not value.is_finite():
        raise ValueError(f"{value} is not representable as a JSON number")
    return format(value, "f")


def _encode(value: Any, depth: int) -> str:
    if isinstance(value, Decimal):
        return decimal_literal(value)
    if value is None or isinstance(value, bool | int | float | str):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, Mapping):
        items = [f"{_encode(str(key), 0)}: {_encode(item, depth + 1)}" for key, item in value.items()]
        return _container("{", items, "}", depth)
    if isinstance(value, list | tuple):
        return _container("[", [_encode(item, depth + 1) for item in value], "]", depth)
    return _encode(str(value), depth)


def _container(opening: str, items: list[str], closing: str, depth: int) -> str:
    if not items:
        return opening + closing
    inner = "\n" + INDENT * (depth + 1)
    return opening + inner + ("," + inner).join(items) + "\n" + INDENT * depth + closing
