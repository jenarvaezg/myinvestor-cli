"""`--fiscal` and the requests behind `coste_fiscal`: the open lots of each fund and the plans' yearly history."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Annotated

import typer

from myinvestor.allocation import is_fund
from myinvestor.client import MyInvestorClient, Period
from myinvestor.fiscal import lots_cost, strict_total
from myinvestor.models import Investment, PensionAccount, PensionPlanPosition, SecuritiesAccount
from myinvestor.plan_costs import PlanCosts, rebuild

FiscalOpt = Annotated[
    bool,
    typer.Option(
        "--fiscal",
        help="Añadir coste_fiscal (coste de adquisición heredado en los traspasos), ganancia_real y "
        "rentab_real: una petición más por fondo y otra por cuenta de pensiones.",
    ),
]

FundCosts = Mapping[tuple[str, str], Decimal | None]  # (securities account id, ISIN) → tax cost


def fund_costs(
    client: MyInvestorClient, positions: Iterable[tuple[str, Investment]]
) -> dict[tuple[str, str], Decimal | None]:
    """Tax cost of each fund held, from its open lots: one request per fund and account."""
    held = dict.fromkeys((account_id, inv.isin) for account_id, inv in positions if is_fund(inv) and inv.isin)
    return {(account_id, isin): lots_cost(client.fund_lots(account_id, isin)) for account_id, isin in held}


def investment_cost(account_id: str, inv: Investment, costs: FundCosts) -> Decimal | None:
    """A fund's, from its lots; a stock's or an ETF's is `invertido` (they are never transferred)."""
    if not is_fund(inv):
        return inv.initial_investment
    return costs.get((account_id, inv.isin or ""))


def account_cost(sec: SecuritiesAccount, costs: FundCosts) -> Decimal | None:
    return strict_total(investment_cost(sec.account_id, inv, costs) for inv in sec.investments)


def plan_costs(client: MyInvestorClient, accounts: Iterable[PensionAccount]) -> dict[str, PlanCosts]:
    """Rebuilt tax costs of each pension account's plans, from a year of history: one request per account."""
    return {a.account_id: rebuild(client.position_history(a.account_id, Period.year, pension=True)) for a in accounts}


def plan_cost(account_id: str, plan: PensionPlanPosition, costs: Mapping[str, PlanCosts]) -> Decimal | None:
    found = costs.get(account_id)
    return found.cost(plan.dgs_code, plan.total_invested) if found else None


def pension_cost(
    account_id: str, plans: Sequence[PensionPlanPosition], costs: Mapping[str, PlanCosts]
) -> Decimal | None:
    return strict_total(plan_cost(account_id, p, costs) for p in plans)
