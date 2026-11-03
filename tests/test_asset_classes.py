from __future__ import annotations

import pytest

from myinvestor.asset_classes import AssetClass, asset_class, known, normalize, underlying

C = AssetClass


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # funds' assetType
        ("VARIABLE_INCOME", C.renta_variable),
        ("FIXED_INCOME", C.renta_fija),
        ("MONEY_MARKET", C.monetario),
        ("MIXED", C.mixto),
        ("COMMODITIES", C.materias_primas),
        ("ALTERNATIVES", C.alternativos),
        # brokerProductType of a share
        ("RV", C.renta_variable),
        # pension plans' activeType and other Spanish labels
        ("Renta Variable", C.renta_variable),
        ("RENTA VARIABLE", C.renta_variable),
        ("Renta Variable Euro", C.renta_variable),
        ("Renta Fija", C.renta_fija),
        ("RF", C.renta_fija),
        ("Renta Fija Mixta", C.mixto),
        ("Renta Variable Mixta Internacional", C.mixto),
        ("Mercado monetario", C.monetario),
        ("Monetario", C.monetario),
        ("Mixto", C.mixto),
        ("Materias primas", C.materias_primas),
        ("Inversión alternativa", C.alternativos),
        ("Inmobiliario", C.inmobiliario),
        # the wrapper alone says nothing; unknown markers and unknown labels
        ("ETF", C.sin_clasificar),
        ("Garantizado", C.sin_clasificar),
        ("Desconocido", C.sin_clasificar),
        ("", C.sin_clasificar),
        (None, C.sin_clasificar),
    ],
)
def test_asset_class_translates_every_known_label(label: str | None, expected: AssetClass) -> None:
    assert asset_class(label) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ETFS EUR HEDGED SILVER", (C.materias_primas, "SILVER")),
        ("X IE PHYSICAL GOLD ETC", (C.materias_primas, "PHYSICAL")),
        ("WISDOMTREE BROAD COMMODITIES", (C.materias_primas, "COMMODITIES")),
        ("GOLD MINERS UCITS ETF", (C.renta_variable, "MINERS")),
        ("EUR GOVT BOND 1-3YR UCITS ETF", (C.renta_fija, "GOVT")),
        ("S&P 500 CORPORATE BOND", (C.renta_fija, "CORPORATE")),
        ("EUR OVERNIGHT RATE SWAP", (C.monetario, "OVERNIGHT")),
        ("CORE MSCI WORLD UCITS ETF", (C.renta_variable, "MSCI")),
        ("S&P 500 UCITS ETF", (C.renta_variable, "S&P")),
    ],
)
def test_underlying_from_the_name(name: str, expected: tuple[AssetClass, str]) -> None:
    assert underlying(name) == expected


def test_underlying_is_none_when_the_name_says_nothing() -> None:
    assert underlying("ISHARES THEMATIC UCITS ETF") is None


def test_normalize_and_known() -> None:
    assert normalize("  Renta Fija Mixta / Euro ") == "RENTA_FIJA_MIXTA_EURO"
    assert normalize("Inversión alternativa") == "INVERSION_ALTERNATIVA"
    assert known("Desconocido") is None and known("UNKNOWN") is None and known(None) is None
    assert known(" RV Global ") == "RV Global"
