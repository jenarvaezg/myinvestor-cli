"""Classify cash-account movements (`operationType` / `operationClass`) into categories for `income`.

Pure functions, no I/O. The API documents no catalog of `operationType` values, so the table has
three tiers, tried in this order (first match wins):

1. `EXACT_TYPES`: `operationType` values observed on real accounts, matched whole.
2. `KEYWORD_RULES`: words that may appear in types not observed yet (interest, dividends, IRPF
   withholding, custody fees…). Each rule lists word prefixes that must all start some word of
   the type, so `INTERES` matches «INTERESES» but «RV» does not match «SERVICIO».
3. `EXACT_CLASSES`: `operationClass` codes, for movements whose type matched nothing.

Anything else is `otros`, flagged as unknown (`Classification.rule is None`) so `income --by type`
lists it with its `operationType` and the table can grow. Types and keywords are compared without
accents and in upper case. To extend: add an observed type to `EXACT_TYPES`; add a keyword only
when it can't be mistaken for something else (card payments are not purchases of securities).

Transfers are `aportacion_externa` by type (money entering or, with a negative amount, leaving
MyInvestor), unless the caller paired both legs of a transfer between two of the user's own accounts
(`transfers_match`): then `classify(..., internal_transfer=True)` makes it `traspaso_interno`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Category(StrEnum):
    """Movement categories, in display order."""

    intereses = "intereses"  # interest credited (account remuneration, deposits)
    dividendos = "dividendos"
    retencion = "retencion"  # tax withheld (IRPF) on interest or dividends
    comision = "comision"  # brokerage, custody and other fees
    compra = "compra"  # purchase of securities
    venta = "venta"  # sale of securities
    aportacion_externa = "aportacion_externa"  # money from/to outside MyInvestor (transfers)
    traspaso_interno = "traspaso_interno"  # between the user's own products
    aportacion_plan = "aportacion_plan"  # contribution to a pension plan: leaves the account, not a spend
    otros = "otros"


@dataclass(frozen=True)
class KeywordRule:
    category: Category
    words: tuple[str, ...]  # word prefixes that must all be present

    def matches(self, tokens: tuple[str, ...]) -> bool:
        return all(any(token.startswith(word) for token in tokens) for word in self.words)

    @property
    def label(self) -> str:
        return "palabra clave: " + " + ".join(self.words)


@dataclass(frozen=True)
class Classification:
    category: Category
    rule: str | None  # which rule matched; None when the movement is unknown (in `otros`)

    @property
    def known(self) -> bool:
        return self.rule is not None


EXACT_TYPE_RULE = "tipo exacto"
EXACT_CLASS_RULE = "clase exacta"
PAIRED_RULE = "traspaso emparejado"  # both legs found in the user's accounts (`transfers_match`)

# `operationType` values seen on real accounts.
EXACT_TYPES: Mapping[str, Category] = MappingProxyType(
    {
        "CARGO POR TRANSFERENCIA": Category.aportacion_externa,
        "ABONO POR TRANSFERENCIA": Category.aportacion_externa,
        "TRANSFERENCIA INMEDIATA": Category.aportacion_externa,
        "TRANSFERENCIA SEPA": Category.aportacion_externa,
        "COMPRA RV CONTADO": Category.compra,  # stock/ETF purchase (renta variable, al contado)
        "SUSCRIPCION IIC": Category.compra,  # fund subscription (institución de inversión colectiva)
        "LIQUIDAC. INTERESES": Category.intereses,  # interest settlement of the cash account
        "APORTACION PLANES": Category.aportacion_plan,  # contribution to a pension plan
        "APERTURA": Category.otros,  # account opening; known, so not reported as unknown
    }
)

# Order matters: withholding and fees go first because their types often name what they apply to
# («RETENCION DIVIDENDOS», «COMISION COMPRA RV»).
KEYWORD_RULES: tuple[KeywordRule, ...] = (
    KeywordRule(Category.retencion, ("RETENC",)),
    KeywordRule(Category.retencion, ("IRPF",)),
    KeywordRule(Category.comision, ("COMISION",)),
    KeywordRule(Category.comision, ("CUSTODIA",)),
    KeywordRule(Category.dividendos, ("DIVIDEND",)),
    KeywordRule(Category.intereses, ("INTERES",)),
    KeywordRule(Category.intereses, ("REMUNERAC",)),
    KeywordRule(Category.compra, ("COMPRA", "RV")),
    KeywordRule(Category.compra, ("COMPRA", "VALORES")),
    KeywordRule(Category.compra, ("SUSCRIPC",)),
    KeywordRule(Category.venta, ("VENTA", "RV")),
    KeywordRule(Category.venta, ("VENTA", "VALORES")),
    KeywordRule(Category.traspaso_interno, ("TRASPASO",)),
    KeywordRule(Category.aportacion_externa, ("TRANSFERENCIA",)),
)

# `operationClass` codes tied to a category. None is yet: no class has been seen to mean one
# thing only. Add codes here once they are.
EXACT_CLASSES: Mapping[str, Category] = MappingProxyType({})

_WORD = re.compile(r"[A-Z0-9]+")


def normalize(text: str | None) -> str:
    """Upper case, without accents and with single spaces: «Liquidación  intereses» → «LIQUIDACION INTERESES»."""
    if not text:
        return ""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(plain.upper().split())


def classify(
    operation_type: str | None, operation_class: str | None = None, *, internal_transfer: bool = False
) -> Classification:
    """Category of a movement from its `operationType` and, failing that, its `operationClass`.

    `internal_transfer`: the movement is one leg of a paired transfer between the user's own accounts.
    """
    if internal_transfer:
        return Classification(Category.traspaso_interno, PAIRED_RULE)
    kind = normalize(operation_type)
    if kind in EXACT_TYPES:
        return Classification(EXACT_TYPES[kind], EXACT_TYPE_RULE)
    tokens = tuple(_WORD.findall(kind))
    rule = next((r for r in KEYWORD_RULES if r.matches(tokens)), None)
    if rule is not None:
        return Classification(rule.category, rule.label)
    code = normalize(operation_class)
    if code in EXACT_CLASSES:
        return Classification(EXACT_CLASSES[code], EXACT_CLASS_RULE)
    return Classification(Category.otros, None)
