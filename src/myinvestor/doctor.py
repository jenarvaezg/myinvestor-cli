"""`doctor` checks: store, session, network/WAF, API, clock, web version and maintenance.

Each check returns one `Check` (OK/WARN/FAIL); nothing here prints. Every dependency (store, HTTP
client, API client factory, clocks) is passed in, so the checks run against respx in tests.
"""

from __future__ import annotations

import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from keyring.errors import KeyringError

from myinvestor.client import MyInvestorClient
from myinvestor.config import API_BASE, APP_VERSION, KEYRING_SERVICE, TOKEN_EXPIRY_SKEW
from myinvestor.errors import (
    CorruptKeyringError,
    CorruptStoreError,
    MyInvestorError,
    NetworkBlockedError,
    _is_waf_block,
)
from myinvestor.session import Session
from myinvestor.session_info import SessionInfo, store_kind, store_path
from myinvestor.stores import SessionStore
from myinvestor.web_info import WebInfoError, WebMode, WebSettings, fetch_settings, fetch_web_version, version_tuple

# Cheap unauthenticated GET: the web asks it before login. Any non-WAF answer proves reachability.
PROBE_PATH = "/login/api/v1/features"
CLOCK_FAIL_SECONDS = 300  # minutes off: tokens look expired (or alive) when they are not
PRIVATE_MODE = 0o600


class Level(StrEnum):
    ok = "OK"
    warn = "WARN"
    fail = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.name, "status": self.level.value, "message": self.message, "data": dict(self.data)}


def _hhmm(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%H:%M %Z") if moment else "?"


def _not_checked(name: str, why: str) -> Check:
    return Check(name, Level.warn, f"no comprobado: {why}")


# ---- store -------------------------------------------------------------------


def _permission_problem(path: Path) -> str | None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return f"{path} tiene permisos {mode:04o} (deberían ser 0600: `chmod 600 {path}`)"
    return None


def check_store(store: SessionStore) -> tuple[Check, SessionInfo | None]:
    """Also reads the session once, so a corrupt store is reported here and not as a crash."""
    kind, path = store_kind(store), store_path(store)
    data: dict[str, Any] = {"store": kind, "store_path": str(path) if path else None}
    try:
        info = SessionInfo.read(store)
    except (CorruptStoreError, CorruptKeyringError) as exc:
        return Check("store", Level.fail, str(exc), data), None
    except (KeyringError, OSError) as exc:
        return Check("store", Level.fail, f"no se puede leer el almacén {kind}: {exc}", data), None
    if path is None:
        where = f"llavero del sistema (servicio {KEYRING_SERVICE})" if kind == "keyring" else kind
        return Check("store", Level.ok, where, data), info
    if not path.exists():
        return Check("store", Level.warn, f"file {path}: no existe todavía (haz `myinvestor login`)", data), info
    files = [f for f in (path, path.with_suffix(".lock")) if f.exists()]
    problems = [p for p in map(_permission_problem, files) if p]
    if problems:
        return Check("store", Level.fail, "; ".join(problems), data), info
    return Check("store", Level.ok, f"file {path} (0600, JSON válido)", data), info


# ---- session -----------------------------------------------------------------


def _worse(a: Level, b: Level) -> Level:
    order = list(Level)
    return a if order.index(a) >= order.index(b) else b


def _session_state(info: SessionInfo, session: Session, now: datetime) -> tuple[Level, str]:
    if not info.is_usable(now):
        if info.relogin_block:
            return Level.fail, "caducada y el re-login automático está en pausa: ejecuta `myinvestor login`"
        return Level.fail, "caducada: ejecuta `myinvestor login`"
    if not session.is_expired(now):
        return Level.ok, "activa"
    if info.can_refresh(now):
        return Level.ok, "token caducado, se renovará en la próxima orden"
    return Level.ok, "caducada, se volverá a entrar con la contraseña guardada"


def check_session(info: SessionInfo | None, now: datetime) -> Check:
    if info is None:
        return Check("sesion", Level.fail, "no se puede leer (ver store)")
    data = {k: v for k, v in info.to_dict().items() if k not in ("name", "verified_online", "store", "store_path")}
    session = info.session
    if session is None or not info.has_tokens:
        return Check("sesion", Level.fail, "sin sesión: ejecuta `myinvestor login`", data)
    level, state = _session_state(info, session, now)
    parts = [
        state,
        f"token hasta las {_hhmm(session.expires_at)}, renovable hasta las {_hhmm(session.refresh_expires_at)}",
        f"contraseña guardada: {'sí' if info.password_remembered else 'no'}",
    ]
    if info.relogin_block:
        parts.append(f"re-login en pausa hasta las {_hhmm(info.relogin_block.until)} ({info.relogin_block.error})")
        level = _worse(level, Level.warn)
    if not session.customer_id:
        parts.append("sin DNI/NIE guardado")
    if not session.device_id:
        parts.append("sin id de dispositivo: el próximo login pedirá segundo factor")
        level = _worse(level, Level.warn)
    return Check("sesion", level, "; ".join(parts), data)


# ---- network, clock ------------------------------------------------------------


def check_network(
    http: httpx.Client, clock: Callable[[], float] = time.monotonic
) -> tuple[Check, httpx.Response | None]:
    url = f"{API_BASE}{PROBE_PATH}"
    start = clock()
    try:
        response = http.get(url)
    except httpx.HTTPError as exc:
        return Check("red", Level.fail, f"sin respuesta de {API_BASE} ({type(exc).__name__})"), None
    latency = round((clock() - start) * 1000)
    data = {"http_status": response.status_code, "latency_ms": latency}
    if _is_waf_block(response):
        return Check("red", Level.fail, str(NetworkBlockedError(response.status_code)), data), response
    if response.status_code >= 500:
        message = f"{API_BASE} responde HTTP {response.status_code} ({latency} ms): MyInvestor puede estar caído"
        return Check("red", Level.warn, message, data), response
    message = f"{API_BASE} responde (HTTP {response.status_code}, {latency} ms), sin bloqueo de Imperva"
    return Check("red", Level.ok, message, data), response


def check_clock(response: httpx.Response | None, now: datetime) -> Check:
    if response is None:
        return _not_checked("reloj", "el servidor no ha respondido")
    try:
        server = parsedate_to_datetime(response.headers["date"])
    except (KeyError, TypeError, ValueError):
        return _not_checked("reloj", "la respuesta no trae cabecera Date")
    if server.tzinfo is None:
        server = server.replace(tzinfo=UTC)
    skew = round((now - server).total_seconds())
    data = {"skew_seconds": skew}
    message = f"desfase de {skew:+d} s con el servidor"
    if abs(skew) <= TOKEN_EXPIRY_SKEW:
        return Check("reloj", Level.ok, message, data)
    impact = f"la caducidad de los tokens se calcula con el reloj local y el margen es de {TOKEN_EXPIRY_SKEW} s"
    level = Level.fail if abs(skew) > CLOCK_FAIL_SECONDS else Level.warn
    return Check("reloj", level, f"{message}: {impact}; sincroniza la hora (NTP)", data)


# ---- API with the session ----------------------------------------------------


def check_api(client_factory: Callable[[], MyInvestorClient]) -> Check:
    try:
        with client_factory() as client:
            client.customer()
    except MyInvestorError as exc:
        return Check("api", Level.fail, str(exc), {"exit_code": exc.exit_code})
    except httpx.HTTPError as exc:
        return Check("api", Level.fail, f"error de red con la sesión ({type(exc).__name__})")
    return Check("api", Level.ok, "MyInvestor acepta la sesión (el perfil de cliente responde)")


# ---- web version and maintenance -----------------------------------------------


def check_version(http: httpx.Client, settings: WebSettings | None) -> Check:
    minimum = settings.min_web_version if settings else None
    data: dict[str, Any] = {"app_version": APP_VERSION, "min_web_version": minimum}
    if minimum and version_tuple(APP_VERSION) < version_tuple(minimum):
        message = f"el CLI se presenta como {APP_VERSION}, por debajo de la mínima de la web ({minimum})"
        return Check("version", Level.warn, message, data)
    try:
        web = fetch_web_version(http)
    except (WebInfoError, httpx.HTTPError) as exc:
        reason = str(exc) if isinstance(exc, WebInfoError) else type(exc).__name__
        return Check("version", Level.warn, f"no comprobable ({reason}); el CLI se presenta como {APP_VERSION}", data)
    data["web_version"] = web
    if web == APP_VERSION:
        return Check("version", Level.ok, f"{APP_VERSION}, la misma que la web", data)
    message = (
        f"la web va por la {web} y el CLI se presenta como {APP_VERSION}; "
        "si empiezan a fallar las peticiones, puede que haya cambiado la API"
    )
    return Check("version", Level.warn, message, data)


def check_maintenance(settings: WebSettings | None, error: str | None, customer_id: str | None) -> Check:
    if settings is None:
        return _not_checked("mantenimiento", error or "settings.json no disponible")
    mode = settings.mode_for(customer_id)
    bounds = (("desde", settings.active_from), ("hasta", settings.active_until))
    window = " ".join(f"{label} {value}" for label, value in bounds if value)
    data = {"mode": mode.value, "active_from": settings.active_from, "active_until": settings.active_until}
    suffix = f" ({window})" if window else ""
    if mode is WebMode.maintenance:
        return Check("mantenimiento", Level.fail, f"MyInvestor está en mantenimiento{suffix}", data)
    if mode is WebMode.read_only:
        message = f"la web está en modo solo lectura{suffix}; el CLI solo lee, pero puede haber incidencias"
        return Check("mantenimiento", Level.warn, message, data)
    return Check("mantenimiento", Level.ok, "sin mantenimiento anunciado", data)


def _settings(http: httpx.Client) -> tuple[WebSettings | None, str | None]:
    try:
        return fetch_settings(http), None
    except WebInfoError as exc:
        return None, str(exc)
    except httpx.HTTPError as exc:
        return None, f"settings.json sin respuesta ({type(exc).__name__})"


# ---- runner ------------------------------------------------------------------


def run_doctor(
    store: SessionStore,
    *,
    http: httpx.Client | None,
    client_factory: Callable[[], MyInvestorClient] | None,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    clock: Callable[[], float] = time.monotonic,
) -> list[Check]:
    """`http=None` is `--offline`: only the local checks run, nothing touches the network nor the session."""
    store_check, info = check_store(store)
    checks = [store_check, check_session(info, now())]
    if http is None:
        offline = ("red", "api", "reloj", "version", "mantenimiento")
        return [*checks, *(_not_checked(name, "--offline") for name in offline)]
    network, response = check_network(http, clock)
    checks.append(network)
    if client_factory is None or info is None or not info.is_usable(now()):
        checks.append(_not_checked("api", "no hay sesión utilizable"))
    else:
        checks.append(check_api(client_factory))
    checks.append(check_clock(response, now()))
    settings, error = _settings(http)
    customer_id = info.session.customer_id if info and info.session else None
    return [*checks, check_version(http, settings), check_maintenance(settings, error, customer_id)]


def has_failures(checks: list[Check]) -> bool:
    return any(c.level is Level.fail for c in checks)
