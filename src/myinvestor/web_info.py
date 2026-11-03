"""Public, unauthenticated facts about the web app: its current version and the maintenance flags.

- The version is a constant of the web bundle, the one it sends as `x-myinvestor-app:
  version=${X},platform=…`. It is found by following that header to the constant's definition.
  Vite/rolldown does not keep it in the entry (`/assets/index-<hash>.js`) but in a chunk the entry
  imports statically (`import{…}from"./api-<hash>.js"`), so those imports are searched too.
- `config.myinvestor.es/flags/settings.json` is what the web polls for its mode:
  `{"mode_name": "transactional" | "read_only" | "maintenance", "min_version": {"android", "ios", "web"},
  "active_from"?, "active_until"?, "exception"?: {"mode_name", "enabled_clients": [customer ids]}}`.
  The web applies the mode as is (the dates are informative) and the exception to the listed customers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin

import httpx

from myinvestor.config import CONFIG_ORIGIN, WEB_ORIGIN

SETTINGS_URL = f"{CONFIG_ORIGIN}/flags/settings.json"

_SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_SRC_ATTR = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MODULE_ATTR = re.compile(r"""\btype\s*=\s*["']module["']""", re.IGNORECASE)
_VERSION_HEADER = re.compile(r"""["']?x-myinvestor-app["']?\s*:\s*`version=\$\{([\w$]+)\}""")
_SEMVER = re.compile(r"^\d+(?:\.\d+)+$")
# Top-level static imports of a chunk: `from"./x.js"` and side-effect `import"./x.js"` (same origin only).
_STATIC_IMPORT = re.compile(r"""(?:\bfrom|\bimport)\s*["'](\.{1,2}/[^"'?#]+\.js)["']""")

MAX_CHUNK_DOWNLOADS = 5  # imported chunks fetched after the entry
MAX_SCRIPT_BYTES = 8 * 1024 * 1024  # the chunk with the version is ~3.2 MB


class WebInfoError(Exception):
    """The public page or flags could not be read or no longer look as expected."""


class WebMode(StrEnum):
    transactional = "transactional"
    read_only = "read_only"
    maintenance = "maintenance"


@dataclass(frozen=True)
class WebSettings:
    mode: WebMode
    min_web_version: str | None
    active_from: str | None
    active_until: str | None
    exception_mode: WebMode | None
    exception_clients: frozenset[str]

    def mode_for(self, customer_id: str | None) -> WebMode:
        """Like the web: listed customers get the exception's mode."""
        if self.exception_mode and customer_id and customer_id.strip().upper() in self.exception_clients:
            return self.exception_mode
        return self.mode


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def entry_script_url(html: str, base: str = WEB_ORIGIN) -> str | None:
    """`src` of the first `<script type="module">` (the Vite entry bundle)."""
    for match in _SCRIPT_TAG.finditer(html):
        attrs = match.group(1)
        src = _SRC_ATTR.search(attrs)
        if src and _MODULE_ATTR.search(attrs):
            return str(urljoin(base + "/", src.group(1)))
    return None


def bundle_version(js: str) -> str | None:
    """Value of the constant interpolated in the `x-myinvestor-app` header."""
    header = _VERSION_HEADER.search(js)
    if not header:
        return None
    definition = re.search(rf"(?<![\w$.]){re.escape(header.group(1))}=`([^`]+)`", js)
    if not definition or not _SEMVER.match(definition.group(1)):
        return None
    return definition.group(1)


def static_imports(js: str, script_url: str) -> list[str]:
    """Absolute URLs of the chunks `js` imports statically, `api-*` first, without duplicates."""
    urls = list(dict.fromkeys(urljoin(script_url, path) for path in _STATIC_IMPORT.findall(js)))
    return sorted(urls, key=lambda url: not url.rsplit("/", 1)[-1].startswith("api-"))


def _fetch_script(http: httpx.Client, url: str) -> str | None:
    """Body of a script, or None if it fails or is larger than MAX_SCRIPT_BYTES (read in streaming)."""
    with http.stream("GET", url, headers={"accept": "*/*"}) as response:
        if not response.is_success:
            return None
        if int(response.headers.get("content-length") or 0) > MAX_SCRIPT_BYTES:
            return None
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_SCRIPT_BYTES:
                return None
    return body.decode("utf-8", errors="replace")


def fetch_web_version(http: httpx.Client) -> str:
    """Raises WebInfoError (or httpx.HTTPError) when it cannot be determined."""
    page = http.get(f"{WEB_ORIGIN}/", headers={"accept": "text/html"})
    if not page.is_success:
        raise WebInfoError(f"{WEB_ORIGIN} responde HTTP {page.status_code}")
    entry_url = entry_script_url(page.text)
    if entry_url is None:
        raise WebInfoError("no se encuentra el script principal en la página")
    entry = _fetch_script(http, entry_url)
    if entry is None:
        raise WebInfoError("no se puede descargar el script principal")
    if version := bundle_version(entry):
        return version
    chunks = static_imports(entry, entry_url)[:MAX_CHUNK_DOWNLOADS]
    for url in chunks:
        script = _fetch_script(http, url)
        if script is not None and (version := bundle_version(script)):
            return version
    raise WebInfoError(f"no se encuentra la versión en el script principal ni en {len(chunks)} de sus imports")


def _mode(value: Any) -> WebMode:
    try:
        return WebMode(value)
    except ValueError as exc:
        raise WebInfoError(f"modo desconocido en settings.json: {value!r}") from exc


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_settings(raw: Any) -> WebSettings:
    if not isinstance(raw, dict):
        raise WebInfoError("settings.json no es un objeto")
    min_version = raw.get("min_version")
    web_min = min_version.get("web") if isinstance(min_version, dict) else None
    exception = raw.get("exception") if isinstance(raw.get("exception"), dict) else None
    clients = exception.get("enabled_clients") if exception else None
    return WebSettings(
        mode=_mode(raw.get("mode_name")),
        min_web_version=web_min if isinstance(web_min, str) and _SEMVER.match(web_min) else None,
        active_from=_optional_str(raw.get("active_from")),
        active_until=_optional_str(raw.get("active_until")),
        exception_mode=_mode(exception.get("mode_name")) if exception else None,
        exception_clients=frozenset(str(c).strip().upper() for c in clients if isinstance(c, str))
        if isinstance(clients, list)
        else frozenset(),
    )


def fetch_settings(http: httpx.Client) -> WebSettings:
    response = http.get(SETTINGS_URL)
    if not response.is_success:
        raise WebInfoError(f"settings.json responde HTTP {response.status_code}")
    try:
        raw = response.json()
    except ValueError as exc:
        raise WebInfoError("settings.json no es JSON") from exc
    return parse_settings(raw)
