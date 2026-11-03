"""Filters of the fund and pension-plan searchers («Explorar» on the web) as API query parameters.

The web builds the query of `/cperf-server/api/v2/funds` in `IAe` and that of `/v2/pension-plans` in
`sMe` (same keys, fewer filters). The valid values come from
`/cperf-server/api/v2/masters/{funds|pension-plans}/searcher/filters/{kind}`; some arrive with leading
blanks or tabs (`"\\t Japan Equity"`): they are shown stripped, but the query must carry them as sent.

Not a filter: `type` (`IAe` sends it, but `/v2/funds` answers 400 to any value, `FONDOS_INDEXADOS`
included) and so neither is `masters/funds/searcher/filters/types`, which only feeds a form.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from myinvestor.errors import InputError

RISK_LEVELS = range(1, 8)  # SRI, 1 (lowest) to 7
RISK_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


class Domain(StrEnum):
    """The catalog a filter list belongs to (path segment of `masters/{domain}/searcher/filters`)."""

    funds = "funds"
    pension_plans = "pension-plans"


class FilterKind(StrEnum):
    categories = "categories"
    managers = "managers"
    currencies = "currencies"
    asset_types = "asset-types"
    regions = "regions"

    @property
    def path(self) -> str:
        return "assets-type" if self is FilterKind.asset_types else self.value

    @property
    def option(self) -> str:
        """The option of `funds` (and `pension-plans`) that takes these values."""
        return FILTER_OPTIONS[self]


FILTER_OPTIONS = {
    FilterKind.categories: "--category",
    FilterKind.managers: "--manager",
    FilterKind.currencies: "--currency",
    FilterKind.asset_types: "--asset-type",
    FilterKind.regions: "--region",
}
PENSION_FILTERS = (FilterKind.managers, FilterKind.asset_types)  # what the pension-plan searcher filters by
# Plans the web shows apart from the searcher, by promoter (a constant of its bundle, 2026-11). They
# are not in `/v2/pension-plans`: the web reads each one with `/v2/pension-plans/{dgs}` (#68).
FEATURED_PENSION_PLANS = {
    "MyInvestor": ("N5394", "N5396", "N5459"),  # Indexado S&P 500, Indexado Global…
    "Finanbest": ("N5271", "N5270"),
    "Merchbanc": ("N1175", "N5290"),
}


@dataclass(frozen=True)
class CatalogFilters:
    """What to filter the catalog by; None (or False) leaves the API's default (no filter)."""

    category: str | None = None
    region: str | None = None
    manager: str | None = None
    asset_type: str | None = None
    currency: str | None = None
    min_ter: float | None = None
    max_ter: float | None = None
    risk: tuple[int, int] | None = None
    rating: int | None = None  # Morningstar stars
    esg: bool = False
    hedged: bool = False

    def params(self) -> dict[str, Any]:
        low, high = self.risk or (None, None)
        pairs: dict[str, Any] = {
            "category": self.category,
            "region": self.region,
            "manager": self.manager,
            "assetsType": self.asset_type,
            "currency": self.currency,
            "mstarRating": self.rating,
            "currencyHedging": "true" if self.hedged else None,
            "responsibleFund": "true" if self.esg else None,
            "terMinValue": self.min_ter,
            "terTopValue": self.max_ter,
            "riskMinValue": low,
            "riskTopValue": high,
        }
        return {key: value for key, value in pairs.items() if value is not None}


def parse_risk(text: str) -> tuple[int, int]:
    """`5` → (5, 5); `3-6` → (3, 6): risk indicator from 1 to 7."""
    match = RISK_RANGE.match(text)
    low, high = (int(match[1]), int(match[2] or match[1])) if match else (0, 0)
    if low not in RISK_LEVELS or high not in RISK_LEVELS or low > high:
        raise InputError(f"--risk {text!r} no válido: un nivel de 1 a 7 (`5`) o un rango (`3-6`).")
    return low, high


def api_code(text: str) -> str:
    """`variable-income` → `VARIABLE_INCOME`: the API's codes of asset types, regions and currencies."""
    return text.strip().upper().replace("-", "_")


def ter_range(min_ter: float | None, max_ter: float | None) -> tuple[float | None, float | None]:
    if min_ter is not None and max_ter is not None and min_ter > max_ter:
        raise InputError(f"--min-ter {min_ter} es mayor que --max-ter {max_ter}.")
    return min_ter, max_ter


def clean_values(raw: Iterable[Any]) -> list[str]:
    """The values to show: stripped, without nulls, blanks or repeats, in the API's order."""
    return list(dict.fromkeys(v.strip() for v in raw if isinstance(v, str) and v.strip()))


def match_value(value: str, options: Iterable[str], kind: FilterKind, domain: Domain = Domain.funds) -> str:
    """The API's exact spelling of `value` (compared stripped and ignoring case)."""
    by_key = {option.strip().casefold(): option for option in options if option.strip()}
    found = by_key.get(value.strip().casefold())
    if found is not None:
        return found
    shown = [option.strip() for option in by_key.values()]
    close = difflib.get_close_matches(value.strip(), shown, n=1)
    hint = f" ¿Quizás «{close[0]}»?" if close else ""
    listing = f"myinvestor fund-filters {kind.value}{' --pension' if domain is Domain.pension_plans else ''}"
    raise InputError(f"{kind.option} {value!r} no es un valor del buscador.{hint} Los valores válidos: `{listing}`.")
