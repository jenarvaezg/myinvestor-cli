"""Regions for `allocation --by geo`, in the vocabulary of the funds' `geoZone` (GLOBAL, EUROPE…).

Funds bring their `geoZone`. Everything else is read from what the position or its sheet says:

- ETFs and pension plans: words of the plan's category and of the name, with `REGION_RULES`
  (`S&P 500` → UNITED_STATES, `MSCI World` → GLOBAL…).
- shares: the country of the ISIN (the issuer's), when it has a zone of its own.
- commodities and cash have no region: `NOT_APPLICABLE`, kept apart from `sin_clasificar`
  (which means "unknown").

What follows `ex` is excluded, not a region: «MSCI World ex USA» is GLOBAL, not UNITED_STATES.
"""

from __future__ import annotations

import re
import unicodedata

from myinvestor.asset_classes import AssetClass

NOT_APPLICABLE = "no_aplica"
# Asset classes that have no region at all.
REGIONLESS = frozenset({AssetClass.materias_primas, AssetClass.efectivo})

# Word patterns → zone, first match wins: emerging markets before the regions they name («Emerging
# Europe», «Global Emerging Markets»), single regions and countries before the whole world.
REGION_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b({pattern})\b"), zone)
    for pattern, zone in (
        (r"EMERGING EUROPE|EUROPA EMERGENTE", "EUROPE_EMERGENT_MARKETS"),
        (r"EMERGENTES?|EMERGING|MSCI EM", "EMERGENT_MARKETS"),
        (r"ASIA EX_JAP(AN|ON)", "ASIA_EX_JAPAN"),
        (r"LATIN AMERICA|AMERICA LATINA|LATINOAMERICA|LATAM", "LATIN_AMERICA"),
        (
            r"S&P ?500|USA|EE\.? ?UU|ESTADOS UNIDOS|UNITED STATES|NORTEAMERICA|NASDAQ|RUSSELL ?\d+",
            "UNITED_STATES",
        ),
        (r"EUROP(A|E|EAN|EA|EO)|EURO ?STOXX|EUROZON(A|E)|ZONA EURO|EURO AREA|EMU|EURO", "EUROPE"),
        (r"UK|UNITED KINGDOM|REINO UNIDO|FTSE (100|250)", "UNITED_KINGDOM"),
        (r"SPAIN|ESPANA|IBEX", "SPAIN"),
        (r"GERMANY|ALEMANIA|DAX", "GERMANY"),
        (r"FRANCE|FRANCIA|CAC ?40", "FRANCE"),
        (r"JAPAN|JAPON|NIKKEI|TOPIX", "JAPAN"),
        (r"CHINA|CSI ?300", "CHINA"),
        (r"INDIA", "INDIA"),
        (r"ASIA|PACIFIC|PACIFICO", "ASIA"),
        (r"GLOBAL|WORLD|MUNDIAL|MUNDO|ACWI|ALL[ -]WORLD|ALL[ -]COUNTRY", "GLOBAL"),
    )
)

# Issuer countries (first two letters of a share's ISIN) that have a zone of their own.
ISIN_COUNTRIES: dict[str, str] = {
    "US": "UNITED_STATES",
    "ES": "SPAIN",
    "DE": "GERMANY",
    "FR": "FRANCE",
    "IT": "ITALY",
    "GB": "UNITED_KINGDOM",
    "CH": "SWITZERLAND",
    "SE": "SWEDEN",
    "NO": "NORWAY",
    "JP": "JAPAN",
    "CN": "CHINA",
    "IN": "INDIA",
    "KR": "KOREA",
    "BR": "BRAZIL",
    "MX": "MEXICO",
}

_EXCLUSION = re.compile(r"\bEX[ -]+(\w)")  # «ex USA» → «EX_USA», which no rule matches as a region


def _plain(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper()
    return _EXCLUSION.sub(r"EX_\1", " ".join(plain.split()))


def region(text: str | None) -> tuple[str, str] | None:
    """Zone named by `text` (a name or category) and the words that gave it away; None if it names none."""
    if not text:
        return None
    plain = _plain(text)
    for pattern, zone in REGION_RULES:
        match = pattern.search(plain)
        if match:
            return zone, match.group(0)
    return None


def isin_region(isin: str | None) -> str | None:
    """Zone of a share's issuer country, from its ISIN; None if the country has no zone of its own."""
    return ISIN_COUNTRIES.get((isin or "")[:2].upper())
