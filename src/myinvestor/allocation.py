"""Portfolio allocation: weights by asset type, region, category, product or account. No I/O.

Each position (fund, ETF, stock, pension plan and, optionally, cash) becomes a `Holding` with the
classification of its sheet, its asset type translated to a canonical class (see `asset_classes`).
`group` adds them up per key and `rebalance` compares the result with target weights, computing how
much to contribute to each key to reach them without selling.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from myinvestor.asset_classes import AssetClass, asset_class, known, underlying
from myinvestor.errors import InputError
from myinvestor.models import (
    CashAccount,
    FundSheet,
    Investment,
    PensionAccount,
    PensionPlanPosition,
    PensionPlanSheet,
    SecuritiesAccount,
)

UNCLASSIFIED = AssetClass.sin_clasificar.value  # the one "unknown" marker, in every grouping
CASH = AssetClass.efectivo.value
TOTAL = "total"
BROKER = "BROKER"  # `investmentProductType` of stocks and ETFs
MANAGED_PORTFOLIO = "SECURITIES_PORTFOLIO"  # `accountType` of a robo-advisor portfolio («Metal»…)
HUNDRED = Decimal(100)
PCT_STEP = Decimal("0.01")
TER_STEP = Decimal("0.0001")
TARGET_TOLERANCE = Decimal("0.01")  # target weights may sum to 100 ± this


class AccountKind(StrEnum):
    securities = "valores"
    portfolio = "cartera"  # managed portfolio
    pension = "pensiones"
    cash = CASH


class Grouping(StrEnum):
    asset = "asset"
    geo = "geo"
    category = "category"
    product = "product"
    account = "account"


# Label columns that follow `clave` in each grouping (`origen`: the API labels behind an asset class).
LABEL_COLUMNS: dict[Grouping, tuple[str, ...]] = {
    Grouping.asset: ("origen",),
    Grouping.product: ("nombre",),
    Grouping.account: ("cuenta_id", "tipo"),
}


@dataclass(frozen=True)
class Holding:
    """One position, classified. `ter` is a percent, `None` when unknown (broker, plans, cash).

    `asset` is an `AssetClass` value; `asset_label` is what it was translated from (`VARIABLE_INCOME`,
    «Renta Variable», `RV`, `ETF: GOLD`…), `None` when there was nothing to translate.
    """

    product: str  # ISIN, DGS code, ticker or `efectivo`
    name: str | None
    account_id: str
    account_name: str
    account_kind: str
    value: Decimal
    invested: Decimal | None
    profit: Decimal | None
    asset: str = UNCLASSIFIED
    geo: str = UNCLASSIFIED
    category: str = UNCLASSIFIED
    ter: Decimal | None = None
    asset_label: str | None = None

    def key(self, by: Grouping) -> str:
        keys = {
            Grouping.asset: self.asset,
            Grouping.geo: self.geo,
            Grouping.category: self.category,
            Grouping.product: self.product,
            Grouping.account: self.account_id,
        }
        return keys[by]


# ---- building holdings ------------------------------------------------------


def account_name(sec: SecuritiesAccount) -> str:
    """A managed portfolio's name (`METAL`), else the account code."""
    return sec.portfolio_name or sec.cod_account or sec.account_id


def account_kind(sec: SecuritiesAccount) -> AccountKind:
    return AccountKind.portfolio if sec.account_type == MANAGED_PORTFOLIO else AccountKind.securities


def is_fund(inv: Investment) -> bool:
    """Funds have a sheet in the catalog; stocks and ETFs (BROKER) are classified without one."""
    return inv.investment_product_type != BROKER and bool(inv.isin)


def broker_asset(inv: Investment) -> tuple[AssetClass, str]:
    """A share (`RV`) is equity; an ETF/ETC is classified by its underlying, read from its name and category."""
    kind = inv.broker_product_type or BROKER
    direct = asset_class(kind)
    if direct is not AssetClass.sin_clasificar:
        return direct, kind
    text = " ".join(t for t in (inv.investment_name, known(inv.my_investor_category)) if t)
    hit = underlying(text)
    return (hit[0], f"{kind}: {hit[1]}") if hit else (AssetClass.sin_clasificar, kind)


def from_investment(sec: SecuritiesAccount, inv: Investment, sheet: FundSheet | None) -> Holding:
    base = Holding(
        product=inv.isin or inv.ticker or inv.investment_name or UNCLASSIFIED,
        name=inv.investment_name,
        account_id=sec.account_id,
        account_name=account_name(sec),
        account_kind=account_kind(sec),
        value=inv.market_value or Decimal(0),
        invested=inv.initial_investment,
        profit=inv.profit,
        category=known(inv.my_investor_category) or UNCLASSIFIED,
    )
    if inv.investment_product_type == BROKER:
        asset, label = broker_asset(inv)
        return _replace(base, asset=asset.value, asset_label=label)
    if sheet is None:
        return base
    return _replace(
        base,
        asset=asset_class(sheet.asset_type).value,
        asset_label=sheet.asset_type,
        geo=known(sheet.geo_zone) or UNCLASSIFIED,
        category=known(sheet.category) or base.category,
        ter=sheet.ter,
    )


def from_pension(account: PensionAccount, plan: PensionPlanPosition, sheet: PensionPlanSheet | None) -> Holding:
    label = sheet.active_type if sheet else None
    return Holding(
        product=plan.dgs_code,
        name=plan.name,
        account_id=account.account_id,
        account_name=account.external_account_code or account.account_id,
        account_kind=AccountKind.pension,
        value=plan.market_value or Decimal(0),
        invested=plan.total_invested,
        profit=plan.profit,
        asset=asset_class(label).value,
        asset_label=label,
        category=known(sheet.category if sheet else None) or UNCLASSIFIED,
    )


def portfolios_by_cash_account(securities: Iterable[SecuritiesAccount]) -> dict[str, SecuritiesAccount]:
    """Managed portfolios by the id of their own cash account."""
    return {s.cash_account_id: s for s in securities if account_kind(s) is AccountKind.portfolio and s.cash_account_id}


def from_cash(acc: CashAccount, portfolio: SecuritiesAccount | None = None) -> Holding:
    """Cash as its own class; a managed portfolio's cash account counts as part of that portfolio."""
    owner = (
        (portfolio.account_id, account_name(portfolio), AccountKind.portfolio)
        if portfolio
        else (acc.account_id, acc.alias or acc.account_id, AccountKind.cash)
    )
    return Holding(
        product=CASH,
        name=None,
        account_id=owner[0],
        account_name=owner[1],
        account_kind=owner[2],
        value=acc.balance,
        invested=acc.balance,
        profit=Decimal(0),
        asset=CASH,
        geo=CASH,
        category=CASH,
    )


def _replace(h: Holding, **changes: Any) -> Holding:
    return Holding(**{**h.__dict__, **changes})


# ---- aggregation ------------------------------------------------------------


def share(part: Decimal, whole: Decimal) -> Decimal | None:
    """`part` as a percent of `whole`, to cents; `None` when `whole` is zero."""
    return None if not whole else (part * HUNDRED / whole).quantize(PCT_STEP, rounding=ROUND_HALF_UP)


def weighted_ter(holdings: Iterable[Holding]) -> Decimal | None:
    """Value-weighted mean TER of the holdings whose TER is known; the rest do not count."""
    known = [(h.ter, h.value) for h in holdings if h.ter is not None]
    weight = sum((value for _, value in known), Decimal(0))
    if not weight:
        return None
    return (sum((ter * value for ter, value in known), Decimal(0)) / weight).quantize(TER_STEP, rounding=ROUND_HALF_UP)


def _value(holdings: Iterable[Holding]) -> Decimal:
    return sum((h.value for h in holdings), Decimal(0))


def _known_sum(values: Iterable[Decimal | None]) -> Decimal | None:
    known = [v for v in values if v is not None]
    return sum(known, Decimal(0)) if known else None


def _labels(by: Grouping, holdings: Sequence[Holding]) -> dict[str, Any]:
    if not holdings:
        return dict.fromkeys(LABEL_COLUMNS.get(by, ()))
    h = holdings[0]
    if by is Grouping.asset:
        return {"origen": ", ".join(sorted({x.asset_label for x in holdings if x.asset_label})) or None}
    if by is Grouping.product:
        return {"nombre": h.name}
    if by is Grouping.account:
        return {"cuenta_id": h.account_id, "tipo": h.account_kind}
    return {}


def _row(key: str, by: Grouping, holdings: Sequence[Holding], total: Decimal) -> dict[str, Any]:
    value = _value(holdings)
    return {
        "clave": key,
        **_labels(by, holdings),
        "valor": value,
        "peso_pct": share(value, total),
        "invertido": _known_sum(h.invested for h in holdings),
        "ganancia": _known_sum(h.profit for h in holdings),
        "ter": weighted_ter(holdings),
    }


def _sorted(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (-r["valor"], r["clave"]))


def group(holdings: Sequence[Holding], by: Grouping) -> list[dict[str, Any]]:
    """One row per key, biggest first: `{clave, [labels], valor, peso_pct, invertido, ganancia, ter}`."""
    buckets: dict[str, list[Holding]] = {}
    for h in holdings:
        buckets.setdefault(h.key(by), []).append(h)
    total = _value(holdings)
    label = (lambda items: items[0].account_name) if by is Grouping.account else (lambda items: items[0].key(by))
    return _sorted(_row(label(items), by, items, total) for items in buckets.values())


def total_row(holdings: Sequence[Holding], by: Grouping, label: str = TOTAL) -> dict[str, Any]:
    """The whole portfolio: `ter` is its value-weighted TER (over the holdings with a known TER)."""
    return {**_row(label, by, holdings, _value(holdings)), **_labels(by, [])}


# ---- targets ----------------------------------------------------------------


def parse_target(data: Any) -> dict[str, Decimal]:
    """Target weights from a JSON object `{clave: percent}` that sums to 100."""
    if not isinstance(data, dict):
        raise InputError('El objetivo debe ser un objeto JSON {"clave": porcentaje}, p. ej. {"renta_variable": 80}.')
    if not data:
        raise InputError("El objetivo está vacío.")
    targets: dict[str, Decimal] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise InputError("Cada clave del objetivo debe ser un texto no vacío.")
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            raise InputError(f"El peso objetivo de {key!r} debe ser un número (porcentaje).")
        weight = Decimal(str(value))
        if not weight.is_finite() or weight < 0:
            raise InputError(f"El peso objetivo de {key!r} es negativo o no es finito.")
        targets[key] = weight
    total = sum(targets.values(), Decimal(0))
    if abs(total - HUNDRED) > TARGET_TOLERANCE:
        raise InputError(f"Los pesos objetivo suman {format(total, 'f')} y deben sumar 100.")
    return targets


def unbalanced_keys(rows: Iterable[dict[str, Any]], targets: Mapping[str, Decimal]) -> list[str]:
    """Keys with value but no (or a zero) target: they cannot shrink without selling, so they are left out."""
    return [r["clave"] for r in rows if r["valor"] > 0 and not targets.get(r["clave"])]


def rebalance(rows: Sequence[dict[str, Any]], targets: Mapping[str, Decimal], by: Grouping) -> list[dict[str, Any]]:
    """`rows` (from `group`) plus target keys not held, with `objetivo_pct`, `desviacion_pct` and `aportar`.

    Only keys with a positive target take part: their current value is the base. The final base is
    the smallest one in which no key is above its target, so `aportar` (what to add to each key)
    never requires selling. `desviacion_pct` = share of the base - target. Keys out of the target
    (see `unbalanced_keys`) get `null` in the three columns.
    """
    present = {r["clave"] for r in rows}
    total = sum((r["valor"] for r in rows), Decimal(0))
    missing = [_row(key, by, [], total) for key in targets if key not in present]
    every = [*rows, *missing]
    targeted = [r for r in every if targets.get(r["clave"])]
    base = sum((r["valor"] for r in targeted), Decimal(0))
    final = max([base, *(r["valor"] * HUNDRED / targets[r["clave"]] for r in targeted)])
    return _sorted({**r, **_target_columns(r, targets.get(r["clave"]), base, final)} for r in every)


def _target_columns(row: dict[str, Any], target: Decimal | None, base: Decimal, final: Decimal) -> dict[str, Any]:
    if not target:
        return {"objetivo_pct": target, "desviacion_pct": None, "aportar": None}
    current = share(row["valor"], base) or Decimal(0)
    return {
        "objetivo_pct": target,
        "desviacion_pct": (current - target).quantize(PCT_STEP, rounding=ROUND_HALF_UP),
        "aportar": max(Decimal(0), final * target / HUNDRED - row["valor"]),
    }


def target_total(rows: Sequence[dict[str, Any]], total: dict[str, Any]) -> dict[str, Any]:
    """The total row with the target columns: the targets' sum and the whole contribution."""
    targets = [r["objetivo_pct"] for r in rows if r["objetivo_pct"] is not None]
    return {
        **total,
        "objetivo_pct": sum(targets, Decimal(0)) if targets else None,
        "desviacion_pct": None,
        "aportar": _known_sum(r["aportar"] for r in rows),
    }


def untranslated_labels(holdings: Iterable[Holding]) -> list[str]:
    """Asset-type labels that fell into `sin_clasificar`, to report them."""
    return sorted({h.asset_label for h in holdings if h.asset == UNCLASSIFIED and h.asset_label})
