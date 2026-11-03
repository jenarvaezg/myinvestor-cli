"""High-level, read-only MyInvestor API client."""

from __future__ import annotations

import contextlib
import copy
import logging
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self
from urllib.parse import quote

import httpx

from myinvestor import envelope
from myinvestor.auth import Authenticator, Relogin
from myinvestor.catalog import CatalogFilters, Domain, FilterKind
from myinvestor.config import API_BASE
from myinvestor.documents import Document, DocumentKind, TemporaryDocument, XrayTarget
from myinvestor.errors import (
    ApiError,
    MyInvestorError,
    NotLoggedInError,
    SessionExpiredError,
    SignatureError,
    _is_waf_block,
    raise_for_response,
)
from myinvestor.http import build_http_client
from myinvestor.models import (
    CashAccount,
    Contribution,
    FeedItem,
    Flow,
    FundLots,
    FundSheet,
    FundSummary,
    FundTransfer,
    Hold,
    NavPoint,
    Order,
    OrderDetail,
    PendingTransfers,
    PensionAccount,
    PensionPlanPosition,
    PensionPlanSheet,
    PensionPlanSummary,
    PensionTransfer,
    PositionPoint,
    Remuneration,
    SecuritiesAccount,
    SentTransfer,
)
from myinvestor.portfolios import AnnualReturn, Portfolio, annual_returns
from myinvestor.session import Session
from myinvestor.signature import SIGNATURE_HEADER, SignatureUI, Signer, parse_signature_request
from myinvestor.stores import SessionStore

CPERF = "/cperf-server/api"
FUND_PAGE_SIZE = 50
CATALOG_MAX_LIMIT = 50  # rows the catalog API returns at most per request
PORTFOLIO_RETURN_YEARS = 3  # `limit` of the web's returns summary (page 1)
SIGNATURE_PREFIX = "/signature/api/"
SIGNER_METHODS = frozenset({"GET", "POST"})
BLOCKED_BACKOFF = (30.0, 120.0, 300.0)  # waits before each retry after an Imperva block; then the last again
DOCUMENT_LISTS = {
    DocumentKind.fiscal: f"{CPERF}/v2/customers/self/documents/fiscal",
    DocumentKind.cash: f"{CPERF}/v2/cash-accounts/self/documents",
    DocumentKind.securities: f"{CPERF}/v2/securities-accounts/self/documents",
    DocumentKind.pension: f"{CPERF}/v2/pension-accounts/self/documents",
    DocumentKind.portfolio: f"{CPERF}/v2/portfolios/self/documents",
}

logger = logging.getLogger(__name__)


def seg(value: str) -> str:
    """Escape a value used as a path segment (ids, ISINs), so it cannot change the route."""
    return quote(str(value), safe="")


class Period(StrEnum):
    """`groupType` of the position-history endpoints. Others (ONE_WEEK, MAX…) answer 400."""

    month = "ONE_MONTH"
    three_months = "THREE_MONTHS"
    six_months = "SIX_MONTHS"
    year = "ONE_YEAR"


@dataclass(frozen=True)
class Statement:
    """Cash-account movements for a date range, with the balances that bracket them."""

    iban: str | None
    start_balance: Decimal | None
    end_balance: Decimal | None
    flows: list[Flow]


@dataclass(frozen=True)
class FundPage:
    funds: list[FundSummary]
    total: int | None
    last: bool


@dataclass(frozen=True)
class PensionPlanPage:
    plans: list[PensionPlanSummary]
    total: int | None
    last: bool


class MyInvestorClient:
    def __init__(
        self,
        session: Session,
        *,
        http: httpx.Client | None = None,
        store: SessionStore | None = None,
        signature_ui: SignatureUI | None = None,
        signer_factory: Callable[..., Signer] = Signer,
        relogin: Relogin | None = None,
        retry_blocked: int = 0,
        cache: bool = False,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """`retry_blocked`: times to wait (`BLOCKED_BACKOFF`) and repeat a data GET blocked by Imperva.
        `cache`: answer identical GETs (same path and params) from memory for the life of this client.
        """
        if not session.access_token:
            raise NotLoggedInError()
        self._relogin = relogin
        self._session = session
        self._http = http or build_http_client()
        self._owns_http = http is None
        self._store = store
        self._auth = Authenticator(self._http)
        self._ui = signature_ui
        self._signer_factory = signer_factory
        self._retry_blocked = max(retry_blocked, 0)
        self._sleep = sleep or time.sleep  # resolved per instance, so tests can patch `time`
        self._cache: dict[tuple[str, str], Any] | None = {} if cache else None

    @property
    def session(self) -> Session:
        return self._session

    @property
    def signature_ui(self) -> SignatureUI | None:
        return self._ui

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- transport ---------------------------------------------------------

    def get(self, path: str, params: Any = None) -> Any:
        """GET returning the decoded body (whole envelope). Second factors are handled transparently."""
        return self._call("GET", path, params=params)

    def get_data(self, path: str, params: Any = None) -> Any:
        return envelope.data(self.get(path, params))

    def refresh(self) -> Session:
        """New tokens: refresh, else adopt another process's, else log in again with a saved password."""
        try:
            self._session = self._refreshed_tokens()
        except SessionExpiredError:
            if self._relogin is None:
                raise
            logger.debug("refresh imposible: re-login con la contraseña guardada")
            self._session = self._relogin(self._session)
        if self._store:
            try:
                self._store.save(self._session)
            except Exception as exc:  # the old refresh token is already dead: keep working in memory
                logger.warning("No se ha podido guardar la sesión renovada: %s", exc)
        return self._session

    def _refreshed_tokens(self) -> Session:
        try:
            return self._auth.refresh(self._session)
        except SessionExpiredError:
            # Another process (cron + manual run) may have rotated the pair first: adopt its tokens.
            stored = self._store.load() if self._store else None
            if not stored or stored.refresh_token in (None, self._session.refresh_token) or not stored.can_refresh():
                raise
            return stored if not stored.is_expired() else self._auth.refresh(stored)

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"authorization": f"Bearer {self._session.access_token}", "x-device-id": self._session.device_id}
        return {**headers, **(extra or {})}

    def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        blocked_retries: int = 0,
        stream: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """`stream`: a 200 body is left unread for the caller (binary downloads); any other is read."""
        refreshed = False
        if self._session.is_expired():
            logger.debug("token caducado: renovando la sesión")
            self.refresh()
            refreshed = True
        url = f"{API_BASE}/{path.lstrip('/')}"
        response = self._request(method, url, headers, blocked_retries, stream=stream, **kwargs)
        if response.status_code == 401 and not refreshed and (self._session.can_refresh() or self._relogin):
            logger.debug("HTTP 401: renovando la sesión")
            self.refresh()
            response = self._request(method, url, headers, blocked_retries, stream=stream, **kwargs)
        raise_for_response(response)
        return response

    def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        blocked_retries: int,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """One request; with `blocked_retries`, an Imperva block is waited out and repeated."""
        response = self._fetch(method, url, headers, stream, **kwargs)
        for attempt in range(blocked_retries):
            if not _is_waf_block(response):
                break
            wait = BLOCKED_BACKOFF[min(attempt, len(BLOCKED_BACKOFF) - 1)]
            logger.warning(
                "Imperva ha bloqueado la petición (HTTP %d): reintento %d/%d dentro de %.0f s.",
                response.status_code,
                attempt + 1,
                blocked_retries,
                wait,
            )
            self._sleep(wait)
            response = self._fetch(method, url, headers, stream, **kwargs)
        return response

    def _fetch(
        self, method: str, url: str, headers: Mapping[str, str] | None, stream: bool, **kwargs: Any
    ) -> httpx.Response:
        if not stream:
            return self._http.request(method, url, headers=self._headers(headers), **kwargs)
        request = self._http.build_request(method, url, headers=self._headers(headers), **kwargs)
        response = self._http.send(request, stream=True)
        if response.status_code != 200:
            response.read()  # errors, Imperva pages and signature requests are small and inspected as text
        return response

    def _call(self, method: str, path: str, *, params: Any = None) -> Any:
        if method != "GET":
            raise MyInvestorError(f"Petición no permitida en un cliente de solo lectura: {method} {path}")
        key = (path, str(httpx.QueryParams(params)))
        if self._cache is not None and key in self._cache:
            logger.debug("%s %s (caché)", method, path)
            return copy.deepcopy(self._cache[key])
        retries = 0 if path.startswith(SIGNATURE_PREFIX) else self._retry_blocked
        response = self._send(method, path, params=params, blocked_retries=retries)
        body = envelope.decode(response)
        if response.status_code != 202:
            if self._cache is not None:
                self._cache[key] = copy.deepcopy(body)
            return body
        request = parse_signature_request(envelope.data(body))
        if request is None or not request.is_pending:
            raise ApiError(202, "respuesta 202 inesperada (sin firma pendiente)", path)
        self._signer_factory(self._signer_call).complete(request, self._ui)
        response = self._send(method, path, headers={SIGNATURE_HEADER: request.id}, params=params)
        body = envelope.decode(response)
        if response.status_code == 202:
            raise SignatureError("MyInvestor sigue pidiendo firma tras aprobarla.")
        return body

    def _open_binary(self, method: str, path: str, *, params: Any = None) -> httpx.Response:
        """Like `_call` (read-only guard, Imperva retries, second factor) but the 200 body stays unread."""
        if method != "GET":
            raise MyInvestorError(f"Petición no permitida en un cliente de solo lectura: {method} {path}")
        response = self._send(method, path, params=params, blocked_retries=self._retry_blocked, stream=True)
        if response.status_code != 202:
            return response
        request = parse_signature_request(envelope.data(envelope.decode(response)))
        if request is None or not request.is_pending:
            raise ApiError(202, "respuesta 202 inesperada (sin firma pendiente)", path)
        self._signer_factory(self._signer_call).complete(request, self._ui)
        response = self._send(method, path, headers={SIGNATURE_HEADER: request.id}, params=params, stream=True)
        if response.status_code == 202:
            raise SignatureError("MyInvestor sigue pidiendo firma tras aprobarla.")
        return response

    @contextlib.contextmanager
    def stream(self, path: str, params: Any = None) -> Iterator[httpx.Response]:
        """GET whose body is not JSON (PDFs): yields the open response, unread and never cached; closed on exit.

        Same headers, token refresh, 401 retry, Imperva retries and second factor as `get`. The caller
        reads the body (see `documents.pdf_chunks`, which checks it and caps its size).
        """
        response = self._open_binary("GET", path, params=params)
        try:
            yield response
        finally:
            response.close()

    def _signer_call(self, method: str, path: str, *, params: Any = None, json: Any = None) -> Any:
        """The only non-GET requests this client sends: starting/validating a second factor."""
        if method not in SIGNER_METHODS or not path.startswith(SIGNATURE_PREFIX):
            raise MyInvestorError(f"Petición no permitida en un cliente de solo lectura: {method} {path}")
        return envelope.data(envelope.decode(self._send(method, path, params=params, json=json)))

    def _list(self, path: str, params: Any = None) -> list[Any]:
        data = self.get_data(path, params)
        return data if isinstance(data, list) else []

    # ---- customer ----------------------------------------------------------

    def customer(self) -> dict[str, Any]:
        data = self.get_data(f"{CPERF}/v3/customers/self")
        return data if isinstance(data, dict) else {}

    # ---- cash accounts -----------------------------------------------------

    def cash_accounts(self) -> list[CashAccount]:
        return [CashAccount.model_validate(a) for a in self._list(f"{CPERF}/v2/cash-accounts/self")]

    def statement(self, account_uuid: str, date_from: date, date_to: date) -> Statement:
        """Movements of a cash account. Ranges older than ~90 days need a second factor (app)."""
        if date_from > date_to:
            raise ValueError("date_from > date_to")
        params = {"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()}
        data = self.get_data(f"/account/api/v3/account/{seg(account_uuid)}/flows", params)
        if not isinstance(data, dict):
            raise ApiError(200, "respuesta de movimientos inesperada", "flows")
        return Statement(
            iban=data.get("iban"),
            start_balance=data.get("startBalance"),
            end_balance=data.get("endBalance"),
            flows=[Flow.model_validate(f) for f in data.get("flowList") or []],
        )

    def transfers(self, account_id: str) -> list[SentTransfer]:
        """Transfers sent from a cash account (its numeric `accountId`), periodic ones included.

        The API takes no date range (the web only filters by concept and amount): filter locally.
        """
        rows = self._list(f"{CPERF}/v2/cash-accounts/{seg(account_id)}/transfers")
        return [SentTransfer.model_validate(t) for t in rows]

    def remuneration(self, account_id: str) -> Remuneration | None:
        """Last interest settlement of a cash account (its numeric `accountId`); None if there is none."""
        data = self.get_data(f"{CPERF}/v2/cash-accounts/{seg(account_id)}/remuneration")
        return Remuneration.model_validate(data) if isinstance(data, dict) and data else None

    def holds(self, account_uuid: str) -> list[Hold]:
        return [Hold.model_validate(h) for h in self._list(f"/account/api/v3/account/{seg(account_uuid)}/holds")]

    def balance_history(self) -> list[tuple[date, Decimal]]:
        """Daily total of all cash accounts (enabled + withheld), oldest first."""
        rows = self._list(f"{CPERF}/v2/cash-accounts/historical-balance", {"accountTypes": "CASH_ACCOUNT"})
        points = [(date.fromisoformat(str(r["date"])[:10]), Decimal(str(r["balance"]))) for r in rows]
        return sorted(points)

    # ---- securities accounts (funds, ETFs, stocks) -------------------------

    def securities_account_ids(self) -> list[str]:
        rows = self._list(f"{CPERF}/v2/securities-accounts/self-basic")
        return [str(r["accountId"]) for r in rows if r.get("accountId") and r.get("status", "ACTIVE") == "ACTIVE"]

    def securities_account(self, account_id: str) -> SecuritiesAccount:
        return SecuritiesAccount.model_validate(self.get_data(f"{CPERF}/v2/securities-accounts/{seg(account_id)}"))

    def securities_accounts(self) -> list[SecuritiesAccount]:
        return [self.securities_account(i) for i in self.securities_account_ids()]

    def orders(self, account_id: str, date_from: date | None = None, date_to: date | None = None) -> list[Order]:
        """Without dates the API returns only recent/in-progress orders; with them, the full range."""
        params = None
        if date_from or date_to:
            params = {
                "dateFrom": (date_from or date(2000, 1, 1)).isoformat(),
                "dateTo": (date_to or date.today()).isoformat(),
            }
        rows = self._list(f"{CPERF}/v3/securities-accounts/{seg(account_id)}/orders", params)
        return sorted((Order.model_validate(r) for r in rows), key=lambda o: o.order_date, reverse=True)

    def position_history(
        self, account_id: str, period: Period = Period.six_months, *, pension: bool = False
    ) -> dict[str, list[PositionPoint]]:
        kind = "pension-accounts" if pension else "securities-accounts"
        data = self.get_data(f"{CPERF}/v2/{kind}/{seg(account_id)}/positions", {"groupType": period.value})
        positions = data.get("positions") if isinstance(data, dict) else None
        return {
            product: sorted((PositionPoint.model_validate(p) for p in points), key=lambda p: p.date)
            for product, points in (positions or {}).items()
        }

    def order_detail(self, account_id: str, reference: str, *, pension: bool = False) -> OrderDetail:
        """An order with its executions (`relatedOperations`): NAV, shares, fees and tax result."""
        kind = "pension-accounts" if pension else "securities-accounts"
        return OrderDetail.model_validate(self.get_data(f"{CPERF}/v2/{kind}/{seg(account_id)}/orders/{seg(reference)}"))

    def fund_lots(self, account_id: str, isin: str) -> FundLots:
        """Open FIFO lots of a fund with their latent gain (funds only; not realised sales)."""
        path = f"{CPERF}/v2/securities-accounts/{seg(account_id)}/investment-summary/funds/{seg(isin)}/added-values"
        return FundLots.model_validate(self.get_data(path))

    def fund_transfers(self, account_id: str, date_from: date, date_to: date) -> list[FundTransfer]:
        """Fund transfers of a securities account (in, out and between funds) in a range of days.

        Both days are required (without them, 400); the web asks for the last 60 days.
        """
        rows = self._list(f"{CPERF}/v2/securities-accounts/{seg(account_id)}/transfers", _days(date_from, date_to))
        return [FundTransfer.model_validate(r) for r in rows]

    def pending_fund_transfers(self, account_id: str) -> int:
        """How many fund transfers of a securities account are still in progress."""
        data = self.get_data(f"{CPERF}/v2/securities-accounts/{seg(account_id)}/transfers/pending")
        return PendingTransfers.model_validate(data).pending_transfers

    # ---- pension plans -----------------------------------------------------

    def pension_accounts(self) -> list[PensionAccount]:
        return [PensionAccount.model_validate(a) for a in self._list(f"{CPERF}/v2/pension-accounts/self")]

    def pension_plans(self, account_id: str) -> list[PensionPlanPosition]:
        rows = self._list(f"{CPERF}/v2/pension-accounts/{seg(account_id)}")
        return [PensionPlanPosition.model_validate(p) for p in rows]

    def pension_orders(self, account_id: str) -> list[Order]:
        rows = self._list(f"{CPERF}/v2/pension-accounts/{seg(account_id)}/orders", {"order": "DATE"})
        return sorted((Order.model_validate(r) for r in rows), key=lambda o: o.order_date, reverse=True)

    def pension_transfers(self, account_id: str, date_from: date, date_to: date) -> list[PensionTransfer]:
        """Pension-plan transfers of a pension account in a range of days (required, as `fund_transfers`)."""
        rows = self._list(f"{CPERF}/v2/pension-accounts/{seg(account_id)}/transfers", _days(date_from, date_to))
        return [PensionTransfer.model_validate(r) for r in rows]

    def pension_plan(self, dgs_code: str) -> dict[str, Any]:
        data = self.get_data(f"{CPERF}/v2/pension-plans/{seg(dgs_code)}")
        return data if isinstance(data, dict) else {}

    def pension_plan_sheet(self, dgs_code: str) -> PensionPlanSheet:
        """Classification of a pension plan (same request as `pension_plan`)."""
        return PensionPlanSheet.model_validate(self.pension_plan(dgs_code))

    # ---- contributions, catalog, feed --------------------------------------

    def contributions(self) -> list[Contribution]:
        return [Contribution.model_validate(c) for c in self._list(f"{CPERF}/v2/automatic-contributions/self")]

    def fund(self, isin: str) -> dict[str, Any]:
        data = self.get_data(f"{CPERF}/v2/funds/{seg(isin)}")
        return data if isinstance(data, dict) else {}

    def fund_sheet(self, isin: str) -> FundSheet:
        """Asset type, region, category and TER of a fund (same request as `fund`)."""
        return FundSheet.model_validate(self.fund(isin))

    def fund_price(self, isin: str) -> dict[str, Any]:
        data = self.get_data(f"{CPERF}/v2/funds/{seg(isin)}/prices/last")
        return data if isinstance(data, dict) else {}

    def fund_nav_history(self, isin: str) -> list[NavPoint]:
        """NAV of a catalog fund since its launch, oldest first; spacing is irregular (weekly, then daily).

        `INIT_GRAPH` is the only `graphType` the API accepts (`ONE_YEAR`, `MAX`… → 400).
        """
        data = self.get_data(f"{CPERF}/v2/funds/{seg(isin)}/graphs/INIT_GRAPH")
        rows = data.get("investmentPositionValueList") if isinstance(data, dict) else None
        points = (NavPoint.model_validate(r) for r in rows or [])
        return sorted((p for p in points if p.market_value is not None), key=lambda p: p.position_date)

    def funds(
        self,
        query: str | None = None,
        *,
        sort: str = "ter,asc",
        page: int = 0,
        limit: int = FUND_PAGE_SIZE,
        filters: CatalogFilters | None = None,
    ) -> FundPage:
        """Fund catalog; `query` is the web's free-text box (`mainInputFilter`: name, ISIN…). Pages from 0."""
        rows, total, last = self._catalog(f"{CPERF}/v2/funds", query, sort, page, limit, filters, first_page=0)
        return FundPage(funds=[FundSummary.model_validate(r) for r in rows], total=total, last=last)

    def pension_plan_catalog(
        self,
        query: str | None = None,
        *,
        sort: str = "ter,asc",
        page: int = 1,
        limit: int = FUND_PAGE_SIZE,
        filters: CatalogFilters | None = None,
    ) -> PensionPlanPage:
        """Pension-plan catalog, like `funds` but its pages start at 1.

        It does not list the plans the web shows apart (`catalog.FEATURED_PENSION_PLANS`): those are
        read one by one with `pension_plan_sheet`.
        """
        path = f"{CPERF}/v2/pension-plans"
        rows, total, last = self._catalog(path, query, sort, page, limit, filters, first_page=1)
        return PensionPlanPage(plans=[PensionPlanSummary.model_validate(r) for r in rows], total=total, last=last)

    def _catalog(
        self,
        path: str,
        query: str | None,
        sort: str,
        page: int,
        limit: int,
        filters: CatalogFilters | None,
        *,
        first_page: int,
    ) -> tuple[list[Any], int | None, bool]:
        """Page `page` (of `limit` rows) of a catalog, asking for at most CATALOG_MAX_LIMIT rows a request.

        The API caps a page at 50 rows but still uses the `limit` asked for to place it: `limit=100`
        returned the last 5 of 55 plans (#68). A larger `limit` becomes several pages of 50.
        """
        if limit <= CATALOG_MAX_LIMIT:
            return _catalog_page(self.get(path, _catalog_params(query, sort, page, limit, filters)))
        start, end = (page - first_page) * limit, (page - first_page + 1) * limit
        rows: list[Any] = []
        total, last = None, True
        for chunk in range(start // CATALOG_MAX_LIMIT, (end - 1) // CATALOG_MAX_LIMIT + 1):
            params = _catalog_params(query, sort, chunk + first_page, CATALOG_MAX_LIMIT, filters)
            data, total, last = _catalog_page(self.get(path, params))
            offset = chunk * CATALOG_MAX_LIMIT
            rows.extend(data[max(start - offset, 0) : end - offset])
            if last or len(data) < CATALOG_MAX_LIMIT:
                return rows, total, True
        return rows, total, last

    def catalog_filter(self, domain: Domain, kind: FilterKind) -> list[str]:
        """Valid values of a searcher filter, spelled exactly as the API sends them (blanks included)."""
        data = self.get_data(f"{CPERF}/v2/masters/{domain.value}/searcher/filters/{kind.path}")
        return [v for v in data if isinstance(v, str)] if isinstance(data, list) else []

    # ---- robo-advisor portfolios ------------------------------------------

    def portfolios(self) -> list[Portfolio]:
        """The robo-advisor catalog, each portfolio with its target funds and weights."""
        return [Portfolio.model_validate(p) for p in self._list(f"{CPERF}/v2/portfolios")]

    def portfolio_annual_returns(self, account_id: str) -> list[AnnualReturn]:
        """Yearly return of a contracted portfolio (its securities `accountId`), as the web asks for it."""
        params = {"page": 1, "limit": PORTFOLIO_RETURN_YEARS}
        return annual_returns(self.get_data(f"{CPERF}/v3/portfolios/{seg(account_id)}/returns/summary", params))

    # ---- documents (PDF) ----------------------------------------------------

    def documents(self, kinds: Iterable[DocumentKind] = tuple(DocumentKind)) -> list[Document]:
        """The document lists of `kinds` (all five by default), in that order."""
        return [Document.from_api(row, kind) for kind in kinds for row in self._list(DOCUMENT_LISTS[kind])]

    def document_pdf(self, document_uuid: str) -> contextlib.AbstractContextManager[httpx.Response]:
        return self.stream(f"{CPERF}/v2/customers/self/documents/signedes/{seg(document_uuid)}")

    def xray_pdf(self, target: XrayTarget, key: str) -> contextlib.AbstractContextManager[httpx.Response]:
        """Portfolio «x-ray» generated on the fly: securities or pension account id, or fund ISIN."""
        return self.stream(f"{CPERF}/v2/documents/{target.value}/{seg(key)}/x-ray")

    def account_summary(self, account_id: str) -> TemporaryDocument:
        """Asks for a cash-account statement; the PDF is then `temporary_document_pdf(id)`."""
        data = self.get_data(f"{CPERF}/v2/cash-accounts/{seg(account_id)}/documents/summary")
        if not isinstance(data, dict) or not data.get("id"):
            raise ApiError(200, "respuesta de extracto inesperada (sin id)", "documents/summary")
        return TemporaryDocument.model_validate(data)

    def temporary_document_pdf(self, file_id: str) -> contextlib.AbstractContextManager[httpx.Response]:
        return self.stream(f"{CPERF}/v2/documents/temporary/{seg(file_id)}")

    def feed(self) -> list[FeedItem]:
        data = self.get_data("/feed/api/feed/self")
        items = data.get("feed") if isinstance(data, dict) else None
        return [FeedItem.model_validate(i) for i in items or []]


def _days(date_from: date, date_to: date) -> dict[str, str]:
    """`initDate`/`endDate` of the transfer lists, `AAAA-MM-DD`."""
    if date_from > date_to:
        raise ValueError("date_from > date_to")
    return {"initDate": date_from.isoformat(), "endDate": date_to.isoformat()}


def _catalog_params(
    query: str | None, sort: str, page: int, limit: int, filters: CatalogFilters | None
) -> dict[str, Any]:
    """Without `query` nor filters, exactly `page`, `limit` and `sort`."""
    params: dict[str, Any] = {"page": page, "limit": limit, "sort": sort}
    if query:
        params["mainInputFilter"] = query
    return {**params, **(filters.params() if filters else {})}


def _catalog_page(body: Any) -> tuple[list[Any], int | None, bool]:
    """Rows, `totalElements` and whether it is the last page of a catalog response."""
    pagination = envelope.payload(body).get("pagination") or {}
    return envelope.data(body) or [], pagination.get("totalElements"), bool(pagination.get("last", True))


def ensure_session(session: Session | None, *, can_relogin: bool = False) -> Session:
    if session is None or not session.access_token:
        raise NotLoggedInError()
    if session.is_expired() and not session.can_refresh() and not can_relogin:
        raise SessionExpiredError()
    return session
