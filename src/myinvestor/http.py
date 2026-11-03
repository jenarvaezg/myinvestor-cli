"""HTTP client factory with retries on by default, optional pacing and a secret-free request log.

Only idempotent methods are retried: login, token refresh and signature calls (POST)
must never be replayed, or we would burn password attempts and one-time codes.

Every request sent on the wire is logged at DEBUG on `myinvestor.http` (`--verbose`) as
`METHOD /path STATUS (N ms) [tags]`: never the query string, headers or bodies, which carry
tokens, passwords, codes and signature ids.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import httpx
from httpx_retries import Retry, RetryTransport

from myinvestor.config import DEFAULT_HEADERS, LOGIN_PATH, REFRESH_PATH
from myinvestor.signature import SIGNATURE_HEADER

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DEFAULT_RETRIES = 4
DEFAULT_BACKOFF = 0.5  # 0.5s, 1s, 2s, 4s (+ jitter)
DEFAULT_TIMEOUT = 60.0

# `/signature/api/v3[/public]/signature/<request id>[/validate/<signature id>]`
SIGNATURE_PATH = re.compile(r"^(/signature/api/v3/(?:public/)?signature)(/.*)?$")

logger = logging.getLogger(__name__)


class Pacer:
    """Keeps at least `interval` seconds between the starts of consecutive requests."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if interval < 0:
            raise ValueError("interval must be >= 0")
        self._interval = interval
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._interval <= 0:
            return
        if self._last is not None:
            missing = self._interval - (self._clock() - self._last)
            if missing > 0:
                self._sleep(missing)
        self._last = self._clock()


def log_path(path: str) -> str:
    """The path as logged: signature request/factor ids are masked (they authorise a signed call)."""
    match = SIGNATURE_PATH.match(path)
    if not match or not match.group(2):
        return path
    rest = "/".join(part if part in ("", "validate") else "*" for part in match.group(2).split("/"))
    return match.group(1) + rest


def _tags(request: httpx.Request) -> list[str]:
    path = request.url.path
    tags = []
    if path == REFRESH_PATH:
        tags.append("refresh")
    elif path == LOGIN_PATH:
        tags.append("login")
    elif SIGNATURE_PATH.match(path):
        tags.append("firma")
    if SIGNATURE_HEADER in request.headers:
        tags.append("firmada")
    return tags


class PacedLoggingTransport(httpx.BaseTransport):
    """Paces and logs every attempt that reaches the network (retries included)."""

    def __init__(self, inner: httpx.BaseTransport, pacer: Pacer | None = None) -> None:
        self._inner = inner
        self._pacer = pacer

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._pacer:
            self._pacer.wait()
        start = time.perf_counter()
        try:
            response = self._inner.handle_request(request)
        except httpx.HTTPError as exc:
            logger.debug("%s %s %s", request.method, log_path(request.url.path), type(exc).__name__)
            raise
        elapsed = (time.perf_counter() - start) * 1000
        tags = _tags(request)
        suffix = f" [{', '.join(tags)}]" if tags else ""
        logger.debug(
            "%s %s %d (%.0f ms)%s", request.method, log_path(request.url.path), response.status_code, elapsed, suffix
        )
        return response

    def close(self) -> None:
        self._inner.close()


def build_retry(retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF) -> Retry:
    return Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=RETRY_METHODS,
        respect_retry_after_header=True,
        max_backoff_wait=20.0,
    )


def build_http_client(
    *,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    timeout: float = DEFAULT_TIMEOUT,
    min_interval: float = 0.0,
    pacer: Pacer | None = None,
) -> httpx.Client:
    """`min_interval` (or an explicit `pacer`) spaces out the requests sent through this client."""
    paced = PacedLoggingTransport(httpx.HTTPTransport(), pacer or Pacer(min_interval))
    transport = RetryTransport(transport=paced, retry=build_retry(retries, backoff))
    return httpx.Client(transport=transport, timeout=timeout, headers=DEFAULT_HEADERS)
