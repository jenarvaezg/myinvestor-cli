"""Robo-advisor portfolios: the catalog (`/cperf-server/api/v2/portfolios`), their labels and the drift
of a contracted portfolio's positions from its target weights.

A contracted portfolio is a securities account with `accountType: SECURITIES_PORTFOLIO` whose
`portfolioName` is the `name` of a catalog portfolio (`METAL`), with its own cash account (`cashAccountId`).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from myinvestor.allocation import AccountKind, account_kind, account_name, share
from myinvestor.models import CashAccount, Investment, SecuritiesAccount, _Model

SPANISH_DAY = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")  # `31/10/2023`
# `profile` / `portfolioProfileType`, from the most conservative to the most aggressive (web bundle).
PROFILE_LABELS = {
    "CONSERVATIVE": "conservadora",
    "MODERATE": "moderada",
    "DYNAMIC": "dinámica",
    "AGGRESSIVE": "agresiva",
    "VERY_AGGRESSIVE": "muy agresiva",
}


def catalog_day(value: Any) -> dt.date | None:
    """A catalog date: `DD/MM/AAAA` (what the API sends) or ISO; anything unreadable is None, never an error."""
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if SPANISH_DAY.fullmatch(text):
            return dt.datetime.strptime(text, "%d/%m/%Y").date()
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


class PortfolioFund(_Model):
    """A fund of a catalog portfolio with its target weight (`percentage`, in %)."""

    isin: str
    name: str | None = None
    currency: str | None = None
    percentage: Decimal | None = None
    category: str | None = None
    my_investor_category: str | None = None


class Portfolio(_Model):
    """A portfolio of the robo-advisor catalog."""

    id: int
    name: str
    profile: str | None = None
    description: str | None = None
    fixed_income: Decimal | None = None  # % of fixed income
    variable_income: Decimal | None = None  # % of equities
    profitability: Decimal | None = None  # cumulative % between `profitability_from` and `profitability_to`
    profitability_from: dt.date | None = None  # the API sends `DD/MM/AAAA` (`31/10/2023`)
    profitability_to: dt.date | None = None
    recommended_time_horizon: int | None = None  # years
    cod_benchmark: str | None = None
    type: str | None = None  # SAVING | HYBRID | INDEXED | SUSTAINABLE
    funds: list[PortfolioFund] = Field(default_factory=list)

    _days = field_validator("profitability_from", "profitability_to", mode="before")(catalog_day)

    @field_validator("funds", mode="before")
    @classmethod
    def _no_funds(cls, value: Any) -> Any:
        return [] if value is None else value


@dataclass(frozen=True)
class AnnualReturn:
    """One year of `…/v3/portfolios/{accountId}/returns/summary`."""

    year: str
    amount: Decimal | None
    percentage: Decimal | None


@dataclass(frozen=True)
class Drift:
    """A fund of a contracted portfolio: target weight, actual weight and their difference, in %."""

    isin: str
    name: str | None
    target: Decimal | None  # None: the portfolio is not in the catalog
    actual: Decimal | None  # None: the portfolio holds nothing yet
    value: Decimal

    @property
    def deviation(self) -> Decimal | None:
        return None if self.target is None or self.actual is None else self.actual - self.target


def profile_label(profile: str | None) -> str | None:
    """`VERY_AGGRESSIVE` → `muy agresiva`; an unknown code is shown as it comes."""
    return None if not profile else PROFILE_LABELS.get(profile, profile)


def with_profile(name: str, profile: str | None) -> str:
    """`METAL (muy agresiva)`, or just the name without a profile."""
    label = profile_label(profile)
    return f"{name} ({label})" if label else name


def is_portfolio(sec: SecuritiesAccount) -> bool:
    return account_kind(sec) is AccountKind.portfolio


def portfolio_title(sec: SecuritiesAccount) -> str:
    """`Cartera METAL (muy agresiva)`: how `position` and `portfolios --mine` name a contracted portfolio."""
    return f"Cartera {with_profile(account_name(sec), sec.portfolio_profile_type)}"


def own_cash(securities: Iterable[SecuritiesAccount], cash: Iterable[CashAccount]) -> dict[str, CashAccount]:
    """The own cash account of each contracted portfolio, by the portfolio's account id."""
    by_id = {a.account_id: a for a in cash}
    return {
        s.account_id: by_id[s.cash_account_id]
        for s in securities
        if is_portfolio(s) and s.cash_account_id and s.cash_account_id in by_id
    }


def find_portfolio(catalog: Sequence[Portfolio], ref: str) -> Portfolio | None:
    """A catalog portfolio by its id or its name, ignoring case (`metal`)."""
    needle = ref.strip().casefold()
    return next((p for p in catalog if needle in (str(p.id), p.name.casefold())), None)


def catalog_entry(catalog: Sequence[Portfolio], sec: SecuritiesAccount) -> Portfolio | None:
    """The catalog portfolio of a contracted one (`portfolioName` == `name`, ignoring case)."""
    name = (sec.portfolio_name or "").casefold()
    return next((p for p in catalog if name and p.name.casefold() == name), None)


def drift(target: Portfolio | None, holdings: Sequence[Investment]) -> list[Drift]:
    """Target funds first (catalog order), then held funds outside the target; matched by ISIN.

    Actual weights are over the value of the positions (the portfolio's cash does not count).
    """
    values: dict[str, Decimal] = {}
    names: dict[str, str | None] = {}
    for inv in holdings:
        key = inv.isin or inv.ticker or ""
        values[key] = values.get(key, Decimal(0)) + (inv.market_value or Decimal(0))
        names.setdefault(key, inv.investment_name)
    total = sum(values.values(), Decimal(0))
    targets = {f.isin: f for f in target.funds} if target else {}
    isins = list(dict.fromkeys([*targets, *values]))
    return [
        Drift(
            isin=isin,
            name=names.get(isin) or (targets[isin].name if isin in targets else None),
            target=_target_weight(target, targets, isin),
            actual=share(values.get(isin, Decimal(0)), total),
            value=values.get(isin, Decimal(0)),
        )
        for isin in isins
    ]


def _target_weight(target: Portfolio | None, targets: dict[str, PortfolioFund], isin: str) -> Decimal | None:
    if target is None:
        return None
    fund = targets.get(isin)
    return fund.percentage if fund and fund.percentage is not None else Decimal(0)


def annual_returns(data: Any) -> list[AnnualReturn]:
    """`years` of the returns summary, oldest first; tolerant because its shape is not verified.

    The web reads an object `{"2025": {amount, percentage}, …}`; its read-only variant, a list
    `[{year, amount, percentage}]`. Both are accepted; anything else (`null`, `[]`) is no years.
    """
    years = data.get("years") if isinstance(data, dict) else None
    if isinstance(years, dict):
        items = [(str(year), value) for year, value in years.items()]
    elif isinstance(years, list):
        items = [(str(v.get("year")), v) for v in years if isinstance(v, dict) and v.get("year") is not None]
    else:
        items = []
    return sorted(
        (AnnualReturn(year, _number(v, "amount"), _number(v, "percentage")) for year, v in items),
        key=lambda r: r.year,
    )


def _number(value: Any, key: str) -> Decimal | None:
    raw = value.get(key) if isinstance(value, dict) else None
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return None
