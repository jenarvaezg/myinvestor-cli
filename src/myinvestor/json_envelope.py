"""`--json-envelope`: wrap a command's JSON as `{"schema", "cli_version", "as_of", "data"}`.

The CLI callbacks record which command is running (`movements`, `state show`); the schema name
comes from it through the contract table, so no command names its own schema.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from myinvestor.json_contract import contract_for


@dataclass(frozen=True)
class EnvelopeSettings:
    enabled: bool = False
    command: str = ""
    cli_version: str = ""
    as_of: str = ""


_settings = EnvelopeSettings()


def configure(*, enabled: bool, command: str = "", cli_version: str = "", as_of: str = "") -> None:
    global _settings
    _settings = EnvelopeSettings(enabled=enabled, command=command, cli_version=cli_version, as_of=as_of)


def set_command(command: str) -> None:
    """The full name of the running command, once a command group knows its subcommand."""
    global _settings
    _settings = replace(_settings, command=command)


def wrap(data: Any) -> Any:
    """`data` inside the envelope when `--json-envelope` is on; as is otherwise."""
    if not _settings.enabled:
        return data
    try:
        contract = contract_for(_settings.command)
    except KeyError as exc:
        raise RuntimeError(f"`{_settings.command}` has no entry in json_contract.CONTRACTS") from exc
    if not contract.enveloped:
        return data
    return {"schema": contract.schema_id, "cli_version": _settings.cli_version, "as_of": _settings.as_of, "data": data}
