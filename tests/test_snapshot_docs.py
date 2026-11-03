"""The example of docs/snapshot.md follows the `snapshot` contract and its sign conventions."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from myinvestor.json_contract import contract_for
from myinvestor.snapshot import SCHEMA
from myinvestor.upcoming import Status
from tests.test_json_contract import check_data

DOC = Path(__file__).resolve().parent.parent / "docs" / "snapshot.md"
JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def documented_example() -> dict[str, Any]:
    """The first JSON block of the schema section."""
    schema_section = DOC.read_text(encoding="utf-8").split(f"## Esquema (`{SCHEMA}`)", 1)[1]
    match = JSON_BLOCK.search(schema_section)
    assert match, f"docs/snapshot.md no tiene un bloque JSON en «Esquema ({SCHEMA})»"
    example: dict[str, Any] = json.loads(match.group(1), parse_float=Decimal)
    return example


def test_the_documented_example_follows_the_snapshot_contract() -> None:
    contract = contract_for("snapshot")
    example = documented_example()

    assert example["schema"] == SCHEMA == contract.schema_id
    check_data(example, contract, contract.variants[0], set(), set())


def test_the_documented_example_shows_holds_as_money_going_out() -> None:
    """`pending.holds[].importe` is negative, like a charge in `movements` and the hold rows of `upcoming`."""
    example = documented_example()
    holds = example["pending"]["holds"]

    assert holds and all(h["importe"] < 0 for h in holds)
    held = [r for r in example["upcoming"] if r["tipo"] == "retencion"]
    assert all(r["importe"] < 0 for r in held)


def test_the_documented_upcoming_shows_both_estados() -> None:
    estados = {r["estado"] for r in documented_example()["upcoming"]}
    assert estados == {s.value for s in Status}
