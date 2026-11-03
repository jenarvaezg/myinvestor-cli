"""The JSON contract: every command's `-f json` output against `json_contract.CONTRACTS` (synthetic data).

A failure here means the keys or types of a command's JSON changed. Removing or renaming a key, or
changing its type or whether it can be null, is incompatible: bump the schema version in
src/myinvestor/json_contract.py (`/1` → `/2`) and note it in CHANGELOG.md. Adding a key is
compatible: add it to the table. `UPDATE_JSON_CONTRACT=1 uv run pytest tests/test_json_contract.py`
then rewrites json_contract.lock.json (the published schemas) and the generated part of
docs/json.md; it never rewrites a published schema with a breaking change.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
import typer
from typer.testing import CliRunner

from myinvestor import cli, cli_common, cli_upcoming, json_envelope
from myinvestor.cli import app
from myinvestor.cli_common import package_version
from myinvestor.errors import InputError
from myinvestor.json_contract import (
    CONTRACTS,
    ENVELOPE,
    ERROR_LINE,
    Contract,
    Field,
    JsonType,
    Shape,
    Variant,
    contract_for,
)
from myinvestor.json_schema import TYPE_LABELS, json_schema, markdown_reference, schema_index
from myinvestor.output import AMOUNT_COLUMNS
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CASH_UUID, CPERF, cash_account, flow, investment, ok, order, securities_account
from tests.test_cli_fund_transfers import fund_transfer, pension_transfer
from tests.test_cli_gains import (
    added_values,
    etc_trades,
    execution,
    fund_lots,
    gains_api,
    lot,
    mock_broker,
    mock_holdings,
    order_detail,
)
from tests.test_cli_transfers import JUNIOR_UUID, STAMP, junior_flows, own_flows
from tests.test_cli_upcoming import NOW, mock_everything
from tests.test_documents import document
from tests.test_sent_transfers import sent
from tests.test_transfers_match import sepa_ref

runner = CliRunner()
LOCK = Path(__file__).with_name("json_contract.lock.json")
DOCS = Path(__file__).resolve().parent.parent / "docs" / "json.md"
DOCS_BEGIN = "<!-- BEGIN: generado de src/myinvestor/json_contract.py; no editar a mano -->"
DOCS_END = "<!-- END: generado -->"
UPDATE = os.environ.get("UPDATE_JSON_CONTRACT") == "1"
UPDATE_HINT = "UPDATE_JSON_CONTRACT=1 uv run pytest tests/test_json_contract.py"
BUMP = (
    "Quitar o renombrar una clave, o cambiar su tipo o si admite null, es un cambio incompatible: sube la "
    "versión del esquema en src/myinvestor/json_contract.py (myinvestor.<orden>/1 → /2) y anótalo en "
    "CHANGELOG.md. Añadir una clave es compatible: añádela a la tabla (y al CHANGELOG)."
)
FUND, STOCK = "IE0000000001", "US0000000004"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---- synthetic API: every field filled ---------------------------------------------------------

ANA = {"me": True, "participantType": "OWNER", "name": "Ana", "surname1": "Ejemplo"}
LEO = {"me": False, "participantType": "JUNIOR", "name": "Leo", "surname1": "Ejemplo"}
INTEREST = flow(
    8,
    3.21,
    reference="000261",
    concept="LIQUIDAC. INTERESES PERIODO 17/09/2026 17/10/2026",
    operationType="LIQUIDAC. INTERESES",
    operationOriginLabel="MyInvestor",
    operationTargetLabel="Mi cuenta",
    operationRef=sepa_ref("000261"),
    fileRef="F0001",
    operationDate=STAMP,
    valueDate=STAMP,
)
# the charge of an own transfer (`000259`, paired), with every optional field: what `--id` shows
LABELLED_TRANSFER = {
    **own_flows()[0],
    "operationOriginLabel": "Mi cuenta",
    "operationTargetLabel": "Junior",
    "fileRef": "F0002",
}
PLAN = {
    "dgsCode": "N0001",
    "name": "Plan",
    "shares": 10.5,
    "averageCost": 57.14,
    "totalInvested": 600.0,
    "marketValue": 700.0,
    "profit": 100.0,
    "totalReturn": 16.67,
    "liquidationValue": 66.666667,
    "liquidationValueDate": "2026-10-30T00:00:00.000Z",
}
POINT = {
    "date": "2026-10-01",
    "marketValue": 1100.5,
    "investmentAddedValue": 1000.0,
    "returnValue": 10.05,
    "totalShares": 12.345678,
    "avgPositionCost": 81.0,
}
CONTRIBUTION = {
    "contributionId": "c1",
    "amount": 100,
    "status": "ACTIVE",
    "isin": FUND,
    "fundName": "Fondo indexado",
    "toAccountType": "SECURITIES_ACCOUNT",
    "contributionTimeFrame": {"recurrence": "ONE_MONTH", "nextExecutionDate": "2026-11-05"},
}
CATALOG_ROW = {
    "isin": FUND,
    "name": "Fondo indexado",
    "managingEntity": "Gestora",
    "category": "RV Global",
    "ter": 0.12,
    "riskRating": 4,
    "liquidationValue": 89.15,
    "currency": "EUR",
    "annualizedProfitability": {"ytd": 9.1, "oneYear": 12.3},
}
PLAN_CATALOG_ROW = {
    "dgsCode": "N0001",
    "name": "Plan",
    "managingEntity": "Gestora",
    "category": "RV Global",
    "ter": 0.3,
    "annualizedProfitability": {"ytd": 8.5, "oneYear": 11.2, "threeYear": 7.1, "fiveYear": 6.4},
}
SHEET = {
    "isinCode": FUND,
    "name": "Fondo indexado",
    "assetType": "VARIABLE_INCOME",
    "geoZone": "GLOBAL",
    "category": "RV Global",
    "ter": 0.12,
    "currency": {"code": "EUR", "symbol": "€"},  # the real shape: an object
}

# a periodic transfer already emitted once and still running, so every key of `transfers` is filled
PERIODIC = sent(
    reference="120261001000001",
    status="ACTIVE",
    periodicity="MONTHLY",
    startDate="2026-10-05",
    endDate="2099-12-31",
    emissionDate="2026-10-05T00:00:00.000+0000",
)
REMUNERATION = {
    "remunerationDate": "2026-09-01T00:00:00.000+0000",
    "paymentDate": "2026-10-18",  # the day of the INTEREST movement, so `liquidacion_*` is crossed
    "officialRemunerationDate": "2026-10-01",
    "remunerationType": "DEFAULT",
    "remunerationPercentage": 2.5,
    "baseRemunerationPercentage": 2.0,
    "promotionRemunerationPercentage": 0.5,
    "taePromotion": 2.53,
    "calculateTaeAverage": 2.52,
    "applicableBaseBalance": 10000,
    "balancePromo": 5000,
    "totalBalance": 15000,
    "baseResults": 16.44,
    "promoResults": 2.05,
    "finalResult": 18.49,
    "taxChargeAmount": 3.51,
    "endDateRemuneration": "2026-12-31",
    "promotionCode": "PROMO",
    "snapshotDate": "",
}


def full_api(mock_api: respx.MockRouter, _monkeypatch: pytest.MonkeyPatch) -> None:
    """Two cash accounts with an own transfer, a fund and a stock, a pension plan, a contribution…"""
    accounts = [cash_account(1, alias="Mi cuenta", holders=[ANA]), cash_account(2, alias="Junior", holders=[LEO, ANA])]
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok(accounts))
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/flows").respond(
        200, json=ok({"flowList": [LABELLED_TRANSFER, *own_flows()[1:], INTEREST]})
    )
    mock_api.get(f"{API}/account/api/v3/account/{JUNIOR_UUID}/flows").respond(
        200, json=ok({"flowList": junior_flows()})
    )
    hold = {"id": "h1", "type": "INVESTMENT_ORDER", "amount": 25.0}
    mock_api.get(f"{API}/account/api/v3/account/{CASH_UUID}/holds").respond(200, json=ok([hold]))
    mock_api.get(f"{API}/account/api/v3/account/{JUNIOR_UUID}/holds").respond(200, json=ok([]))
    mock_api.get(f"{CPERF}/v2/cash-accounts/historical-balance").respond(
        200, json=ok([{"date": "2026-11-01T00:00:00.000Z", "balance": 1200.5}])
    )
    securities = securities_account(
        "201",
        portfolioName="Cartera",
        cashAccountId="101",
        securitiesAccountInvestments={
            "INDEXED_FUND": {"investmentList": [investment(FUND)]},
            "BROKER": {"investmentList": [investment(STOCK, "BROKER", marketValue=50.0, brokerProductType="RV")]},
        },
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(200, json=ok(securities))
    mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(200, json=ok([order("o1", "2026-11-02")]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/positions").respond(200, json=ok({"positions": {FUND: [POINT]}}))
    pension = {"accountId": "301", "externalAccountCode": "P301", "marketValue": 700.0, "totalInvested": 600.0}
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(
        200, json=ok([{**pension, "profit": 100.0, "totalReturn": 16.67}])
    )
    mock_api.get(f"{CPERF}/v2/pension-accounts/301").respond(200, json=ok([PLAN]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/positions").respond(200, json=ok({"positions": {"N0001": [POINT]}}))
    mock_api.get(f"{CPERF}/v2/pension-plans/N0001").respond(
        200,
        json=ok(
            {
                "dgsCode": "N0001",
                "activeType": "RENTA VARIABLE",
                "category": "RV MIXTA",
                "commissionList": [
                    {"name": "Comisión de gestión", "percentage": 0.25},
                    {"name": "Comisión de depósito", "percentage": 0.05},
                ],
            }
        ),
    )
    mock_api.get(f"{CPERF}/v2/automatic-contributions/self").respond(200, json=ok([CONTRIBUTION]))
    mock_api.get(f"{CPERF}/v2/funds").respond(200, json=ok([CATALOG_ROW], pagination={"totalElements": 1}))
    mock_api.get(f"{CPERF}/v2/funds/{FUND}").respond(200, json=ok(SHEET))
    for path in ("categories", "managers", "currencies", "assets-type", "regions"):
        mock_api.get(f"{CPERF}/v2/masters/funds/searcher/filters/{path}").respond(200, json=ok(["\t Valor", None]))
    mock_api.get(f"{CPERF}/v2/pension-plans").respond(200, json=ok([PLAN_CATALOG_ROW], pagination={"totalElements": 1}))
    mock_api.get(f"{CPERF}/v2/funds/{FUND}/graphs/INIT_GRAPH").respond(
        200, json=ok({"investmentPositionValueList": [{"positionDate": "2026-11-02", "marketValue": 89.15}]})
    )
    mock_api.get(f"{CPERF}/v2/funds/{FUND}/prices/last").respond(
        200, json=ok({"date": "2026-11-02", "price": 89.15, "currency": "EUR", "dayProfitPercentage": 0.2})
    )
    feed = {"type": "ORDER", "date": "2026-11-01T09:00:00.000Z", "product": FUND, "title": "Compra", "body": "Hecha"}
    mock_api.get(f"{API}/feed/api/feed/self").respond(200, json=ok({"feed": [feed]}))
    mock_api.get(f"{API}/feed/api/x").respond(200, json=ok([1]))
    mock_api.get(f"{CPERF}/v3/customers/self").respond(200, json=ok({"name": "Ana"}))
    mock_api.get(f"{CPERF}/v2/cash-accounts/101/transfers").respond(200, json=ok([sent(), PERIODIC]))
    mock_api.get(f"{CPERF}/v2/cash-accounts/102/transfers").respond(200, json=ok([]))
    mock_api.get(f"{CPERF}/v2/cash-accounts/101/remuneration").respond(200, json=ok(REMUNERATION))
    mock_api.get(f"{CPERF}/v2/cash-accounts/102/remuneration").respond(200, json=ok(REMUNERATION))


def documents_api(mock_api: respx.MockRouter, _monkeypatch: pytest.MonkeyPatch) -> None:
    """A tax document without account and a cash-account report with one."""
    fiscal = document("dddddddd-0000-0000-0000-000000000001", "INFORMACIÓN FISCAL 2025")
    report = document("dddddddd-0000-0000-0000-000000000002", "Costes y gastos 2025", accountCode="C101")
    mock_api.get(f"{CPERF}/v2/customers/self/documents/fiscal").respond(200, json=ok([fiscal]))
    mock_api.get(f"{CPERF}/v2/cash-accounts/self/documents").respond(200, json=ok([report]))
    for path in ("securities-accounts", "pension-accounts", "portfolios"):
        mock_api.get(f"{CPERF}/v2/{path}/self/documents").respond(200, json=ok([]))


def upcoming_api(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    """A contribution the account cannot cover, so `falta` is filled."""
    monkeypatch.setattr(cli_upcoming, "now_local", lambda: NOW)
    mock_everything(mock_api, contribution_amount=130.0)


TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
# a saved snapshot of yesterday that saw none of today's movements, so every key of `delta` is filled
PREVIOUS_SNAPSHOT = {
    "schema": "myinvestor.snapshot/1",
    "as_of": f"{YESTERDAY.isoformat()}T09:00:00+01:00",
    "total": 2000.0,
    "cash": [{"id": "101", "alias": "Mi cuenta", "saldo": 500.0}, {"id": "102", "alias": "Junior", "saldo": 50.0}],
    "securities": [{"id": "201", "nombre": "Cartera", "valor": 800.0, "invertido": 750.0}],
    "pensions": [{"id": "301", "nombre": "P301", "valor": 650.0, "invertido": 600.0}],
    "movements": {"desde": (YESTERDAY - timedelta(days=3)).isoformat(), "huecos": [], "movimientos": []},
}


def snapshot_api(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    """`full_api` plus a pension order in progress paired with the hold and a contribution the account cannot cover."""
    full_api(mock_api, monkeypatch)
    pension = {"accountId": "301", "externalAccountCode": "P301", "cashAccountId": "101", "marketValue": 700.0}
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(
        200, json=ok([{**pension, "totalInvested": 600.0, "profit": 100.0, "totalReturn": 16.67}])
    )
    in_progress = order(
        "p1",
        TODAY.isoformat(),
        securitiesAccountId=None,
        pensionAccountId="301",
        isin=None,
        dgsCode="N0001",
        fundName="Plan",
        status="IN_PROGRESS",
        cash=25.0,
    )
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders").respond(200, json=ok([in_progress]))
    due = {
        **CONTRIBUTION,
        "amount": 5000,
        "fromAccountId": "101",
        "contributionTimeFrame": {
            "recurrence": "ONE_MONTH",
            "nextExecutionDate": (TODAY + timedelta(days=2)).isoformat(),
        },
    }
    mock_api.get(f"{CPERF}/v2/automatic-contributions/self").respond(200, json=ok([due]))


METAL = {
    "id": 7,
    "name": "METAL",
    "profile": "VERY_AGGRESSIVE",
    "description": "Cartera de ejemplo",
    "fixedIncome": 20,
    "variableIncome": 80,
    "profitability": 8.25,
    "profitabilityFrom": "31/10/2023",
    "profitabilityTo": "31/10/2026",
    "codBenchmark": "BENCH_EJEMPLO",
    "type": "SAVING",
    "recommendedTimeHorizon": 10,
    "funds": [
        {
            "isin": FUND,
            "name": "Fondo indexado",
            "currency": "EUR",
            "percentage": 100,
            "category": "RV Global",
            "myInvestorCategory": "RV Global",
        }
    ],
}


def portfolio_api(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    """`full_api` plus the «METAL» robo-advisor portfolio (202) with its own cash account (103) and returns."""
    full_api(mock_api, monkeypatch)
    accounts = [
        cash_account(1, alias="Mi cuenta", holders=[ANA]),
        cash_account(2, alias="Junior", holders=[LEO, ANA]),
        cash_account(3, alias="Cartera Metal", holders=[ANA]),
    ]
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok(accounts))
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(
        200, json=ok([{"accountId": "201"}, {"accountId": "202"}])
    )
    metal = securities_account(
        "202",
        accountType="SECURITIES_PORTFOLIO",
        portfolioName="METAL",
        portfolioProfileType="VERY_AGGRESSIVE",
        cashAccountId="103",
        securitiesAccountInvestments={"INDEXED_FUND": {"investmentList": [investment(FUND, accountId="202")]}},
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/202").respond(200, json=ok(metal))
    mock_api.get(f"{CPERF}/v2/portfolios").respond(200, json=ok([METAL]))
    summary = {
        "indexedAccountId": 202,
        "portfolioName": "METAL",
        "years": {"2025": {"amount": 40.5, "percentage": 4.05}},
    }
    mock_api.get(f"{CPERF}/v3/portfolios/202/returns/summary").respond(200, json=ok(summary))


def order_api(mock_api: respx.MockRouter, _monkeypatch: pytest.MonkeyPatch) -> None:
    detail = order_detail("R1", executions=[execution()])
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/orders/R1").respond(200, json=ok(detail))


def fund_transfers_api(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    """`full_api` plus a fund transfer, a switch between funds, a pension-plan transfer and the pending counter."""
    full_api(mock_api, monkeypatch)
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/transfers").respond(
        200, json=ok([fund_transfer("T1", TODAY.isoformat())])
    )
    day = TODAY.isoformat()
    legs = [
        order("SW1", day, operationType="IIC_SWITCH_DEREGISTRATION"),
        order("SW2", day, operationType="IIC_SWITCH_REGISTRATION", isin="IE0000000003"),
    ]
    mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders").respond(200, json=ok(legs))
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders").respond(200, json=ok([]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/transfers/pending").respond(200, json=ok({"pendingTransfers": 1}))
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/transfers").respond(
        200, json=ok([pension_transfer("P1", TODAY.isoformat())])
    )


def lots_api(mock_api: respx.MockRouter, _monkeypatch: pytest.MonkeyPatch) -> None:
    mock_holdings(mock_api)
    for isin in ("IE0000000001", "IE0000000002"):
        mock_api.get(added_values(isin)).respond(200, json=ok(fund_lots(lot("N1", "2024-03-01"))))


def gains_contract_api(mock_api: respx.MockRouter, _monkeypatch: pytest.MonkeyPatch) -> None:
    gains_api(mock_api)
    mock_broker(mock_api, etc_trades())


# ---- one case per variant of every contract ------------------------------------------------------


@dataclass(frozen=True)
class Case:
    command: str
    variant: str
    argv: tuple[str, ...]
    setup: Callable[[respx.MockRouter, pytest.MonkeyPatch], None] = full_api
    options: tuple[str, ...] = ()  # what adds the `only_with` keys (`{target}`: a target file)
    target: dict[str, Any] = field(default_factory=dict)
    always_null: frozenset[str] = frozenset()  # nullable keys the synthetic data cannot fill
    with_format: bool = True  # append `-f json`

    @property
    def id(self) -> str:
        return f"{self.command}[{self.variant}]" if self.variant else self.command


CASES = (
    Case("position", "", ("position",), setup=portfolio_api),
    Case("accounts", "", ("accounts",)),
    Case("movements", "", ("movements",), options=("--details",)),
    Case(
        "movements",
        "--id",
        ("movements", "--id", f"{CASH_UUID}:000259"),
        options=("--details",),
        always_null=frozenset({"pareja_id"}),  # --id only reads the movement's account: the other leg is not there
    ),
    Case("holds", "", ("holds",)),
    Case("balance-history", "", ("balance-history",)),
    Case("holdings", "", ("holdings",), setup=portfolio_api, options=("--ter",)),
    Case("orders", "", ("orders",)),
    Case("pensions", "", ("pensions",)),
    Case("history", "", ("history",)),
    Case("contributions", "", ("contributions",)),
    Case("funds", "", ("funds",)),
    Case("fund-filters", "", ("fund-filters",)),
    Case("pension-plans", "", ("pension-plans",), options=("--mine",)),
    Case("fund-history", "", ("fund-history", FUND)),
    Case("portfolios", "", ("portfolios",), setup=portfolio_api),
    Case("portfolios", "CARTERA", ("portfolios", "metal"), setup=portfolio_api),
    Case("portfolios", "--mine", ("portfolios", "--mine"), setup=portfolio_api),
    Case("feed", "", ("feed",)),
    Case("internal-transfers", "", ("internal-transfers", "--unmatched")),
    Case("upcoming", "", ("upcoming",), setup=upcoming_api),
    Case("documents", "", ("documents",), setup=documents_api),
    Case("transfers", "", ("transfers", "--from", "2026-01-01")),
    Case("interest", "", ("interest",)),
    Case("income", "--by month | --by year", ("income",)),
    Case("income", "--by type", ("income", "--by", "type")),
    Case("income", "--by account", ("income", "--by", "account")),
    Case(
        "allocation",
        "--by asset",
        ("allocation",),
        options=("--target", "{target}"),
        target={"renta_variable": 100},
    ),
    Case(
        "allocation",
        "--by geo | --by category",
        ("allocation", "--by", "geo"),
        options=("--target", "{target}"),
        target={"global": 100},
    ),
    Case(
        "allocation",
        "--by product",
        ("allocation", "--by", "product"),
        options=("--target", "{target}"),
        target={FUND: 100},
    ),
    Case(
        "allocation",
        "--by account",
        ("allocation", "--by", "account"),
        options=("--target", "{target}"),
        target={"Cartera": 100},
    ),
    Case("order", "", ("order", "R1", "-a", "201"), setup=order_api),
    # `--pension` fills `destino_nombre` of the listed transfers; the switch from the orders, `ref_destino`
    Case("fund-transfers", "", ("fund-transfers",), setup=fund_transfers_api, options=("--pension",)),
    Case("fund-transfers", "--pending", ("fund-transfers", "--pending"), setup=fund_transfers_api),
    Case("lots", "", ("lots",), setup=lots_api),
    Case("gains", "", ("gains", "--year", "2025"), setup=gains_contract_api),
    Case(
        "status",
        "",
        ("status",),
        always_null=frozenset({"store_path", "relogin_blocked_until", "last_relogin_error"}),  # memory store
    ),
    Case("doctor", "", ("doctor", "--offline")),
    Case("state show", "", ("state", "show", "--state", "{tmp}/seen.json")),
    Case(
        "snapshot",
        "",
        ("snapshot", "--previous", "{target}"),
        setup=snapshot_api,
        target=PREVIOUS_SNAPSHOT,
        with_format=False,
        # a successful snapshot has no gaps nor errors
        always_null=frozenset(
            {f"movements.huecos[].{k}" for k in ("cuenta_id", "cuenta", "desde", "hasta")}
            | {f"errors[].{k}" for k in ("section", "error", "exit_code", "message")}
        ),
    ),
    Case("raw", "", ("raw", "/feed/api/x"), with_format=False),
    Case("fund", "", ("fund", FUND), with_format=False),
)
IDS = [c.id for c in CASES]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def argv(case: Case, tmp_path: Path, *, options: bool, envelope: bool = False) -> list[str]:
    target = tmp_path / "objetivo.json"
    target.write_text(json.dumps(case.target), encoding="utf-8")
    args = [*case.argv, *(case.options if options else ()), *(("-f", "json") if case.with_format else ())]
    args = [a.replace("{tmp}", str(tmp_path)).replace("{target}", str(target)) for a in args]
    return ["--json-envelope", *args] if envelope else args


def run_json(args: list[str]) -> Any:
    result = runner.invoke(app, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout, parse_float=Decimal)


def variant_of(contract: Contract, label: str) -> Variant:
    return next(v for v in contract.variants if v.label == label)


# ---- checking a value against the table ----------------------------------------------------------


def _is_datetime(value: Any) -> bool:
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _is_date(value: Any) -> bool:
    try:
        return isinstance(value, str) and bool(DATE.fullmatch(value)) and bool(date.fromisoformat(value))
    except ValueError:
        return False


TYPE_CHECKS: dict[JsonType, Callable[[Any], bool]] = {
    JsonType.string: lambda v: isinstance(v, str),
    JsonType.amount: lambda v: isinstance(v, Decimal) and v.as_tuple().exponent == -2,
    JsonType.number: lambda v: isinstance(v, Decimal | int) and not isinstance(v, bool),
    JsonType.integer: lambda v: isinstance(v, int) and not isinstance(v, bool),
    JsonType.boolean: lambda v: isinstance(v, bool),
    JsonType.date: _is_date,
    JsonType.datetime: _is_datetime,
    JsonType.object: lambda v: isinstance(v, dict),
    JsonType.array: lambda v: isinstance(v, list),
    JsonType.any: lambda v: True,
}


def _present(fields: tuple[Field, ...], options: set[str]) -> tuple[Field, ...]:
    return tuple(f for f in fields if f.only_with is None or f.only_with in options)


def check_object(
    obj: Any, fields: tuple[Field, ...], where: str, options: set[str], filled: set[str], prefix: str = ""
) -> None:
    present = _present(fields, options)
    if not isinstance(obj, dict):
        pytest.fail(f"{where}: se esperaba un objeto en `{prefix or '.'}` y llega {type(obj).__name__}. {BUMP}")
    expected, keys = {f.name for f in present}, set(obj)
    if keys != expected:
        pytest.fail(
            f"{where}: las claves{f' de `{prefix}`' if prefix else ''} han cambiado.\n"
            f"  faltan: {sorted(expected - keys)}\n  sobran: {sorted(keys - expected)}\n{BUMP}"
        )
    for f in present:
        value, path = obj[f.name], f"{prefix}{f.name}"
        if value is None:
            if not f.nullable:
                pytest.fail(f"{where}: `{path}` es null y el contrato dice que nunca lo es. {BUMP}")
            continue
        filled.add(path)
        if not TYPE_CHECKS[f.type](value):
            pytest.fail(f"{where}: `{path}` = {value!r} no es {TYPE_LABELS[f.type]}. {BUMP}")
        if f.fields and f.type is JsonType.array:
            for item in value:
                check_object(item, f.fields, where, options, filled, f"{path}[].")
        elif f.fields:
            check_object(value, f.fields, where, options, filled, f"{path}.")


def check_data(data: Any, contract: Contract, variant: Variant, options: set[str], filled: set[str]) -> None:
    where = f"{contract.schema_id} ({variant.label or 'por defecto'})"
    if variant.shape is Shape.rows:
        assert isinstance(data, list) and data, f"{where}: se esperaba una lista no vacía. {BUMP}"
        for row in data:
            check_object(row, variant.fields, where, options, filled)
    elif variant.shape is Shape.object:
        check_object(data, variant.fields, where, options, filled)


def field_paths(fields: tuple[Field, ...], prefix: str = "") -> Iterator[str]:
    for f in fields:
        yield f"{prefix}{f.name}"
        yield from field_paths(f.fields, f"{prefix}{f.name}{'[]' if f.type is JsonType.array else ''}.")


# ---- the contract tests -------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_output_follows_its_contract(
    case: Case, mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    contract = contract_for(case.command)
    variant = variant_of(contract, case.variant)
    case.setup(mock_api, monkeypatch)
    filled: set[str] = set()
    for with_options in dict.fromkeys((False, bool(case.options))):
        options = set(case.options) if with_options else set()
        check_data(run_json(argv(case, tmp_path, options=with_options)), contract, variant, options, filled)
    # every key must have shown a value, or its type would never have been checked
    unfilled = set(field_paths(variant.fields)) - filled - case.always_null
    assert not unfilled, f"los datos sintéticos de {case.id} no rellenan {sorted(unfilled)}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_envelope_wraps_the_output_with_its_schema(
    case: Case, mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    contract = contract_for(case.command)
    case.setup(mock_api, monkeypatch)

    doc = run_json(argv(case, tmp_path, options=False, envelope=True))

    if not contract.enveloped:  # it already carries its own `schema`
        assert doc["schema"] == contract.schema_id
        check_data(doc, contract, variant_of(contract, case.variant), set(), set())
        return
    assert list(doc) == [f.name for f in ENVELOPE]
    assert doc["schema"] == contract.schema_id
    assert doc["cli_version"] == package_version()
    assert _is_datetime(doc["as_of"])
    check_data(doc["data"], contract, variant_of(contract, case.variant), set(), set())


def test_envelope_from_the_environment(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_api(mock_api, monkeypatch)
    result = runner.invoke(app, ["holds", "-f", "json"], env={"MYINVESTOR_JSON_ENVELOPE": "1"})
    assert json.loads(result.stdout)["schema"] == "myinvestor.holds/1"


def test_without_the_option_the_output_is_unchanged(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_api(mock_api, monkeypatch)
    bare = run_json(["holds", "-f", "json"])
    assert isinstance(bare, list)
    assert run_json(["--json-envelope", "holds", "-f", "json"])["data"] == bare
    csv_plain = runner.invoke(app, ["holds", "-f", "csv"]).stdout
    assert runner.invoke(app, ["--json-envelope", "holds", "-f", "csv"]).stdout == csv_plain


def test_error_line_follows_its_contract_and_is_never_wrapped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["myinvestor", "--json-envelope", "movements", "--from", "ayer", "-f", "json"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    [line] = capsys.readouterr().err.splitlines()
    error = json.loads(line)
    check_data(error, ERROR_LINE, ERROR_LINE.variants[0], set(), set())
    assert error["exit_code"] == exit_info.value.code == 2


def test_a_self_described_output_is_not_wrapped() -> None:
    """Outputs that carry their own `schema` (`error`, `snapshot`) stay as they are."""
    json_envelope.configure(enabled=True, command=ERROR_LINE.command)
    assert json_envelope.wrap({"error": "x"}) == {"error": "x"}


def test_every_command_with_json_output_has_a_contract() -> None:
    """A new command with `-f json` must get an entry in json_contract.CONTRACTS (and a case here)."""
    not_stdout = {"export"}  # writes files (CSV/JSON Lines and manifest.json), nothing on stdout
    not_data = {"schema"}  # prints the JSON Schemas themselves

    def leaves(command: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
        for name, sub in getattr(command, "commands", {}).items():
            yield f"{prefix}{name}", sub  # a group too: `documents` lists from its own callback
            if hasattr(sub, "commands"):
                yield from leaves(sub, f"{prefix}{name} ")

    with_format = {
        name
        for name, command in leaves(typer.main.get_command(app))
        if any(p.name == "fmt" for p in command.params) and name not in not_stdout | not_data
    }
    assert with_format == {c.command for c in CONTRACTS}


# Commands without `-f`: they write files or state, or ask for credentials; none prints data on stdout.
NO_FORMAT = frozenset(
    {"login", "logout", "xray", "documents get", "documents summary", "state", "state mark"}
    | {"state reset", "summary"}  # `summary` is the human-readable snapshot, never JSON
)


def test_every_reading_command_takes_format_json() -> None:
    """`-f json` works everywhere it makes sense, so a script can always pass it (#75)."""

    def leaves(command: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
        for name, sub in getattr(command, "commands", {}).items():
            yield f"{prefix}{name}", sub
            yield from leaves(sub, f"{prefix}{name} ")

    without = {
        name
        for name, command in leaves(typer.main.get_command(app))
        if not any("--format" in getattr(p, "opts", ()) for p in command.params)
    }
    assert without == NO_FORMAT


def test_every_variant_has_a_case() -> None:
    variants = {(c.command, v.label) for c in CONTRACTS for v in c.variants}
    assert variants == {(case.command, case.variant) for case in CASES}


def test_amount_keys_are_the_ones_rounded_to_cents() -> None:
    """`amount` keys are the numbers `output.normalize_row` rounds to cents, by name, in every command.

    `retenido` is also a boolean in `upcoming` (normalize_row leaves booleans alone).
    """

    def every(fields: tuple[Field, ...]) -> Iterator[Field]:
        for f in fields:
            yield f
            yield from every(f.fields)

    for contract in CONTRACTS:
        for variant in contract.variants:
            for f in every(variant.fields):
                if f.type is JsonType.amount:
                    assert f.name in AMOUNT_COLUMNS, (contract.command, f.name)
                elif f.name in AMOUNT_COLUMNS:
                    assert f.type is JsonType.boolean, (contract.command, f.name)


# ---- published schemas and generated docs ------------------------------------------------------------


def _signature(fields: tuple[Field, ...], prefix: str = "") -> Iterator[tuple[str, str]]:
    for f in fields:
        kind = f"{f.type}{'|null' if f.nullable else ''}{f' (solo con {f.only_with})' if f.only_with else ''}"
        yield f"{prefix}{f.name}", kind
        yield from _signature(f.fields, f"{prefix}{f.name}{'[]' if f.type is JsonType.array else ''}.")


def published_form(contract: Contract) -> dict[str, dict[str, str]]:
    return {(v.label or "por defecto"): dict(_signature(v.fields)) or {"*": v.shape.value} for v in contract.variants}


def current_schemas() -> dict[str, dict[str, dict[str, str]]]:
    schemas = {"envelope": {"por defecto": dict(_signature(ENVELOPE))}}
    for contract in (*CONTRACTS, ERROR_LINE):
        schemas.setdefault(contract.schema_id, published_form(contract))
    return schemas


def breaking_changes(published: dict[str, dict[str, str]], current: dict[str, dict[str, str]]) -> Iterator[str]:
    for variant, keys in published.items():
        now = current.get(variant)
        if now is None:
            yield f"desaparece la variante «{variant}»"
            continue
        for key, kind in keys.items():
            if key not in now:
                yield f"«{variant}»: se quita `{key}`"
            elif now[key] != kind:
                yield f"«{variant}»: `{key}` pasa de {kind} a {now[key]}"


def test_published_schemas_never_change_incompatibly() -> None:
    """json_contract.lock.json keeps every published schema; old versions stay as history."""
    lock: dict[str, Any] = json.loads(LOCK.read_text(encoding="utf-8"))
    current = current_schemas()
    breaking = {sid: list(breaking_changes(lock[sid], now)) for sid, now in current.items() if sid in lock}
    breaking = {sid: changes for sid, changes in breaking.items() if changes}
    assert not breaking, f"Cambio incompatible en un esquema publicado: {breaking}\n{BUMP}"
    pending = sorted(sid for sid, now in current.items() if lock.get(sid) != now)
    if pending and UPDATE:
        LOCK.write_text(json.dumps({**lock, **current}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return
    assert not pending, f"Esquemas nuevos o con claves añadidas sin publicar: {pending}. Ejecuta {UPDATE_HINT}."


def test_docs_reference_is_generated_from_the_table() -> None:
    text = DOCS.read_text(encoding="utf-8")
    head, rest = text.split(DOCS_BEGIN)
    _, tail = rest.split(DOCS_END)
    generated = f"{head}{DOCS_BEGIN}\n\n{markdown_reference()}\n{DOCS_END}{tail}"
    if UPDATE:
        DOCS.write_text(generated, encoding="utf-8")
        return
    assert text == generated, f"docs/json.md no coincide con la tabla de json_contract.py. Ejecuta {UPDATE_HINT}."


# ---- `myinvestor schema` --------------------------------------------------------------------------


def test_schema_index_lists_every_command() -> None:
    result = runner.invoke(app, ["--json-envelope", "schema"])  # never wrapped
    assert json.loads(result.stdout) == schema_index() == {c.command: c.schema_id for c in CONTRACTS}


@pytest.mark.parametrize("contract", [*CONTRACTS, ERROR_LINE], ids=lambda c: c.command)
def test_json_schema_matches_the_table(contract: Contract) -> None:
    result = runner.invoke(app, ["schema", contract.command.replace(" ", "-")])
    schema = json.loads(result.stdout)
    assert schema == json_schema(contract)
    assert schema["title"] == contract.schema_id
    shapes = schema.get("oneOf", [schema])
    for variant, shape in zip(contract.variants, shapes, strict=True):
        if variant.shape is Shape.any:
            assert "type" not in shape
            continue
        obj = shape["items"] if variant.shape is Shape.rows else shape
        assert list(obj["properties"]) == [f.name for f in variant.fields]
        assert obj["required"] == [f.name for f in variant.fields if f.only_with is None]
        for f in variant.fields:
            assert ("null" in obj["properties"][f.name]["type"]) is f.nullable


def test_json_schema_with_envelope() -> None:
    schema = json.loads(runner.invoke(app, ["schema", "movements", "--envelope"]).stdout)
    assert schema["properties"]["schema"] == {"const": "myinvestor.movements/1"}
    assert len(schema["properties"]["data"]["oneOf"]) == 2
    assert schema["required"] == [f.name for f in ENVELOPE]


def test_schema_of_an_unknown_command() -> None:
    result = runner.invoke(app, ["schema", "nope"])
    assert isinstance(result.exception, InputError) and "movements" in str(result.exception)
