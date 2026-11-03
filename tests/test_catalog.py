"""Filters of the fund and pension-plan searchers as query parameters (pure functions)."""

from __future__ import annotations

import pytest

from myinvestor.catalog import (
    CatalogFilters,
    FilterKind,
    api_code,
    clean_values,
    match_value,
    parse_risk,
    ter_range,
)
from myinvestor.errors import InputError


@pytest.mark.parametrize(("text", "expected"), [("5", (5, 5)), ("3-6", (3, 6)), (" 2 - 4 ", (2, 4)), ("7-7", (7, 7))])
def test_risk_is_a_level_or_a_range(text: str, expected: tuple[int, int]) -> None:
    assert parse_risk(text) == expected


@pytest.mark.parametrize("text", ["0", "8", "6-3", "x", "1-", "-3", "1-2-3", ""])
def test_risk_outside_1_to_7_or_reversed_is_rejected(text: str) -> None:
    with pytest.raises(InputError, match="--risk"):
        parse_risk(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("variable_income", "VARIABLE_INCOME"), ("fixed-income", "FIXED_INCOME"), (" usd ", "USD")],
)
def test_codes_are_upper_case_with_underscores(text: str, expected: str) -> None:
    assert api_code(text) == expected


def test_ter_range_rejects_a_minimum_above_the_maximum() -> None:
    assert ter_range(0.1, 0.5) == (0.1, 0.5)
    assert ter_range(None, None) == (None, None)
    with pytest.raises(InputError, match="--min-ter"):
        ter_range(0.6, 0.5)


def test_values_are_stripped_and_nulls_dropped() -> None:
    raw = ["\t Japan Equity", None, "  ", "Europe Equity", "Europe Equity ", 3]

    assert clean_values(raw) == ["Japan Equity", "Europe Equity"]


def test_a_value_is_matched_to_the_exact_api_spelling() -> None:
    options = ["\t Japan Equity", "Europe Large-Cap Blend Equity"]

    assert match_value(" japan equity", options, FilterKind.categories) == "\t Japan Equity"


def test_an_unknown_value_suggests_the_closest_and_the_listing() -> None:
    with pytest.raises(InputError) as error:
        match_value("Japon Equity", ["\t Japan Equity", "Europe Equity"], FilterKind.categories)

    message = str(error.value)
    assert "«Japan Equity»" in message
    assert "fund-filters categories" in message


def test_no_filters_means_no_parameters() -> None:
    assert CatalogFilters().params() == {}


def test_every_filter_maps_to_its_query_parameter() -> None:
    filters = CatalogFilters(
        category="\t Japan Equity",
        region="UNITED_STATES",
        manager="Gestora Uno",
        asset_type="VARIABLE_INCOME",
        currency="USD",
        min_ter=0.1,
        max_ter=0.3,
        risk=(5, 6),
        rating=4,
        esg=True,
        hedged=True,
    )

    assert filters.params() == {
        "category": "\t Japan Equity",
        "region": "UNITED_STATES",
        "manager": "Gestora Uno",
        "assetsType": "VARIABLE_INCOME",
        "currency": "USD",
        "mstarRating": 4,
        "currencyHedging": "true",
        "responsibleFund": "true",
        "terMinValue": 0.1,
        "terTopValue": 0.3,
        "riskMinValue": 5,
        "riskTopValue": 6,
    }
