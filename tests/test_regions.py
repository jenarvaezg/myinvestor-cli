from __future__ import annotations

import pytest

from myinvestor.regions import isin_region, region


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # MyInvestor's own pension plans and their categories
        ("MyInvestor Indexado S&P 500, PP", ("UNITED_STATES", "S&P 500")),
        ("MyInvestor Indexado Global, PP", ("GLOBAL", "GLOBAL")),
        ("RV Global PP", ("GLOBAL", "GLOBAL")),
        ("RV EE.UU.", ("UNITED_STATES", "EE.UU")),
        ("Renta Variable Estados Unidos", ("UNITED_STATES", "ESTADOS UNIDOS")),
        ("RV Euro", ("EUROPE", "EURO")),
        ("Renta Variable Europa", ("EUROPE", "EUROPA")),
        ("RV Países Emergentes", ("EMERGENT_MARKETS", "EMERGENTES")),
        # ETF names
        ("iShares Core MSCI World UCITS ETF", ("GLOBAL", "WORLD")),
        ("Vanguard FTSE All-World UCITS ETF", ("GLOBAL", "ALL-WORLD")),
        ("SPDR MSCI ACWI IMI", ("GLOBAL", "ACWI")),
        ("iShares Core S&P500 UCITS ETF", ("UNITED_STATES", "S&P500")),
        ("Invesco EQQQ Nasdaq-100", ("UNITED_STATES", "NASDAQ")),
        ("Xtrackers MSCI USA", ("UNITED_STATES", "USA")),
        ("Amundi Euro Stoxx 50", ("EUROPE", "EURO STOXX")),
        ("iShares STOXX Europe 600", ("EUROPE", "EUROPE")),
        ("iShares Core MSCI EM IMI", ("EMERGENT_MARKETS", "MSCI EM")),
        ("Global Emerging Markets Equity", ("EMERGENT_MARKETS", "EMERGING")),  # emerging before global
        ("MSCI Emerging Europe", ("EUROPE_EMERGENT_MARKETS", "EMERGING EUROPE")),
        ("MSCI Japan", ("JAPAN", "JAPAN")),
        ("Asia ex Japan Equity", ("ASIA_EX_JAPAN", "ASIA EX_JAPAN")),
        ("Amundi IBEX 35", ("SPAIN", "IBEX")),
        ("MSCI World ex USA", ("GLOBAL", "WORLD")),  # what follows «ex» is excluded, not the region
        ("MSCI Europe ex UK", ("EUROPE", "EUROPE")),
    ],
)
def test_region_from_name_or_category(text: str, expected: tuple[str, str]) -> None:
    assert region(text) == expected


@pytest.mark.parametrize(
    "text",
    [None, "", "Plan", "RV MIXTA", "Fondo IE0000000001", "iShares Physical Gold ETC"],
)
def test_region_is_none_when_nothing_names_one(text: str | None) -> None:
    assert region(text) is None


def test_eur_currency_is_not_a_region() -> None:
    assert region("Thematic Robotics UCITS ETF EUR Hedged") is None


@pytest.mark.parametrize(
    ("isin", "zone"),
    [("US0378331005", "UNITED_STATES"), ("es0113900J37", "SPAIN"), ("NL0010273215", None), (None, None)],
)
def test_isin_region_is_the_issuer_country(isin: str | None, zone: str | None) -> None:
    assert isin_region(isin) == zone
