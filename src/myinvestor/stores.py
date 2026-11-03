"""Where the session lives: OS keyring, a 0600 file, or memory.

The password is stored only after `login --direct --remember`, keyed by customer id, so an expired
session can log in again unattended (cron). It is deleted on logout and when MyInvestor says it is
wrong or must be changed (`UNAUTHORIZED`, `UPDATE_PASSWORD_REQUIRED`). Any other rejection keeps it
but pauses the re-login (`ReloginBlock`) so cron does not retry it on every run.

`login --direct --start` leaves a `PendingLogin` (the SMS already sent) that `login --direct --code`
completes in another process. It holds the password only with `--remember`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import keyring

from myinvestor.config import KEYRING_SERVICE
from myinvestor.errors import CorruptKeyringError, CorruptStoreError
from myinvestor.session import Session

_NO_KEYRING_MODULES = {"keyring.backends.fail", "keyring.backends.null"}


@dataclass(frozen=True)
class ReloginBlock:
    """No unattended re-login before `until` (UTC); `error` is the rejection that caused it."""

    until: datetime
    error: str

    def is_active(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(tz=UTC)) < self.until

    def to_dict(self) -> dict[str, str]:
        return {"relogin_blocked_until": self.until.isoformat(), "last_relogin_error": self.error}

    @classmethod
    def from_dict(cls, raw: Any) -> ReloginBlock:
        """Raises ValueError/TypeError/KeyError on malformed data."""
        until = datetime.fromisoformat(raw["relogin_blocked_until"])
        if until.tzinfo is None:
            raise ValueError("relogin_blocked_until without timezone")
        return cls(until=until, error=str(raw["last_relogin_error"]))


@dataclass(frozen=True)
class PendingLogin:
    """A `login --direct --start` waiting for its code: what repeating the login with `x-signature-id` needs.

    The web repeats the very same `POST /login/api/v2/auth/token` (`customerId`, `password`, same device)
    adding `x-signature-id: <signatureRequestId>` once the code is validated, so the password is needed
    again: it is kept here only with `--remember` (it would be saved anyway); otherwise `--code` asks for it.
    """

    customer_id: str
    device_id: str
    request_id: str  # signatureRequestId: sent back as `x-signature-id`
    signature_id: str  # the started code method, for `…/validate/{signatureId}`
    method: str
    destination: str | None
    started_at: datetime
    wrong_codes: int = 0
    password: str | None = field(default=None, repr=False)

    def with_wrong_code(self) -> PendingLogin:
        return replace(self, wrong_codes=self.wrong_codes + 1)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "started_at": self.started_at.isoformat()}

    @classmethod
    def from_dict(cls, raw: Any) -> PendingLogin:
        """Raises ValueError/TypeError/KeyError on malformed data."""
        started_at = datetime.fromisoformat(raw["started_at"])
        if started_at.tzinfo is None:
            raise ValueError("started_at without timezone")
        password = raw.get("password")
        return cls(
            customer_id=str(raw["customer_id"]),
            device_id=str(raw["device_id"]),
            request_id=str(raw["request_id"]),
            signature_id=str(raw["signature_id"]),
            method=str(raw["method"]),
            destination=str(raw["destination"]) if raw.get("destination") else None,
            started_at=started_at,
            wrong_codes=int(raw.get("wrong_codes", 0)),
            password=str(password) if password else None,
        )


class StoreKind(StrEnum):
    auto = "auto"
    keyring = "keyring"
    file = "file"


class SessionStore(Protocol):
    def load(self) -> Session | None: ...
    def save(self, session: Session) -> None: ...
    def clear(self) -> None: ...
    def load_password(self, customer_id: str) -> str | None: ...
    def save_password(self, customer_id: str, password: str) -> None: ...
    def delete_password(self, customer_id: str) -> None: ...
    def load_relogin_block(self, customer_id: str) -> ReloginBlock | None: ...
    def save_relogin_block(self, customer_id: str, block: ReloginBlock) -> None: ...
    def clear_relogin_block(self, customer_id: str) -> None: ...
    def load_pending_login(self) -> PendingLogin | None: ...
    def save_pending_login(self, pending: PendingLogin) -> None: ...
    def clear_pending_login(self) -> None: ...


class KeyringSessionStore:
    _SESSION_KEY = "session"
    _PENDING_LOGIN_KEY = "pending-login"

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self._service = service

    def load(self) -> Session | None:
        raw = keyring.get_password(self._service, self._SESSION_KEY)
        if not raw:
            return None
        try:
            return Session.from_json(raw)
        except (ValueError, TypeError, AttributeError) as exc:
            raise CorruptKeyringError(self._service) from exc

    def save(self, session: Session) -> None:
        keyring.set_password(self._service, self._SESSION_KEY, session.to_json())

    def clear(self) -> None:
        if keyring.get_password(self._service, self._SESSION_KEY) is not None:
            keyring.delete_password(self._service, self._SESSION_KEY)

    def load_password(self, customer_id: str) -> str | None:
        return keyring.get_password(self._service, f"password:{customer_id}")

    def save_password(self, customer_id: str, password: str) -> None:
        keyring.set_password(self._service, f"password:{customer_id}", password)

    def delete_password(self, customer_id: str) -> None:
        if self.load_password(customer_id) is not None:
            keyring.delete_password(self._service, f"password:{customer_id}")

    def load_relogin_block(self, customer_id: str) -> ReloginBlock | None:
        raw = keyring.get_password(self._service, f"relogin-block:{customer_id}")
        if not raw:
            return None
        try:
            return ReloginBlock.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            raise CorruptKeyringError(self._service) from exc

    def save_relogin_block(self, customer_id: str, block: ReloginBlock) -> None:
        keyring.set_password(self._service, f"relogin-block:{customer_id}", json.dumps(block.to_dict()))

    def clear_relogin_block(self, customer_id: str) -> None:
        if keyring.get_password(self._service, f"relogin-block:{customer_id}") is not None:
            keyring.delete_password(self._service, f"relogin-block:{customer_id}")

    def load_pending_login(self) -> PendingLogin | None:
        raw = keyring.get_password(self._service, self._PENDING_LOGIN_KEY)
        if not raw:
            return None
        try:
            return PendingLogin.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            raise CorruptKeyringError(self._service) from exc

    def save_pending_login(self, pending: PendingLogin) -> None:
        keyring.set_password(self._service, self._PENDING_LOGIN_KEY, json.dumps(pending.to_dict()))

    def clear_pending_login(self) -> None:
        if keyring.get_password(self._service, self._PENDING_LOGIN_KEY) is not None:
            keyring.delete_password(self._service, self._PENDING_LOGIN_KEY)


class FileSessionStore:
    """JSON file readable only by the owner (dir 0700, file 0600), for headless Linux/CI.

    Plain JSON: a password saved with `--remember` is in clear text, protected only by the permissions.
    Writes go through a temporary file and `os.replace`, under `flock` on `<name>.lock` (0600).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize read-modify-write across processes (e.g. two cron jobs refreshing the token)."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise CorruptStoreError(self.path) from exc
        if not isinstance(data, dict):
            raise CorruptStoreError(self.path)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".session-", suffix=".tmp")  # unique, 0600
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def _update(self, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        with self._locked():
            self._write(change(self._read()))

    def load(self) -> Session | None:
        raw = self._read().get("session")
        if raw is None:
            return None
        try:
            return Session.from_dict(raw)
        except (TypeError, AttributeError) as exc:
            raise CorruptStoreError(self.path) from exc

    def save(self, session: Session) -> None:
        self._update(lambda data: {**data, "session": asdict(session)})

    def clear(self) -> None:
        self._update(lambda data: {k: v for k, v in data.items() if k != "session"})

    def load_password(self, customer_id: str) -> str | None:
        return self._read().get("passwords", {}).get(customer_id)

    def save_password(self, customer_id: str, password: str) -> None:
        self._update(lambda data: {**data, "passwords": {**data.get("passwords", {}), customer_id: password}})

    def delete_password(self, customer_id: str) -> None:
        self._update(
            lambda data: {**data, "passwords": {k: v for k, v in data.get("passwords", {}).items() if k != customer_id}}
        )

    def load_relogin_block(self, customer_id: str) -> ReloginBlock | None:
        raw = self._read().get("relogin_blocks", {}).get(customer_id)
        if raw is None:
            return None
        try:
            return ReloginBlock.from_dict(raw)
        except (ValueError, TypeError, KeyError) as exc:
            raise CorruptStoreError(self.path) from exc

    def save_relogin_block(self, customer_id: str, block: ReloginBlock) -> None:
        self._update(
            lambda data: {**data, "relogin_blocks": {**data.get("relogin_blocks", {}), customer_id: block.to_dict()}}
        )

    def clear_relogin_block(self, customer_id: str) -> None:
        if self.load_relogin_block(customer_id) is None:
            return  # don't rewrite the file on every successful login
        self._update(
            lambda data: {
                **data,
                "relogin_blocks": {k: v for k, v in data.get("relogin_blocks", {}).items() if k != customer_id},
            }
        )

    def load_pending_login(self) -> PendingLogin | None:
        raw = self._read().get("pending_login")
        if raw is None:
            return None
        try:
            return PendingLogin.from_dict(raw)
        except (ValueError, TypeError, KeyError) as exc:
            raise CorruptStoreError(self.path) from exc

    def save_pending_login(self, pending: PendingLogin) -> None:
        self._update(lambda data: {**data, "pending_login": pending.to_dict()})

    def clear_pending_login(self) -> None:
        if "pending_login" not in self._read():
            return
        self._update(lambda data: {k: v for k, v in data.items() if k != "pending_login"})


class MemorySessionStore:
    """In-memory store for tests and library users who manage persistence themselves."""

    def __init__(self, session: Session | None = None) -> None:
        self.session = session
        self.passwords: dict[str, str] = {}
        self.relogin_blocks: dict[str, ReloginBlock] = {}
        self.pending_login: PendingLogin | None = None

    def load(self) -> Session | None:
        return self.session

    def save(self, session: Session) -> None:
        self.session = session

    def clear(self) -> None:
        self.session = None

    def load_password(self, customer_id: str) -> str | None:
        return self.passwords.get(customer_id)

    def save_password(self, customer_id: str, password: str) -> None:
        self.passwords[customer_id] = password

    def delete_password(self, customer_id: str) -> None:
        self.passwords.pop(customer_id, None)

    def load_relogin_block(self, customer_id: str) -> ReloginBlock | None:
        return self.relogin_blocks.get(customer_id)

    def save_relogin_block(self, customer_id: str, block: ReloginBlock) -> None:
        self.relogin_blocks[customer_id] = block

    def clear_relogin_block(self, customer_id: str) -> None:
        self.relogin_blocks.pop(customer_id, None)

    def load_pending_login(self) -> PendingLogin | None:
        return self.pending_login

    def save_pending_login(self, pending: PendingLogin) -> None:
        self.pending_login = pending

    def clear_pending_login(self) -> None:
        self.pending_login = None


def default_store_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "myinvestor" / "session.json"


def keyring_available() -> bool:
    return type(keyring.get_keyring()).__module__ not in _NO_KEYRING_MODULES


def resolve_store(kind: StoreKind = StoreKind.auto, path: Path | None = None) -> SessionStore:
    if kind is StoreKind.auto:
        kind = StoreKind.keyring if keyring_available() else StoreKind.file
    if kind is StoreKind.keyring:
        return KeyringSessionStore()
    return FileSessionStore(path or default_store_path())
