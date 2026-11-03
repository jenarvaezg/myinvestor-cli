"""`schema`: the JSON Schema of a command's `-f json` output, generated from the contract table."""

from __future__ import annotations

from typing import Annotated

import typer

from myinvestor.cli_common import JsonOnlyFormatOpt, app, only_json
from myinvestor.errors import InputError
from myinvestor.json_contract import contract_for
from myinvestor.json_output import dumps
from myinvestor.json_schema import json_schema, schema_index
from myinvestor.output import OutputFormat


@app.command()
def schema(
    command: Annotated[
        str | None, typer.Argument(help="Orden (movements, state-show…) o `error`. Sin ella: la lista de esquemas.")
    ] = None,
    envelope: Annotated[bool, typer.Option("--envelope", help="El esquema de la salida con --json-envelope.")] = False,
    fmt: JsonOnlyFormatOpt = OutputFormat.json,
) -> None:
    """JSON Schema de la salida con -f json de una orden (ver docs/json.md).

    Sin orden, `{orden: esquema}` de todas. Nunca va en el envoltorio de --json-envelope.
    """
    only_json(fmt, "schema")
    if command is None:
        print(dumps(schema_index()))
        return
    try:
        contract = contract_for(command)
    except KeyError as exc:
        known = ", ".join(schema_index())
        raise InputError(f"No hay esquema para {command!r}. Órdenes: {known} (y `error`).") from exc
    print(dumps(json_schema(contract, envelope=envelope)))
