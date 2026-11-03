"""Canonical asset classes: every asset-type code or label MyInvestor sends, mapped to one fixed set.

Sources (all seen in the API or in the web bundle):

- funds: `assetType` of the sheet/catalog: VARIABLE_INCOME, FIXED_INCOME, MONEY_MARKET, COMMODITIES,
  MIXED, ALTERNATIVES (the web labels them «Renta variable», «Renta fija», «Mercado monetario»,
  «Materias primas», «Mixto», «Inversión alternativa»). No real-estate code exists.
- pension plans: `activeType` of the plan sheet, free text («Renta Variable», «Renta Fija Mixta»…).
- stocks and ETFs (`investmentProductType: BROKER`): `brokerProductType` is `RV` for a share and `ETF`
  for an ETF or ETC. An ETF wrapper says nothing about what it holds, so ETFs are classified by their
  underlying, read from the name (and category) with `UNDERLYING_RULES`, or left unclassified.

Labels are compared after `normalize` (no accents, upper case, `_` between words).
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum


class AssetClass(StrEnum):
    renta_variable = "renta_variable"
    renta_fija = "renta_fija"
    monetario = "monetario"
    mixto = "mixto"
    materias_primas = "materias_primas"
    alternativos = "alternativos"  # ALTERNATIVES, «Inversión alternativa»
    inmobiliario = "inmobiliario"  # no code seen in the API: only if a label says so
    efectivo = "efectivo"
    sin_clasificar = "sin_clasificar"


_C = AssetClass

# Exact labels, normalized.
ASSET_LABELS: dict[str, AssetClass] = {
    # funds' `assetType`
    "VARIABLE_INCOME": _C.renta_variable,
    "FIXED_INCOME": _C.renta_fija,
    "MONEY_MARKET": _C.monetario,
    "MIXED": _C.mixto,
    "COMMODITIES": _C.materias_primas,
    "ALTERNATIVES": _C.alternativos,
    # `brokerProductType` (and the web's own short codes)
    "RV": _C.renta_variable,
    "STOCK": _C.renta_variable,
    "RF": _C.renta_fija,
    "ETC": _C.materias_primas,  # exchange-traded commodity
    # Spanish labels (pension plans' `activeType`, the web)
    "RENTA_VARIABLE": _C.renta_variable,
    "ACCIONES": _C.renta_variable,
    "RENTA_FIJA": _C.renta_fija,
    "MONETARIO": _C.monetario,
    "MERCADO_MONETARIO": _C.monetario,
    "MIXTO": _C.mixto,
    "MIXTA": _C.mixto,
    "RENTA_MIXTA": _C.mixto,
    "MATERIAS_PRIMAS": _C.materias_primas,
    "INVERSION_ALTERNATIVA": _C.alternativos,
    "ALTERNATIVO": _C.alternativos,
    "INMOBILIARIO": _C.inmobiliario,
    "REAL_ESTATE": _C.inmobiliario,
    "EFECTIVO": _C.efectivo,
    "LIQUIDEZ": _C.efectivo,
    "CASH": _C.efectivo,
}

# Label prefixes, first match wins (`Renta Variable Euro`, `Renta Fija Mixta Internacional`…).
ASSET_PREFIXES: tuple[tuple[str, AssetClass], ...] = (
    ("RENTA_FIJA_MIXTA", _C.mixto),
    ("RENTA_VARIABLE_MIXTA", _C.mixto),
    ("RENTA_VARIABLE", _C.renta_variable),
    ("RENTA_FIJA", _C.renta_fija),
    ("MONETARIO", _C.monetario),
)

# What an ETF holds, from words in its name or category; first match wins. Miners and producers go
# first so that «Gold Miners» is equity, not gold; bonds before indices so that «S&P … Bond» is debt.
UNDERLYING_RULES: tuple[tuple[re.Pattern[str], AssetClass], ...] = tuple(
    (re.compile(pattern), cls)
    for pattern, cls in (
        (r"\b(MINERS?|MINING|PRODUCERS?)\b", _C.renta_variable),
        (r"\b(GOLD|SILVER|PLATINUM|PALLADIUM|PRECIOUS|PHYSICAL|ETCS?|COMMODIT\w*|ORO|PLATA)\b", _C.materias_primas),
        (r"\b(MONEY MARKET|OVERNIGHT|ESTR|MONETARIO)\b", _C.monetario),
        (
            r"\b(BONDS?|TREASURY|TREASURIES|GOVT|GOVERNMENT|AGGREGATE|CORPORATE|GILTS?|HIGH YIELD|RENTA FIJA)\b",
            _C.renta_fija,
        ),
        (
            r"\b(MSCI|S&P|STOXX|NASDAQ|FTSE|DAX|IBEX|NIKKEI|TOPIX|RUSSELL|EQUITY|EQUITIES|DIVIDENDS?|RENTA VARIABLE)\b",
            _C.renta_variable,
        ),
    )
)

# Labels that mean "unknown" (the API sends «Desconocido» as the category of broker positions).
UNKNOWN_LABELS = frozenset({"", "DESCONOCIDO", "UNKNOWN", "OTHERS", "OTROS", "N_A", "NA", "SIN_CLASIFICAR", "NONE"})


def normalize(label: str) -> str:
    """`Renta Fija Mixta` → `RENTA_FIJA_MIXTA`: no accents, upper case, `_` between words."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", label) if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", "_", plain.upper()).strip("_")


def known(label: str | None) -> str | None:
    """`label` unless it is missing or one of the API's "unknown" markers."""
    if label is None or normalize(label) in UNKNOWN_LABELS:
        return None
    return label.strip()


def asset_class(label: str | None) -> AssetClass:
    """The canonical class of an asset-type code or label; `sin_clasificar` if it is not recognized."""
    if not known(label):
        return _C.sin_clasificar
    key = normalize(label or "")
    if key in ASSET_LABELS:
        return ASSET_LABELS[key]
    return next((cls for prefix, cls in ASSET_PREFIXES if key.startswith(prefix)), _C.sin_clasificar)


def underlying(text: str) -> tuple[AssetClass, str] | None:
    """Class of what an ETF holds and the word that gave it away, from its name/category; None if unclear."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper()
    for pattern, cls in UNDERLYING_RULES:
        match = pattern.search(plain)
        if match:
            return cls, match.group(0)
    return None
