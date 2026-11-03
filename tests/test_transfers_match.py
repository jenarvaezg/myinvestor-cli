"""`transfers_match`: parsing `operationRef` and pairing both legs of internal transfers (synthetic data)."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from myinvestor.transfers_match import (
    CHARGE_TYPE,
    CREDIT_TYPE,
    REF_LENGTH,
    Leg,
    MatchKind,
    OperationRef,
    match_transfers,
    parse_operation_ref,
    transfer_rows,
    with_transfer_fields,
)

DAY = date(2026, 11, 2)
INTERNAL = "Transferencia desde MyInvestor"
ACCOUNT_A = "000000000000000001"
ACCOUNT_B = "000000000000000002"
KEY = "120261102700001"
BLOCK = "3000000001"


def op_ref(
    op_id: str,
    counter: str,
    *,
    account: str = ACCOUNT_A,
    gap: str = "",
    block: str = BLOCK,
    booked: str = "20261102",
    check: str = "9",
    entity: str = "ENTIDAD GENERICA",
) -> str:
    """A synthetic `operationRef` with the observed 93-character internal-transfer layout."""
    return f"{account:>18}{gap:<10}{op_id:<15} {entity:<24}{block}{counter}{booked}{check}"


def sepa_ref(counter: str, *, account: str = ACCOUNT_B) -> str:
    """Same length, but digits in 18-27 and free text where the operation id goes."""
    return f"{account}1234567890{'REMESA:SINTET':<15} {'ENTIDAD GENERICA':<24}{'0' * 10}{counter}202611023"


def leg(
    leg_id: str,
    account: str,
    amount: str,
    *,
    day: date = DAY,
    concept: str | None = None,
    ref: str | None = None,
    reference: str | None = None,
    kind: str | None = None,
) -> Leg:
    if kind is None:
        kind = CHARGE_TYPE if amount.startswith("-") else CREDIT_TYPE
    return Leg(
        id=leg_id,
        account=account,
        day=day,
        amount=Decimal(amount),
        concept=concept,
        operation_ref=ref,
        reference=reference,
        kind=kind,
    )


# ---- operationRef ----------------------------------------------------------------


def test_synthetic_refs_have_the_real_length() -> None:
    assert len(op_ref(KEY, "000259")) == len(sepa_ref("000001")) == REF_LENGTH == 93


def test_parses_the_fixed_width_fields() -> None:
    raw = op_ref("120261102758295", "000259", check="9")

    assert parse_operation_ref(raw, "000259") == OperationRef(
        account=ACCOUNT_A,
        operation_id="120261102758295",
        operation_day=date(2026, 11, 2),
        entity="ENTIDAD GENERICA",
        block=BLOCK,
        counter="000259",
        booked_day=date(2026, 11, 2),
        check="9",
    )


def test_both_legs_share_the_operation_id_and_the_block() -> None:
    out = parse_operation_ref(op_ref(KEY, "000259", account=ACCOUNT_A, check="9"))
    into = parse_operation_ref(op_ref(KEY, "000032", account=ACCOUNT_B, check="7"))

    assert out is not None and into is not None
    assert out.pairing_key == into.pairing_key == (KEY, BLOCK)
    assert (out.counter, into.counter) == ("000259", "000032")


def test_trailing_blanks_are_tolerated_and_the_reference_may_be_unpadded() -> None:
    assert parse_operation_ref(op_ref(KEY, "000259") + "   ", "259") is not None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "0000000000OPREF0001",
        op_ref(KEY, "000259")[:-1],  # one character short
        op_ref(KEY, "000259") + "X",  # something after the last digit
        op_ref(KEY, "000259", gap="1234567890"),  # digits in 18-27: SEPA, card, instant transfer
        sepa_ref("000259"),  # free text where the operation id goes
        op_ref("1202611 2700001", "000259"),  # blank inside the operation id
        op_ref("12026110270000A", "000259"),  # letter in the operation id
        op_ref("120261340700001", "000259"),  # month 13 in the operation id
        op_ref(KEY, "000259", block="300000000A"),  # letter in the block
        op_ref(KEY, "00025A"),  # letter in the counter
        op_ref(KEY, "000259", booked="20260231"),  # 31 February
        op_ref(KEY, "000259", account="CUENTA-SINTETICA"),  # not an account number
        " " * REF_LENGTH,  # «APERTURA»: everything blank
    ],
)
def test_rejects_anything_off_the_layout(raw: str | None) -> None:
    assert parse_operation_ref(raw) is None


def test_rejects_a_counter_that_is_not_the_reference() -> None:
    assert parse_operation_ref(op_ref(KEY, "000259"), "000258") is None


def test_leg_key_is_the_operation_id() -> None:
    assert leg("a", "A", "-1", ref=op_ref(KEY, "000001")).key == KEY
    assert leg("a", "A", "-1", ref="free text").key is None


# ---- pairing by key -------------------------------------------------------------------


def test_pairs_both_legs_by_key() -> None:
    out = leg("A:259", "A", "-50", ref=op_ref(KEY, "000259"), reference="000259")
    into = leg("B:32", "B", "50", ref=op_ref(KEY, "000032", account=ACCOUNT_B, check="7"), reference="000032")

    matching = match_transfers([into, out])

    [pair] = matching.pairs
    assert (pair.source, pair.target, pair.key, pair.match) == (out, into, KEY, MatchKind.key)
    assert matching.unmatched == ()


def test_same_operation_id_with_another_block_is_not_paired() -> None:
    out = leg("A:1", "A", "-50", ref=op_ref(KEY, "000001"))
    into = leg("B:1", "B", "50", ref=op_ref(KEY, "000002", block="3000000002"))

    assert match_transfers([out, into]).pairs == ()


def test_credit_from_another_client_is_not_paired() -> None:
    """Another MyInvestor client's transfer looks like mine but carries an id none of my accounts has."""
    mine_out = leg("A:259", "A", "-50", ref=op_ref(KEY, "000259"))
    mine_in = leg("B:32", "B", "50", ref=op_ref(KEY, "000032"))
    gift = leg("B:33", "B", "50", concept=INTERNAL, ref=op_ref("120261014477050", "000033", block="3000000099"))

    matching = match_transfers([mine_out, mine_in, gift])

    assert [(p.source.id, p.target.id) for p in matching.pairs] == [("A:259", "B:32")]
    assert matching.unmatched == (gift,)


def test_regression_two_charges_three_credits_and_a_sepa() -> None:
    """Real case (anonymised): two own transfers of 50, a gift from another client and a SEPA, same day."""
    first, second, foreign = "120261102758295", "120261102758296", "120261014477050"
    charges = [
        leg("A:259", "A", "-50", concept=INTERNAL, ref=op_ref(first, "000259", block="3000000001")),
        leg("A:260", "A", "-50", concept=INTERNAL, ref=op_ref(second, "000260", block="3000000002")),
    ]
    credits = [
        leg("B:32", "B", "50", concept=INTERNAL, ref=op_ref(first, "000032", block="3000000001", check="7")),
        leg("B:33", "B", "50", concept=INTERNAL, ref=op_ref(foreign, "000033", block="3000000099", check="7")),
        leg("B:34", "B", "50", concept=INTERNAL, ref=op_ref(second, "000034", block="3000000002", check="7")),
        leg("B:35", "B", "50", concept="Regalo", ref=sepa_ref("000035"), kind="TRANSFERENCIA SEPA"),
    ]

    matching = match_transfers([*credits, *charges])

    assert [(p.source.id, p.target.id, p.key, p.match) for p in matching.pairs] == [
        ("A:259", "B:32", first, MatchKind.key),
        ("A:260", "B:34", second, MatchKind.key),
    ]
    assert [u.id for u in matching.unmatched] == ["B:33", "B:35"]


@pytest.mark.parametrize(
    ("out_amount", "in_amount", "in_account"),
    [("-50", "49.99", "B"), ("-50", "50", "A"), ("50", "50", "B")],
    ids=["different amount", "same account", "two credits"],
)
def test_same_key_without_a_matching_counterpart_is_not_paired(
    out_amount: str, in_amount: str, in_account: str
) -> None:
    legs = [
        leg("A:1", "A", out_amount, ref=op_ref(KEY, "000001")),
        leg("X:2", in_account, in_amount, ref=op_ref(KEY, "000002")),
    ]

    assert match_transfers(legs).pairs == ()


def test_key_group_with_several_legs_pairs_each_once() -> None:
    legs = [
        leg("A:1", "A", "-50", ref=op_ref(KEY, "000001")),
        leg("A:2", "A", "-20", ref=op_ref(KEY, "000002")),
        leg("B:1", "B", "20", ref=op_ref(KEY, "000003")),
        leg("B:2", "B", "50", ref=op_ref(KEY, "000004")),
        leg("B:3", "B", "50", ref=op_ref(KEY, "000005")),
    ]

    matching = match_transfers(legs)

    assert {(p.source.id, p.target.id) for p in matching.pairs} == {("A:1", "B:2"), ("A:2", "B:1")}
    assert [u.id for u in matching.unmatched] == ["B:3"]


# ---- heuristic fallback -------------------------------------------------------------


@pytest.mark.parametrize("shift", [-1, 0, 1])
def test_heuristic_pairs_keyless_legs_within_a_day(shift: int) -> None:
    out = leg("A:1", "A", "-50", concept="Paga")
    into = leg("B:1", "B", "50", day=DAY + timedelta(days=shift), concept=f"  {INTERNAL.upper()} ")

    [pair] = match_transfers([out, into]).pairs

    assert (pair.source, pair.target, pair.key, pair.match) == (out, into, None, MatchKind.heuristic)


@pytest.mark.parametrize(
    ("into", "why"),
    [
        (leg("B:1", "B", "50", day=DAY + timedelta(days=2), concept=INTERNAL), "two days apart"),
        (leg("B:1", "B", "50", concept="Bizum"), "no internal-transfer concept"),
        (leg("B:1", "B", "49", concept=INTERNAL), "different amount"),
        (leg("A:2", "A", "50", concept=INTERNAL), "same account"),
        (leg("B:1", "B", "50", concept=INTERNAL, kind="TRANSFERENCIA SEPA"), "a SEPA credit"),
        (leg("B:1", "B", "50", concept=INTERNAL, kind=""), "no type"),
        (leg("B:1", "B", "50", concept=INTERNAL, ref=op_ref(KEY, "000001")), "the credit has a key"),
    ],
)
def test_heuristic_needs_every_condition(into: Leg, why: str) -> None:
    out = leg("A:1", "A", "-50", concept="Paga")

    assert match_transfers([out, into]).pairs == (), why


def test_heuristic_needs_a_transfer_charge() -> None:
    out = leg("A:1", "A", "-50", concept=INTERNAL, kind="TRANSFERENCIA SEPA")
    into = leg("B:1", "B", "50", concept=INTERNAL)

    assert match_transfers([out, into]).pairs == ()


def test_credit_from_another_client_is_never_paired_heuristically() -> None:
    """Even with a keyless charge of the same amount and day: a leg with a key only pairs by key."""
    keyless_out = leg("A:1", "A", "-50", concept=INTERNAL)
    gift = leg("B:33", "B", "50", concept=INTERNAL, ref=op_ref("120261014477050", "000033"))

    matching = match_transfers([keyless_out, gift])

    assert matching.pairs == ()
    assert set(matching.unmatched) == {keyless_out, gift}


@pytest.mark.parametrize(
    "legs",
    [
        [leg("A:1", "A", "-50", concept=INTERNAL), leg("B:1", "B", "50"), leg("C:1", "C", "50")],
        [leg("A:1", "A", "-50", concept=INTERNAL), leg("C:1", "C", "-50", concept=INTERNAL), leg("B:1", "B", "50")],
    ],
    ids=["two credits", "two charges"],
)
def test_heuristic_does_not_pair_when_in_doubt(legs: list[Leg]) -> None:
    matching = match_transfers(legs)

    assert matching.pairs == ()
    assert len(matching.unmatched) == 3


def test_heuristic_resolves_the_unambiguous_pair_next_to_an_ambiguous_one() -> None:
    legs = [
        leg("A:1", "A", "-50", concept=INTERNAL),
        leg("B:1", "B", "50"),
        leg("C:1", "C", "50"),
        leg("A:2", "A", "-20", concept=INTERNAL),
        leg("B:2", "B", "20"),
    ]

    assert [(p.source.id, p.target.id) for p in match_transfers(legs).pairs] == [("A:2", "B:2")]


def test_unmatched_only_lists_transfer_like_legs() -> None:
    card = leg("A:9", "A", "-12.30", concept="Compra con tarjeta", kind="COMPRA CON TARJETA")
    sepa = leg("B:8", "B", "100", concept="Regalo", kind="TRANSFERENCIA SEPA")
    external = leg("B:9", "B", "100", concept=INTERNAL, kind="OTRO")

    assert match_transfers([card, sepa, external]).unmatched == (sepa, external)


def test_never_puts_a_movement_in_two_pairs() -> None:
    rng = random.Random(22)
    keys = [KEY, "120261102700002", "120261102700003"]
    for _ in range(300):
        legs = [
            leg(
                f"{account}:{n}",
                account,
                rng.choice(["-50", "50", "-20", "20"]),
                day=DAY + timedelta(days=rng.randint(-2, 2)),
                concept=rng.choice([INTERNAL, None]),
                ref=rng.choice([None, *(op_ref(k, f"{n:06d}") for k in keys)]),
            )
            for n, account in enumerate(rng.choices("ABC", k=rng.randint(0, 10)))
        ]

        matching = match_transfers(legs)

        ids = [leg_id for p in matching.pairs for leg_id in (p.source.id, p.target.id)]
        assert len(ids) == len(set(ids))
        assert not set(ids) & {u.id for u in matching.unmatched}
        for p in matching.pairs:
            assert p.source.amount == -p.target.amount < 0 and p.source.account != p.target.account


# ---- rows -------------------------------------------------------------------------------


def movement(movement_id: str, uuid: str, label: str, amount: str, **extra: object) -> dict[str, object]:
    return {
        "id": movement_id,
        "fecha": "2026-11-02",
        "cuenta": label,
        "cuenta_uuid": uuid,
        "tipo": CHARGE_TYPE if amount.startswith("-") else CREDIT_TYPE,
        "concepto": extra.pop("concepto", None),
        "importe": Decimal(amount),
        "ref": extra.pop("ref", None),
        "operacion_ref": extra.pop("operacion_ref", None),
        **extra,
    }


def test_with_transfer_fields_marks_both_legs_without_mutating() -> None:
    rows = [
        movement("A:259", "A", "Mi cuenta", "-50", ref="000259", operacion_ref=op_ref(KEY, "000259")),
        movement("B:32", "B", "Junior", "50", ref="000032", operacion_ref=op_ref(KEY, "000032")),
        movement("B:33", "B", "Junior", "50", concepto=INTERNAL),
    ]
    before = [dict(r) for r in rows]

    marked = with_transfer_fields(rows)

    assert rows == before
    assert [(r["pareja_id"], r["traspaso_interno"]) for r in marked] == [("B:32", True), ("A:259", True), (None, False)]
    assert all(r.items() >= o.items() for r, o in zip(marked, rows, strict=True))


def test_transfer_rows_are_flat() -> None:
    rows = [
        movement("A:259", "A", "Mi cuenta", "-50", concepto="Paga", operacion_ref=op_ref(KEY, "000259")),
        movement("B:32", "B", "Junior", "50", concepto=INTERNAL, operacion_ref=op_ref(KEY, "000032")),
        movement("B:33", "B", "Junior", "25", concepto=INTERNAL),
    ]

    pairs, lonely = transfer_rows(rows)

    assert pairs == [
        {
            "fecha": "2026-11-02",
            "importe": Decimal("50"),
            "desde_cuenta": "Mi cuenta",
            "desde_id": "A:259",
            "hacia_cuenta": "Junior",
            "hacia_id": "B:32",
            "concepto": "Paga",
            "clave": KEY,
            "match": "key",
        }
    ]
    assert lonely == [
        {
            "fecha": "2026-11-02",
            "importe": Decimal("25"),
            "desde_cuenta": None,
            "desde_id": None,
            "hacia_cuenta": "Junior",
            "hacia_id": "B:33",
            "concepto": INTERNAL,
            "clave": None,
            "match": None,
        }
    ]
