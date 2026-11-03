"""Where the session lives: OS keyring, a 0600 file, or memory.

The password is stored only after `login --direct --remember`, keyed by customer id, so an expired
session can log in again unattended (cron). It is deleted on logout and on the first rejection.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import keyring

from myinvestor.config import KEYRING_SERVICE
from myinvestor.errors import CorruptKeyringError, CorruptStoreError
from myinvestor.session import Session

_NO_KEYRING_MODULES = {"keyring.backends.fail", "keyring.backends.null"}


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


class KeyringSessionStore:
    _SESSION_KEY = "session"

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


class FileSessionStore:
    """JSON file readable only by the owner (dir 0700, file 0600), for headless Linux/CI."""

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


class MemorySessionStore:
    """In-memory store for tests and library users who manage persistence themselves."""

    def __init__(self, session: Session | None = None) -> None:
        self.session = session
        self.passwords: dict[str, str] = {}

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
