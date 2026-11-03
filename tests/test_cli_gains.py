"""`order` (an order with its executions), `lots` (open FIFO lots) and `gains` (realised gains)."""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.client import MyInvestorClient
from myinvestor.errors import InputError
from myinvestor.models import FundLots, OrderDetail
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import CPERF, investment, ok, order, securities_account

runner = CliRunner()
DNI = "00000000T"  # synthetic `ordererCustomer`: must never reach the output
SEC_LIST = f"{CPERF}/v3/securities-accounts/201/orders"
PENSION_LIST = f"{CPERF}/v2/pension-accounts/301/orders"
REIMBURSEMENT = "INVESTMENT_FUND_REIMBURSEMENT"


def execution(num: str = "OP1", day: str = "2026-10-02", **overrides: Any) -> dict[str, Any]:
    base = {
        "operationNumber": num,
        "executionDate": f"{day}T00:00:00.000+0000",
        "executedShares": 1.5,
        "liquidationValue": 100.123456,
        "grossAmountOperationFundCurrency": 150.18,
        "commissions": 0.0,
        "originRetention": 1.5,
        "destinyRetention": 0.5,
        "netAmountFundCurrency": 148.18,
        "exchangeRate": 1.0,
        "netAmountEUR": 148.18,
        "netAmountAccountCurrency": 148.18,
        "taxResult": 10.0,
    }
    return {**base, **overrides}


def order_detail(ref: str = "R1", executions: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    base = {
        "ordererCustomer": DNI,
        "isin": "IE0000000001",
        "orderReference": ref,
        "orderStatus": "COMPLETE",
        "accountNumber": "ACC-0001",
        "orderDate": "2026-10-01T00:00:00.000+0000",
        "orderTypeName": "Reembolso",
        "orderClass": "ORDINARY",
        "grossAmountEUR": 150.18,
        "shares": 1.5,
        "grossAmountFundCurrency": 150.18,
        "expirationDate": "2026-10-05T00:00:00.000+0000",
        "cash": 150.18,
        "fundName": "Fondo indexado",
        "currency": "EUR",
        "periodicityCode": "",
        "operationType": REIMBURSEMENT,
        "relatedOperations": [execution()] if executions is None else executions,
    }
    return {**base, **overrides}


def lot(num: str, fiscal_day: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "operationType": "SUBSCRIPTION",
        "operationDesc": "SUSCRIPCIÓN FONDOS INVERSIÓN",
        "operationNum": num,
        "fiscalDate": f"{fiscal_day}T00:00:00.000+0000",
        "securities": 1.25,
        "netAssetValue": 80.0,
        "investment": 100.0,
        "marketValue": 111.444,
        "plusMinus": 11.444,
        "fiscalResult": 11.444,
    }
    return {**base, **overrides}


def fund_lots(*lots: dict[str, Any]) -> dict[str, Any]:
    return {
        "currency": "EUR",
        "fundName": "Fondo indexado",
        "investmentAddedValue": 200.0,
        "addedValue": 22.89,
        "plusMinusList": list(lots),
    }


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def json_out(*args: str) -> Any:
    result = invoke(*args, "-f", "json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout, parse_float=Decimal)


def mock_securities(mock_api: respx.MockRouter, *ids: str) -> None:
    rows = [{"accountId": i, "status": "ACTIVE"} for i in ids or ("201",)]
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok(rows))


# ---- models and client ---------------------------------------------------------------------------


def test_order_detail_model_types_executions_and_never_keeps_the_dni() -> None:
    detail = OrderDetail.model_validate(order_detail())

    assert detail.order_reference == "R1"
    assert detail.order_date == date(2026, 10, 1)
    assert detail.gross_amount_eur == Decimal("150.18")
    [run] = detail.related_operations
    assert run.execution_date == date(2026, 10, 2)
    assert run.net_amount_eur == Decimal("148.18")
    assert run.retentions == Decimal("2.0")
    dumped = str(detail.model_dump())
    assert DNI not in dumped
    assert "ACC-0001" not in dumped


def test_client_order_detail_securities_and_pension_paths(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    sec = mock_api.get(f"{CPERF}/v2/securities-accounts/201/orders/R%2F1").respond(200, json=ok(order_detail("R/1")))
    pen = mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders/P1").respond(200, json=ok(order_detail("P1")))
    client = MyInvestorClient(session, http=http)

    assert client.order_detail("201", "R/1").order_reference == "R/1"
    assert client.order_detail("301", "P1", pension=True).order_reference == "P1"
    assert sec.called and pen.called


def test_client_fund_lots(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{CPERF}/v2/securities-accounts/201/investment-summary/funds/IE0000000001/added-values")
    route.respond(200, json=ok(fund_lots(lot("N1", "2024-03-01"))))

    lots = MyInvestorClient(session, http=http).fund_lots("201", "IE0000000001")

    assert isinstance(lots, FundLots)
    assert lots.added_value == Decimal("22.89")
    [first] = lots.plus_minus_list
    assert (first.fiscal_date, first.securities, first.fiscal_result) == (
        date(2024, 3, 1),
        Decimal("1.25"),
        Decimal("11.444"),
    )


# ---- order REF -----------------------------------------------------------------------------------


def test_order_finds_the_account_and_shows_order_and_executions(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_securities(mock_api)
    listing = mock_api.get(SEC_LIST).respond(200, json=ok([order("R0", "2026-09-01"), order("R1", "2026-10-01")]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/orders/R1").respond(200, json=ok(order_detail()))

    result = invoke("order", "R1", "-f", "json")

    assert DNI not in result.stdout and "ACC-0001" not in result.stdout
    data = json.loads(result.stdout, parse_float=Decimal)
    assert set(data) == {"orden", "ejecuciones"}
    assert data["orden"] == {
        "cuenta": "201",
        "tipo_cuenta": "valores",
        "ref": "R1",
        "fecha": "2026-10-01",
        "isin": "IE0000000001",
        "fondo": "Fondo indexado",
        "operacion": REIMBURSEMENT,
        "tipo": "Reembolso",
        "estado": "COMPLETE",
        "importe": Decimal("150.18"),
        "participaciones": Decimal("1.5"),
        "divisa": "EUR",
        "vencimiento": "2026-10-05",
    }
    assert data["ejecuciones"] == [
        {
            "num_operacion": "OP1",
            "fecha": "2026-10-02",
            "participaciones": Decimal("1.5"),
            "vl": Decimal("100.123456"),
            "bruto": Decimal("150.18"),
            "comisiones": Decimal("0.00"),
            "retenciones": Decimal("2.00"),
            "neto": Decimal("148.18"),
            "tipo_cambio": Decimal("1.0"),
            "resultado": Decimal("10.00"),
        }
    ]
    assert "dateFrom" in listing.calls.last.request.url.params


def test_order_falls_back_to_pension_accounts(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_securities(mock_api)
    mock_api.get(SEC_LIST).respond(200, json=ok([order("R0", "2026-09-01")]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([{"accountId": "301"}]))
    pension_order = order("P1", "2026-10-01", securitiesAccountId=None, pensionAccountId="301", dgsCode="N0001")
    mock_api.get(PENSION_LIST).respond(200, json=ok([pension_order]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders/P1").respond(
        200, json=ok(order_detail("P1", isin="N0001", operationType="PENSION_CONTRIBUTION"))
    )

    data = json_out("order", "P1")

    assert (data["orden"]["cuenta"], data["orden"]["tipo_cuenta"], data["orden"]["isin"]) == (
        "301",
        "pensiones",
        "N0001",
    )


def test_order_with_account_goes_straight_to_the_detail(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    listing = mock_api.get(PENSION_LIST).respond(200, json=ok([]))
    detail = mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders/P1").respond(200, json=ok(order_detail("P1")))

    data = json_out("order", "P1", "-a", "301", "--pension")

    assert detail.call_count == 1 and not listing.called
    assert data["orden"]["tipo_cuenta"] == "pensiones"


def test_order_unknown_reference_is_an_input_error(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_securities(mock_api)
    mock_api.get(SEC_LIST).respond(200, json=ok([order("R0", "2026-09-01")]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self").respond(200, json=ok([]))

    result = runner.invoke(app, ["order", "NOPE"])

    assert isinstance(result.exception, InputError)
    assert "NOPE" in str(result.exception)


def test_order_csv_has_one_row_per_execution_and_table_shows_both(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    runs = [execution("OP1"), execution("OP2", "2026-10-03", taxResult=-3.5)]
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/orders/R1").respond(200, json=ok(order_detail(executions=runs)))

    rows = list(csv.DictReader(io.StringIO(invoke("order", "R1", "-a", "201", "-f", "csv").stdout)))
    table = invoke("order", "R1", "-a", "201").stdout

    assert [(r["ref"], r["num_operacion"], r["resultado"]) for r in rows] == [
        ("R1", "OP1", "10.00"),
        ("R1", "OP2", "-3.50"),
    ]
    assert "Fondo indexado" in table and "OP2" in table and DNI not in table


# ---- lots ----------------------------------------------------------------------------------------


def mock_holdings(mock_api: respx.MockRouter) -> None:
    mock_securities(mock_api)
    groups = {
        "INDEXED_FUND": {"investmentList": [investment("IE0000000001"), investment("IE0000000002")]},
        "BROKER": {"investmentList": [investment("US0000000001", kind="BROKER")]},
    }
    mock_api.get(f"{CPERF}/v2/securities-accounts/201").respond(
        200, json=ok(securities_account(securitiesAccountInvestments=groups))
    )


def added_values(isin: str) -> str:
    return f"{CPERF}/v2/securities-accounts/201/investment-summary/funds/{isin}/added-values"


def test_lots_one_row_per_open_lot_of_each_fund_and_a_total(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_holdings(mock_api)
    switch = lot("N2", "2019-05-10", operationDesc="ALTA IIC SWITCH", securities=0.5, investment=40.0, fiscalResult=-2)
    mock_api.get(added_values("IE0000000001")).respond(200, json=ok(fund_lots(lot("N1", "2024-03-01"), switch)))
    mock_api.get(added_values("IE0000000002")).respond(200, json=ok(fund_lots(lot("N3", "2025-01-15"))))
    broker = mock_api.get(added_values("US0000000001"))

    rows = json_out("lots")

    assert not broker.called
    assert [(r["isin"], r["fecha_fiscal"], r["operacion"]) for r in rows[:-1]] == [
        ("IE0000000001", "2019-05-10", "ALTA IIC SWITCH"),
        ("IE0000000001", "2024-03-01", "SUSCRIPCIÓN FONDOS INVERSIÓN"),
        ("IE0000000002", "2025-01-15", "SUSCRIPCIÓN FONDOS INVERSIÓN"),
    ]
    assert rows[1] == {
        "cuenta": "201",
        "isin": "IE0000000001",
        "fondo": "Fondo indexado",
        "fecha_fiscal": "2024-03-01",
        "operacion": "SUSCRIPCIÓN FONDOS INVERSIÓN",
        "participaciones": Decimal("1.25"),
        "vl_compra": Decimal("80.0"),
        "coste": Decimal("100.00"),
        "valor": Decimal("111.44"),
        "plusvalia": Decimal("11.44"),
    }
    total = rows[-1]
    assert total["cuenta"] == "total"
    assert (total["coste"], total["valor"], total["plusvalia"]) == (
        Decimal("240.00"),
        Decimal("334.33"),
        Decimal("20.89"),
    )


def test_lots_filters_by_isin_and_rejects_a_fund_not_held(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_holdings(mock_api)
    mock_api.get(added_values("IE0000000002")).respond(200, json=ok(fund_lots(lot("N3", "2025-01-15"))))
    other = mock_api.get(added_values("IE0000000001"))

    rows = json_out("lots", "ie0000000002")

    assert [(r["cuenta"], r["isin"]) for r in rows] == [("201", "IE0000000002"), ("total", None)]
    assert not other.called
    assert isinstance(runner.invoke(app, ["lots", "IE0000000009"]).exception, InputError)


# ---- gains ---------------------------------------------------------------------------------------


def gains_api(mock_api: respx.MockRouter) -> tuple[respx.Route, dict[str, respx.Route]]:
    mock_securities(mock_api)
    orders = [
        order("BUY", "2025-03-01"),  # a subscription: no detail
        order("SALE", "2025-06-01", operationType=REIMBURSEMENT),
        order("LOSS", "2025-07-01", operationType=REIMBURSEMENT),
        order("MOVE", "2025-08-01", operationType="INTERNAL_TRANSFER_REIMBURSEMENT"),
        order("LATE", "2025-12-30", operationType=REIMBURSEMENT),  # executed in 2026
        order("EARLY", "2024-12-30", operationType=REIMBURSEMENT),  # given in 2024, executed in 2025
        order("GONE", "2025-09-01", operationType=REIMBURSEMENT, status="CANCEL"),
    ]
    listing = mock_api.get(SEC_LIST).respond(200, json=ok(orders))
    runs = {
        "SALE": [execution("S1", "2025-06-03", taxResult=12.3)],
        "LOSS": [execution("L1", "2025-07-02", taxResult=-4.1)],
        "MOVE": [execution("M1", "2025-08-04", taxResult=50.0)],
        "LATE": [execution("X1", "2026-01-02", taxResult=99.0)],
        "EARLY": [execution("E1", "2025-01-02", taxResult=1.0)],
        "GONE": [],
        "BUY": [],
    }
    kinds = {o["reference"]: o["operationType"] for o in orders}
    details = {
        ref: mock_api.get(f"{CPERF}/v2/securities-accounts/201/orders/{ref}").respond(
            200, json=ok(order_detail(ref, executions=r, operationType=kinds[ref]))
        )
        for ref, r in runs.items()
    }
    return listing, details


def test_gains_lists_executed_outgoing_orders_of_the_year_with_totals(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    listing, details = gains_api(mock_api)

    rows = json_out("gains", "--year", "2025")

    assert dict(listing.calls.last.request.url.params) == {"dateFrom": "2024-12-01", "dateTo": "2025-12-31"}
    assert not details["BUY"].called and not details["GONE"].called
    sales = [r for r in rows if r["ref"]]
    assert [(r["fecha"], r["ref"], r["resultado"], r["traspaso"]) for r in sales] == [
        ("2025-01-02", "EARLY", Decimal("1.00"), False),
        ("2025-06-03", "SALE", Decimal("12.30"), False),
        ("2025-07-02", "LOSS", Decimal("-4.10"), False),
        ("2025-08-04", "MOVE", Decimal("50.00"), True),
    ]
    assert set(sales[0]) == {
        "cuenta",
        "fecha",
        "ref",
        "isin",
        "fondo",
        "operacion",
        "traspaso",
        "participaciones",
        "vl",
        "bruto",
        "comisiones",
        "retenciones",
        "neto",
        "resultado",
    }
    totals = {r["cuenta"]: r["resultado"] for r in rows if not r["ref"]}
    assert totals == {"plusvalias": Decimal("13.30"), "minusvalias": Decimal("-4.10"), "total": Decimal("9.20")}


def test_gains_filters_by_isin(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_securities(mock_api)
    sale = order("SALE", "2025-06-01", operationType=REIMBURSEMENT)
    other = order("OTHER", "2025-06-01", operationType=REIMBURSEMENT, isin="IE0000000002")
    mock_api.get(SEC_LIST).respond(200, json=ok([sale, other]))
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/orders/SALE").respond(
        200, json=ok(order_detail("SALE", executions=[execution("S1", "2025-06-03")]))
    )
    skipped = mock_api.get(f"{CPERF}/v2/securities-accounts/201/orders/OTHER")

    rows = json_out("gains", "IE0000000001", "--year", "2025")

    assert [r["ref"] for r in rows if r["ref"]] == ["SALE"]
    assert not skipped.called


def test_gains_rejects_a_future_year(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["gains", "--year", str(date.today().year + 1)])

    assert isinstance(result.exception, InputError)


# ---- pension contributions: the shape seen on real accounts ------------------------------------


def pension_detail(status: str, executions: list[dict[str, Any]] | None) -> dict[str, Any]:
    return order_detail(
        "P1",
        isin="N0001",
        orderStatus=status,
        operationType="PLAN_CONTRIBUTION",
        grossAmountEUR=100,
        relatedOperations=executions,
    )


def nulls(data: dict[str, Any], *keep: str) -> dict[str, Any]:
    return {key: (value if key in keep else None) for key, value in data.items()}


def test_models_tolerate_nulls_from_the_api() -> None:
    detail = OrderDetail.model_validate(nulls(order_detail(), "orderReference"))
    [run] = OrderDetail.model_validate(order_detail(executions=[nulls(execution())])).related_operations
    lots = FundLots.model_validate({**nulls(fund_lots()), "plusMinusList": [nulls(lot("N1", "2024-01-01"))]})

    assert (detail.related_operations, detail.currency, detail.order_date) == ([], "EUR", None)
    assert (run.execution_date, run.retentions, run.tax_result) == (None, None, None)
    assert lots.plus_minus_list[0].gain is None


def test_order_of_a_pension_contribution_in_progress_has_no_executions(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders/P1").respond(
        200, json=ok(pension_detail("IN_PROGRESS", None))
    )

    data = json_out("order", "P1", "-a", "301", "--pension")
    table = invoke("order", "P1", "-a", "301", "--pension")

    assert (data["orden"]["estado"], data["orden"]["importe"], data["ejecuciones"]) == (
        "IN_PROGRESS",
        Decimal("100.00"),
        [],
    )
    assert table.exit_code == 0 and "PLAN_CONTRIBUTION" in table.stdout


def test_order_of_a_completed_pension_contribution_without_execution_date(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    run = execution(executionDate=None, netAmountEUR=100, netAmountAccountCurrency=100, taxResult=0)
    mock_api.get(f"{CPERF}/v2/pension-accounts/301/orders/P1").respond(200, json=ok(pension_detail("COMPLETE", [run])))

    [row] = json_out("order", "P1", "-a", "301", "--pension")["ejecuciones"]

    assert (row["fecha"], row["neto"], row["resultado"]) == (None, Decimal("100.00"), Decimal("0.00"))


def test_gains_of_an_execution_without_date_uses_the_order_date(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_securities(mock_api)
    mock_api.get(SEC_LIST).respond(200, json=ok([order("SALE", "2025-06-01", operationType=REIMBURSEMENT)]))
    detail = order_detail("SALE", executions=[execution(executionDate=None)], orderDate="2025-06-01T00:00:00.000+0000")
    mock_api.get(f"{CPERF}/v2/securities-accounts/201/orders/SALE").respond(200, json=ok(detail))

    rows = json_out("gains", "--year", "2025")

    assert rows[0]["fecha"] == "2025-06-01"
