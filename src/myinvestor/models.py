"""Typed views over MyInvestor responses.

Amounts are euros (already `Decimal`, see `envelope.decode`). Dates come as ISO datetimes at
midnight (`2026-11-02T00:00:00.000+0000`); only the calendar day matters, so they become `datetime.date`.
Unknown fields are ignored: the API adds fields often and the CLI only needs a stable subset.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


def _day(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return value


def _day_or_none(value: Any) -> Any:
    """Like `_day`, but some endpoints send `""` for a missing date: that is None."""
    if isinstance(value, str) and not value.strip():
        return None
    return _day(value)


def _currency_code(value: Any) -> Any:
    """The fund detail sends `currency` as an object (`{"code": "EUR", …}`); a bare code also works.

    Anything else is None: the currency is informative and must never break `allocation` or `holdings --ter`.
    """
    if isinstance(value, dict):
        value = value.get("code")
    return value if isinstance(value, str) and value.strip() else None


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True)


# Known `participantType` values of a cash-account holder.
OWNER = "OWNER"  # the account holder (own accounts and CASH_PORTFOLIO)
REPRESENTATIVE = "REPRESENTATIVE"  # a parent on a minor's account
JUNIOR = "JUNIOR"  # the minor on a junior account

# Characters of the sha256 hex digest kept in the id of a movement without `reference`.
FLOW_HASH_LENGTH = 16


class Ownership(StrEnum):
    """Whose a cash account is, from the logged-in user's point of view."""

    me = "me"  # I am an OWNER of it
    junior = "junior"  # a minor's account (it has a JUNIOR holder)
    other = "other"  # anything else (e.g. I only represent or am authorised on it)


class Holder(_Model):
    """A holder of a cash account. The API also sends `documentNumber` (the ID card): never parsed."""

    me: bool = False
    participant_type: str | None = None
    name: str | None = None
    surname1: str | None = None
    surname2: str | None = None

    @property
    def full_name(self) -> str | None:
        return " ".join(p for p in (self.name, self.surname1, self.surname2) if p) or None


class CashAccount(_Model):
    account_id: str
    account_uuid: str
    alias: str | None = None
    iban: str | None = None
    account_type: str | None = None
    status: str | None = None
    currency: str = "EUR"
    enabled_balance: Decimal = Decimal(0)
    withheld_balance: Decimal = Decimal(0)
    holders: list[Holder] = Field(default_factory=list)

    @property
    def balance(self) -> Decimal:
        return self.enabled_balance + self.withheld_balance

    @property
    def my_role(self) -> str | None:
        """`participantType` of the logged-in user on this account."""
        return next((h.participant_type for h in self.holders if h.me), None)

    @property
    def main_holder(self) -> Holder | None:
        """Whose money it is: the minor on a junior account, else the OWNER (me first if joint)."""
        for kind in (JUNIOR, OWNER):
            hits = sorted((h for h in self.holders if h.participant_type == kind), key=lambda h: not h.me)
            if hits:
                return hits[0]
        return None

    @property
    def ownership(self) -> Ownership:
        if any(h.participant_type == JUNIOR for h in self.holders):
            return Ownership.junior
        if any(h.me and h.participant_type == OWNER for h in self.holders):
            return Ownership.me
        return Ownership.other


class Flow(_Model):
    """A cash-account movement. `amount` is signed: charges are negative."""

    reference: str | None = None
    operation_date: dt.date
    value_date: dt.date | None = None
    amount: Decimal
    currency: str = "EUR"
    flow_type: str | None = None  # IN | OUT
    concept: str | None = None
    operation_type: str | None = None
    total_balance: Decimal | None = None
    operation_class: str | None = None
    operation_origin_label: str | None = None
    operation_target_label: str | None = None
    operation_ref: str | None = None  # fixed-width; shared by both legs of a transfer
    file_ref: str | None = None
    is_migrated: bool | None = None

    _days = field_validator("operation_date", "value_date", mode="before")(_day)

    @property
    def outgoing(self) -> bool:
        return self.flow_type == "OUT" if self.flow_type else self.amount < 0

    @property
    def counterparty(self) -> str | None:
        """The payee on charges and the payer on credits, when the API labels them."""
        label = self.operation_target_label if self.outgoing else self.operation_origin_label
        return (label or "").strip() or None

    def stable_id(self, account_uuid: str) -> str:
        """Id of this movement, the same on every run and date range (before de-duplication, see `stable_ids`).

        `reference` is a per-account counter, so it is qualified with the account. Without it, a hash of
        the fields that describe the movement (not the running balance, which is not part of it).
        """
        if self.reference:
            return f"{account_uuid}:{self.reference}"
        fields = (
            account_uuid,
            self.operation_date.isoformat(),
            self.value_date.isoformat() if self.value_date else "",
            _canonical(self.amount),
            self.concept or "",
            self.operation_type or "",
            self.operation_ref or "",
        )
        digest = hashlib.sha256("|".join(fields).encode()).hexdigest()
        return f"{account_uuid}:h{digest[:FLOW_HASH_LENGTH]}"

    def _tiebreak(self) -> tuple[str, ...]:
        """Remaining fields, to number identical movements without depending on the list order."""
        return tuple(
            "" if v is None else str(v)
            for v in (
                self.total_balance,
                self.file_ref,
                self.flow_type,
                self.operation_class,
                self.operation_origin_label,
                self.operation_target_label,
                self.currency,
                self.is_migrated,
            )
        )


def _canonical(amount: Decimal) -> str:
    """`-12.50` and `-12.5` are the same amount."""
    return format(amount.normalize(), "f")


def stable_ids(account_uuid: str, flows: Sequence[Flow]) -> list[str]:
    """`Flow.stable_id` of each flow, with `#2`, `#3`… on movements that would otherwise share an id.

    Identical movements are numbered by the fields outside the id (running balance first), so the
    result does not depend on the order of `flows`. Assuming the API filters by operation date, a range
    covers whole days, so every range that includes the day sees the whole group; only a new identical
    movement booked later the same day can renumber its group.
    """
    groups: defaultdict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(flows):
        groups[f.stable_id(account_uuid)].append(i)
    ids = [""] * len(flows)
    for base, members in groups.items():
        for n, i in enumerate(sorted(members, key=lambda i: flows[i]._tiebreak()), start=1):
            ids[i] = base if n == 1 else f"{base}#{n}"
    return ids


class Hold(_Model):
    """Money withheld on a cash account (e.g. for a pending fund order).

    `amount` is always negative, like a charge in the movements: it is money out of `enabledBalance`.
    The API sends it negative; a response with it positive is normalised.
    """

    id: str
    account_id: str | None = None
    type: str | None = None
    amount: Decimal
    currency: str = "EUR"

    @field_validator("amount")
    @classmethod
    def _outgoing(cls, value: Decimal) -> Decimal:
        return -abs(value)


class SentTransfer(_Model):
    """A transfer sent from a cash account (`…/cash-accounts/{accountId}/transfers`).

    `amount` is positive, as the API sends it. The API also sends `sender` (name and ID card of
    who sent it): never parsed.
    """

    reference: str  # 15 digits; the operation id inside the charge's `operationRef` (positions 28-42)
    account_id: str | None = None
    account_alias: str | None = None
    amount: Decimal
    currency: str = "EUR"
    concept: str | None = None
    emission_date: dt.date | None = None
    start_date: dt.date | None = None  # scheduled and periodic transfers
    end_date: dt.date | None = None
    periodicity: str | None = None  # WEEKLY | BIWEEKLY | MONTHLY | BIMONTHLY | QUARTERLY | BIANNUAL | ANNUAL
    name_destination: str | None = None
    iban_destination: str | None = None
    bank: str | None = None
    bic_swift: str | None = None
    country_id_destination: str | None = None
    transfer_type: str | None = None  # PUNCTUAL (the only value seen)
    execution_type: str | None = None  # STANDARD | INSTANT
    status: str | None = None  # COMMITED (sic) | ACTIVE | PENDING | ACCEPTED_SETTLEMENT_IN_PROGRESS | …
    cancelable: bool | None = None

    _days = field_validator("emission_date", "start_date", "end_date", mode="before")(_day_or_none)


class Remuneration(_Model):
    """Last interest settlement of a cash account (`…/cash-accounts/{accountId}/remuneration`).

    Rates are percentages (`2.5` is 2.5 %), as the web shows them. Per the web's own mapping,
    `final_result` is the gross interest of the period and `tax_charge_amount` the withholding on it
    (the web shows `final_result - tax_charge_amount` as the net interest).
    """

    remuneration_date: dt.date | None = None  # the month settled (its first day)
    payment_date: dt.date | None = None
    official_remuneration_date: dt.date | None = None
    creation_update_date: dt.date | None = None
    remuneration_type: str | None = None
    remuneration_percentage: Decimal | None = None
    base_remuneration_percentage: Decimal | None = None
    promotion_remuneration_percentage: Decimal | None = None
    tae_promotion: Decimal | None = None
    calculate_tae_average: Decimal | None = None
    applicable_base_balance: Decimal | None = None
    balance_promo: Decimal | None = None
    total_balance: Decimal | None = None
    base_results: Decimal | None = None
    promo_results: Decimal | None = None
    final_result: Decimal | None = None
    tax_charge_amount: Decimal | None = None
    end_date_remuneration: dt.date | None = None
    promotion_code: str | None = None

    _days = field_validator(
        "remuneration_date",
        "payment_date",
        "official_remuneration_date",
        "creation_update_date",
        "end_date_remuneration",
        mode="before",
    )(_day_or_none)

    @property
    def has_breakdown(self) -> bool:
        """False for the «empty» row: every balance and result 0 or missing.

        The web only paints balances and interests («Tu remuneración») for promotion types
        (`BALANCE_INCREASE`, `…_PROMOTION`); a `DEFAULT` account gets the row with zeros, which is
        not its settlement (that one is in the movements, «LIQUIDAC. INTERESES»).
        """
        amounts = (
            self.applicable_base_balance,
            self.balance_promo,
            self.total_balance,
            self.base_results,
            self.promo_results,
            self.final_result,
            self.tax_charge_amount,
        )
        return any(a for a in amounts)


class Investment(_Model):
    """A fund, ETF or stock held in a securities account."""

    account_id: str | None = None
    investment_product_type: str | None = None  # INDEXED_FUND | BROKER | …
    broker_product_type: str | None = None  # on BROKER positions: ETF | STOCK | …
    isin: str | None = None
    ticker: str | None = None
    investment_name: str | None = None
    shares: Decimal | None = None
    available_shares: Decimal | None = None
    average_cost: Decimal | None = None
    initial_investment: Decimal | None = None
    market_value: Decimal | None = None
    profit: Decimal | None = None
    total_return: Decimal | None = None  # percent
    ytd_total_return: Decimal | None = None
    liquidation_value: Decimal | None = None
    liquidation_value_date: dt.date | None = None
    liquidation_value_currency: str | None = None
    my_investor_category: str | None = None
    status: str | None = None

    _days = field_validator("liquidation_value_date", mode="before")(_day)


class InvestmentGroup(_Model):
    product_num: int = 0
    total_amount: Decimal = Decimal(0)
    investment_list: list[Investment] = Field(default_factory=list)


class SecuritiesAccount(_Model):
    account_id: str
    cod_account: str | None = None
    external_cod_account: str | None = None
    cash_account_id: str | None = None
    account_type: str | None = None
    status: str | None = None
    portfolio_name: str | None = None  # managed portfolios (`SECURITIES_PORTFOLIO`): catalog `name`, `METAL`
    portfolio_profile_type: str | None = None  # managed portfolios: `VERY_AGGRESSIVE`…
    amount: Decimal | None = None
    market_value: Decimal | None = None
    total_invested: Decimal | None = None
    profit: Decimal | None = None
    total_return: Decimal | None = None
    securities_account_investments: dict[str, InvestmentGroup] = Field(default_factory=dict)

    @property
    def investments(self) -> list[Investment]:
        return [inv for group in self.securities_account_investments.values() for inv in group.investment_list]


class PensionAccount(_Model):
    account_id: str
    external_account_code: str | None = None
    cash_account_id: str | None = None
    active: bool = True
    plans_number: int | None = None
    amount_account: Decimal | None = None
    market_value: Decimal | None = None
    total_invested: Decimal | None = None
    profit: Decimal | None = None
    total_return: Decimal | None = None


class PensionPlanPosition(_Model):
    account_pension_id: int | str | None = None
    dgs_code: str
    name: str | None = None
    shares: Decimal | None = None
    average_cost: Decimal | None = None
    total_invested: Decimal | None = None
    market_value: Decimal | None = None
    profit: Decimal | None = None
    total_return: Decimal | None = None
    liquidation_value: Decimal | None = None
    liquidation_value_date: dt.date | None = None
    currency: str = "EUR"
    status: str | None = None

    _days = field_validator("liquidation_value_date", mode="before")(_day)


class Order(_Model):
    """A fund/stock order (securities account) or a contribution order (pension account)."""

    reference: str
    order_date: dt.date
    securities_account_id: str | None = None
    pension_account_id: str | None = None
    cod_account: str | None = None
    isin: str | None = None
    dgs_code: str | None = None
    ticker: str | None = None
    fund_name: str | None = None
    operation_type: str | None = None
    order_type: str | None = None
    status: str | None = None
    cash: Decimal | None = None
    shares: Decimal | None = None
    currency: str = "EUR"
    product_type: str | None = None
    market: str | None = None
    effective_date: dt.date | None = None
    expiration_date: dt.date | None = None
    is_cancelable: bool | None = None
    amount_to_block: Decimal | None = None  # what a fund buy withholds on the cash account

    _days = field_validator("order_date", "effective_date", "expiration_date", mode="before")(_day)

    @property
    def account_id(self) -> str | None:
        return self.securities_account_id or self.pension_account_id

    @property
    def product(self) -> str | None:
        return self.isin or self.dgs_code or self.ticker


class PositionPoint(_Model):
    """One day of a position's history (`…/positions?groupType=…`)."""

    date: dt.date
    market_value: Decimal | None = None
    investment_added_value: Decimal | None = None
    return_value: Decimal | None = None
    total_shares: Decimal | None = None
    avg_position_cost: Decimal | None = None

    _days = field_validator("date", mode="before")(_day)


class NavPoint(_Model):
    """One NAV of a catalog fund (`…/funds/{isin}/graphs/INIT_GRAPH`).

    The API also sends `profit`, equal to `marketValue` in every point observed: not a gain, never parsed.
    """

    position_date: dt.date
    market_value: Decimal | None = None  # the NAV

    _days = field_validator("position_date", mode="before")(_day)


class ContributionTimeFrame(_Model):
    recurrence: str | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    next_execution_date: dt.date | None = None

    _days = field_validator("start_date", "end_date", "next_execution_date", mode="before")(_day)


class Contribution(_Model):
    """A periodic contribution ("aportación automática") to a fund or pension plan."""

    contribution_id: str
    alias: str | None = None
    amount: Decimal
    currency: str = "EUR"
    status: str | None = None
    isin: str | None = None
    fund_name: str | None = None
    contribution_type: str | None = None
    subscription_type: str | None = None
    to_account_type: str | None = None
    from_account_id: str | None = None  # `accountId` of the cash account it is charged to
    from_account_alias: str | None = None
    contribution_time_frame: ContributionTimeFrame | None = None


class Profitability(_Model):
    ytd: Decimal | None = None
    one_year: Decimal | None = None
    three_year: Decimal | None = None
    five_year: Decimal | None = None
    from_beginning: Decimal | None = None


class FundSummary(_Model):
    """A row of the fund catalog (`/cperf-server/api/v2/funds`)."""

    isin: str
    name: str | None = None
    managing_entity: str | None = None
    category: str | None = None
    currency: str | None = None
    ter: Decimal | None = None
    risk_rating: int | None = None
    asset_type: str | None = None
    geographical_area: str | None = None
    management_type: str | None = None
    dividend_type: str | None = None
    liquidation_value: Decimal | None = None
    liquidation_value_date: dt.date | None = None
    annualized_profitability: Profitability | None = None

    _days = field_validator("liquidation_value_date", mode="before")(_day)


class PensionPlanSummary(_Model):
    """A row of the pension-plan catalog (`/cperf-server/api/v2/pension-plans`); any key may be null."""

    dgs_code: str | None = None
    name: str | None = None
    managing_entity: str | None = None
    category: str | None = None
    currency: str | None = None
    ter: Decimal | None = None
    risk_rating: int | None = None
    morningstar_rating: int | None = None
    asset_type: str | None = None
    liquidation_value: Decimal | None = None
    annualized_profitability: Profitability | None = None


class FundSheet(_Model):
    """The part of a fund's detail (`/cperf-server/api/v2/funds/{isin}`) that classifies it."""

    isin_code: str | None = None
    name: str | None = None
    product_type: str | None = None
    asset_type: str | None = None  # VARIABLE_INCOME | FIXED_INCOME | MONEY_MARKET | COMMODITIES | MIXED | …
    geo_zone: str | None = None  # GLOBAL | EUROPE | UNITED_STATES | EMERGENT_MARKETS | …
    category: str | None = None
    myinvestor_category: str | None = None
    ter: Decimal | None = None  # percent
    currency: str | None = None  # of the NAV (`EUR`, `USD`…)

    _currency = field_validator("currency", mode="before")(_currency_code)


def _empty_list(value: Any) -> Any:
    """`null` where the API means "none yet" (e.g. the executions of an order in progress)."""
    return [] if value is None else value


class Commission(_Model):
    """A fee of a fund's or plan's detail (`commissionList[]`): its name and percentage."""

    name: str | None = None
    percentage: Decimal | None = None


class PensionPlanSheet(_Model):
    """A pension plan's detail (`/cperf-server/api/v2/pension-plans/{dgs}`): what classifies it and its fees.

    The web never reads a `ter` there (only the catalog rows have one): it is kept if it comes.
    """

    dgs_code: str | None = None
    name: str | None = None
    category: str | None = None
    plan_type: str | None = None
    active_type: str | None = None  # "Tipo de activo" on the web
    management_entity: str | None = None
    ter: Decimal | None = None
    annualized_profitability: Profitability | None = None
    commission_list: list[Commission] = Field(default_factory=list)

    _commissions = field_validator("commission_list", mode="before")(_empty_list)


class FeedItem(_Model):
    type: str | None = None
    date: dt.date | None = None
    product: str | None = None
    title: str | None = None
    body: str | None = None

    _days = field_validator("date", mode="before")(_day)


def _euros(value: Any) -> Any:
    return "EUR" if value is None else value


class OrderExecution(_Model):
    """One execution (`relatedOperations[]`) of an order: what was really bought or sold, and at what NAV.

    Gross amount, commissions and withholdings are in the fund's currency; `net_amount_eur` in euros.
    """

    operation_number: str | None = None
    execution_date: dt.date | None = None
    executed_shares: Decimal | None = None
    liquidation_value: Decimal | None = None
    gross_amount_operation_fund_currency: Decimal | None = None
    commissions: Decimal | None = None
    origin_retention: Decimal | None = None
    destiny_retention: Decimal | None = None
    net_amount_fund_currency: Decimal | None = None
    exchange_rate: Decimal | None = None
    net_amount_eur: Decimal | None = Field(None, alias="netAmountEUR")
    net_amount_account_currency: Decimal | None = None
    tax_result: Decimal | None = None  # realised gain (+) or loss (-) of a sale

    _days = field_validator("execution_date", mode="before")(_day)

    @property
    def retentions(self) -> Decimal | None:
        """Withholding at origin plus at destination (`None` when the API sends neither)."""
        parts = [r for r in (self.origin_retention, self.destiny_retention) if r is not None]
        return sum(parts, Decimal(0)) if parts else None


class OrderDetail(_Model):
    """An order with its executions (`…/{securities,pension}-accounts/{id}/orders/{reference}`).

    The API also sends `ordererCustomer` (the ID card) and `accountNumber`: never parsed.
    On a pension order `isin` holds the plan's DGS code.
    """

    order_reference: str
    isin: str | None = None
    order_status: str | None = None  # INITIAL | PENDING | IN_PROGRESS | COMPLETE | CANCEL | REJECTED
    order_date: dt.date | None = None
    order_type_name: str | None = None
    order_class: str | None = None
    operation_type: str | None = None
    gross_amount_eur: Decimal | None = Field(None, alias="grossAmountEUR")
    gross_amount_fund_currency: Decimal | None = None
    cash: Decimal | None = None
    shares: Decimal | None = None
    fund_name: str | None = None
    currency: str = "EUR"
    expiration_date: dt.date | None = None
    periodicity_code: str | None = None
    related_operations: list[OrderExecution] = Field(default_factory=list)  # `null` while in progress

    _days = field_validator("order_date", "expiration_date", mode="before")(_day)
    _lists = field_validator("related_operations", mode="before")(_empty_list)
    _currency = field_validator("currency", mode="before")(_euros)


class FundLot(_Model):
    """An open lot of a fund (FIFO): the shares of one subscription still held, with its latent gain.

    `fiscal_date` is the lot's tax date: after a transfer between funds it is the date inherited from
    the source fund, not the date of the transfer.
    """

    operation_type: str | None = None  # SUBSCRIPTION
    operation_desc: str | None = None  # "SUSCRIPCIÓN FONDOS INVERSIÓN", "ALTA IIC SWITCH", "SUSC.TRASPASO. INT."…
    operation_num: str | None = None
    fiscal_date: dt.date | None = None
    securities: Decimal | None = None
    net_asset_value: Decimal | None = None
    investment: Decimal | None = None
    market_value: Decimal | None = None
    plus_minus: Decimal | None = None
    fiscal_result: Decimal | None = None

    _days = field_validator("fiscal_date", mode="before")(_day)

    @property
    def gain(self) -> Decimal | None:
        return self.fiscal_result if self.fiscal_result is not None else self.plus_minus


class FundLots(_Model):
    """Open lots of a fund (`…/investment-summary/funds/{isin}/added-values`): latent, not realised, gains."""

    currency: str = "EUR"
    fund_name: str | None = None
    investment_added_value: Decimal | None = None
    added_value: Decimal | None = None
    plus_minus_list: list[FundLot] = Field(default_factory=list)

    _lists = field_validator("plus_minus_list", mode="before")(_empty_list)
    _currency = field_validator("currency", mode="before")(_euros)


def _text(value: Any) -> Any:
    """A reference the API may send as a number: keep it as text."""
    return str(value) if isinstance(value, int | Decimal) and not isinstance(value, bool) else value


class FundTransfer(_Model):
    """A fund transfer of a securities account (`…/securities-accounts/{id}/transfers`).

    Never seen with data (the accounts observed answered `[]`): the fields are the web's (bundle),
    so all but `reference` are optional.
    """

    reference: str
    isin_origin: str | None = None
    isin_destination: str | None = None
    status: str | None = None
    transfer_date: dt.date | None = None
    shares: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = None
    is_cancelable: bool | None = None

    _reference = field_validator("reference", mode="before")(_text)
    _days = field_validator("transfer_date", mode="before")(_day_or_none)


class PensionTransfer(_Model):
    """A pension-plan transfer of a pension account (`…/pension-accounts/{id}/transfers`), as `FundTransfer`."""

    reference: str
    dgs_code_origin: str | None = None
    fund_name_origin: str | None = None
    dgs_code_destination: str | None = None
    pension_plan_name_destination: str | None = None
    status: str | None = None
    transfer_date: dt.date | None = None
    shares: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = None
    is_cancelable: bool | None = None

    _reference = field_validator("reference", mode="before")(_text)
    _days = field_validator("transfer_date", mode="before")(_day_or_none)


class PendingTransfers(_Model):
    """`…/securities-accounts/{id}/transfers/pending`: how many fund transfers are still in progress."""

    pending_transfers: int
