"""Login in a real browser, because the web login requires reCAPTCHA Enterprise.

`myinvestor login` opens newapp.myinvestor.es in a Chromium window driven by Playwright (optional
extra `myinvestor[browser]`). The person logs in as usual, the web app stores its Keycloak tokens
in localStorage (`myinvestor-app:TOKEN` / `REFRESH_TOKEN`) and we copy them. The device id is the
Capacitor web id (`_capuid`), which the web sends as `x-device-id`; the browser profile is kept in
`$XDG_DATA_HOME/myinvestor/browser` so that device stays known and the SMS is only asked once.

Once copied, the tokens are removed from the profile: the browser must not keep refreshing a
session the CLI now owns. Passwords never pass through this code.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from myinvestor.config import SIGNIN_URL, WEB_ORIGIN
from myinvestor.errors import AuthError
from myinvestor.session import Session

try:  # optional extra `myinvestor[browser]`
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the install
    PlaywrightError = Exception  # type: ignore[assignment,misc]
    sync_playwright = None  # type: ignore[assignment]

STORAGE_PREFIX = "myinvestor-app:"
TOKEN_KEY = f"{STORAGE_PREFIX}TOKEN"
REFRESH_KEY = f"{STORAGE_PREFIX}REFRESH_TOKEN"
EXPIRES_KEY = f"{STORAGE_PREFIX}TOKEN_EXPIRES_AT"
FORM_KEY = f"{STORAGE_PREFIX}LOGIN_FORM_VALUES"
DEVICE_KEY = "_capuid"
TOKEN_KEYS = (TOKEN_KEY, REFRESH_KEY, EXPIRES_KEY)

DEFAULT_LOGIN_TIMEOUT = 300.0
MAX_READ_FAILURES = 30  # consecutive failed localStorage reads before giving up

logger = logging.getLogger(__name__)

_READ_STORAGE = """keys => Object.fromEntries(keys.map(k => [k, localStorage.getItem(k)]))"""
_DROP_TOKENS = """keys => keys.forEach(k => localStorage.removeItem(k))"""


def default_profile_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "myinvestor" / "browser"


def _unquote(value: str | None) -> str | None:
    """The web app stores some values JSON-encoded (`"eyJ…"`), others raw."""
    if not value:
        return None
    if value.startswith(('"', "{", "[")):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, str) else value
    return value


def session_from_storage(storage: dict[str, str | None]) -> Session | None:
    """Build a Session from the web app's localStorage, or None while not logged in."""
    access, refresh = _unquote(storage.get(TOKEN_KEY)), _unquote(storage.get(REFRESH_KEY))
    if not access or not refresh:
        return None
    form: dict[str, Any] = {}
    raw_form = storage.get(FORM_KEY)
    if raw_form:
        try:
            parsed = json.loads(raw_form)
            form = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            form = {}
    device_id = _unquote(storage.get(DEVICE_KEY)) or form.get("deviceId")
    if not device_id:
        raise AuthError("No encuentro el id de dispositivo de la web (localStorage `_capuid`).")
    customer_id = form.get("customerId")
    return Session(
        device_id=str(device_id),
        customer_id=str(customer_id) if customer_id else None,
        access_token=access,
        refresh_token=refresh,
    )


def _private_dir(path: Path) -> None:
    """The profile holds MyInvestor cookies and device id: owner-only, also if it already existed."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def browser_login(
    *, profile_dir: Path | None = None, timeout: float = DEFAULT_LOGIN_TIMEOUT, poll: float = 1.0
) -> Session:
    if sync_playwright is None:
        raise AuthError(
            "El login con navegador necesita Playwright: `uv tool install 'myinvestor[browser]'` "
            "(o `uv sync --extra browser`) y `playwright install chromium`. "
            "Alternativas: `myinvestor login --direct` o `--refresh-token-stdin`."
        )
    profile = profile_dir or default_profile_dir()
    _private_dir(profile)
    try:
        with sync_playwright() as pw:
            context = _launch(pw, profile)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                return capture_session(page, timeout=timeout, poll=poll)
            finally:
                with contextlib.suppress(PlaywrightError):
                    context.close()
    except PlaywrightError as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise AuthError(f"El navegador ha fallado: {first_line}") from exc


def capture_session(
    page: Any,
    *,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Session:
    """Wait on an open MyInvestor page until the person logs in, then take the session over.

    Tokens already in the profile (a previous run) are dropped first and never accepted: the web
    app could write them back from memory, and returning them would report a dead session as new.
    """
    keys = [*TOKEN_KEYS, FORM_KEY, DEVICE_KEY]
    page.goto(WEB_ORIGIN, wait_until="domcontentloaded")
    before = page.evaluate(_READ_STORAGE, keys)
    stale = {_unquote(before.get(TOKEN_KEY)), _unquote(before.get(REFRESH_KEY))} - {None}
    _drop_tokens(page)
    page.goto(SIGNIN_URL, wait_until="domcontentloaded")
    deadline = clock() + timeout
    failures = 0
    while clock() < deadline:
        if page.is_closed():
            raise AuthError("Has cerrado el navegador sin terminar el login.")
        try:
            storage = page.evaluate(_READ_STORAGE, keys)
            failures = 0
        except PlaywrightError as exc:  # usually the SPA navigating; a dead page keeps failing
            failures += 1
            logger.debug("reading localStorage failed (%d): %s", failures, exc)
            if failures >= MAX_READ_FAILURES:
                raise AuthError("No consigo leer la página de MyInvestor en el navegador.") from exc
            sleep(poll)
            continue
        session = session_from_storage(storage)
        if session is not None and session.refresh_token not in stale and session.can_refresh():
            try:
                _drop_tokens(page)
            except PlaywrightError:
                logger.warning("No se han podido borrar los tokens del perfil del navegador.")
            return session
        sleep(poll)
    raise AuthError(f"No se ha completado el login en {int(timeout)} s.")


def _drop_tokens(page: Any) -> None:
    """Remove the tokens from localStorage; retried once because the SPA may be mid-navigation."""
    try:
        page.evaluate(_DROP_TOKENS, list(TOKEN_KEYS))
    except PlaywrightError:
        page.wait_for_load_state("domcontentloaded")
        page.evaluate(_DROP_TOKENS, list(TOKEN_KEYS))


def _launch(pw: Any, profile: Path) -> Any:
    """Prefer the installed Chrome (no download); fall back to Playwright's Chromium."""
    options = {"headless": False, "viewport": None, "locale": "es-ES"}
    try:
        return pw.chromium.launch_persistent_context(str(profile), channel="chrome", **options)
    except PlaywrightError as exc:
        logger.debug("Chrome channel unavailable, falling back to bundled Chromium: %s", exc)
        return pw.chromium.launch_persistent_context(str(profile), **options)
