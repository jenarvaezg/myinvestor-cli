"""The JSON contract table as JSON Schema (`myinvestor schema ORDEN`) and as the tables of docs/json.md."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from myinvestor.json_contract import CONTRACTS, ENVELOPE, ERROR_LINE, Contract, Field, JsonType, Shape, Variant

DRAFT = "https://json-schema.org/draft/2020-12/schema"

_BASE: dict[JsonType, dict[str, Any]] = {
    JsonType.string: {"type": "string"},
    JsonType.amount: {"type": "number", "description": "euros, con 2 decimales"},
    JsonType.number: {"type": "number"},
    JsonType.integer: {"type": "integer"},
    JsonType.boolean: {"type": "boolean"},
    JsonType.date: {"type": "string", "format": "date"},
    JsonType.datetime: {"type": "string", "format": "date-time"},
    JsonType.object: {"type": "object"},
    JsonType.array: {"type": "array"},
    JsonType.any: {},
}

TYPE_LABELS = {
    JsonType.string: "string",
    JsonType.amount: "number (€, 2 decimales)",
    JsonType.number: "number (exacto)",
    JsonType.integer: "integer",
    JsonType.boolean: "boolean",
    JsonType.date: "date `AAAA-MM-DD`",
    JsonType.datetime: "datetime ISO 8601 con zona",
    JsonType.object: "object",
    JsonType.array: "array de objetos",
    JsonType.any: "lista u objeto",
}


# ---- JSON Schema -----------------------------------------------------------------------------


def _field_schema(field: Field) -> dict[str, Any]:
    if field.type is JsonType.array and field.fields:
        schema = {"type": "array", "items": _object_schema(field.fields)}
    elif field.type is JsonType.object and field.fields:
        schema = _object_schema(field.fields)
    else:
        schema = dict(_BASE[field.type])
    notes = [n for n in (schema.get("description"), field.note) if n]
    if field.only_with:
        notes.append(f"solo con {field.only_with}")
    if notes:
        schema["description"] = "; ".join(notes)
    if field.nullable and "type" in schema:
        schema["type"] = [schema["type"], "null"]
    return schema


def _object_schema(fields: tuple[Field, ...]) -> dict[str, Any]:
    """Unknown keys are allowed: adding a key is a compatible change."""
    return {
        "type": "object",
        "properties": {f.name: _field_schema(f) for f in fields},
        "required": [f.name for f in fields if f.only_with is None],
        "additionalProperties": True,
    }


def _variant_schema(variant: Variant) -> dict[str, Any]:
    if variant.shape is Shape.any:
        schema: dict[str, Any] = {}
    elif variant.shape is Shape.rows:
        schema = {"type": "array", "items": _object_schema(variant.fields)}
    else:
        schema = _object_schema(variant.fields)
    return {"title": variant.label, **schema} if variant.label else schema


def data_schema(contract: Contract) -> dict[str, Any]:
    if len(contract.variants) == 1:
        return _variant_schema(contract.variants[0])
    return {"oneOf": [_variant_schema(v) for v in contract.variants]}


def json_schema(contract: Contract, *, envelope: bool = False) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) of a command's output, bare or inside `--json-envelope`."""
    head: dict[str, Any] = {"$schema": DRAFT, "title": contract.schema_id}
    if contract.note:
        head["description"] = contract.note
    if not envelope or not contract.enveloped:
        return {**head, **data_schema(contract)}
    wrapper = _object_schema(ENVELOPE)
    wrapper["properties"] = {
        **wrapper["properties"],
        "schema": {"const": contract.schema_id},
        "data": data_schema(contract),
    }
    return {**head, **wrapper}


def schema_index() -> dict[str, str]:
    """`{orden: esquema}` of every command with JSON output."""
    return {c.command: c.schema_id for c in CONTRACTS}


# ---- docs/json.md ----------------------------------------------------------------------------


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _lines(fields: tuple[Field, ...], prefix: str = "") -> Iterator[str]:
    for f in fields:
        notes = [f.note] if f.note else []
        if f.only_with:
            notes.insert(0, f"solo con `{f.only_with}`")
        stable = "sí" if f.stable else "no (informativo)"
        null = "sí" if f.nullable else "no"
        yield f"| `{prefix}{f.name}` | {TYPE_LABELS[f.type]} | {null} | {stable} | {_cell('; '.join(notes))} |"
        yield from _lines(f.fields, f"{prefix}{f.name}[]." if f.type is JsonType.array else f"{prefix}{f.name}.")


def _shape_line(variant: Variant) -> str:
    what = {Shape.rows: "lista de objetos", Shape.object: "un objeto", Shape.any: "lo que responda MyInvestor"}
    return f"{f'Con `{variant.label}`' if variant.label else 'Salida'}: {what[variant.shape]}."


def _section(contract: Contract) -> Iterator[str]:
    yield f"### `{contract.command}` — `{contract.schema_id}`"
    yield ""
    if contract.note:
        yield contract.note
        yield ""
    for i, variant in enumerate(contract.variants):
        yield _shape_line(variant)
        yield ""
        same = next((v for v in contract.variants[:i] if v.fields == variant.fields), None)
        if same is not None:
            yield f"Las mismas claves que {f'con `{same.label}`' if same.label else 'la salida por defecto'}."
            yield ""
        elif variant.fields:
            yield "| Clave | Tipo | `null` | Estable | Notas |"
            yield "|---|---|---|---|---|"
            yield from _lines(variant.fields)
            yield ""


def markdown_reference() -> str:
    """The generated part of docs/json.md: one section per command, then the error line."""
    lines = [line for c in (*CONTRACTS, ERROR_LINE) for line in _section(c)]
    return "\n".join(lines).rstrip() + "\n"
