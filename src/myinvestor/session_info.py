"""What `status` and `doctor` say about the stored session: expiry, renewal options, never a secret."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from myinvestor.session import Session
from myinvestor.stores import FileSessionStore, KeyringSessionStore, ReloginBlock, SessionStore

VISIBLE_ID_CHARS = 4


def store_kind(store: SessionStore) -> str:
    if isinstance(store, KeyringSessionStore):
        return "keyring"
    if isinstance(store, FileSessionStore):
        return "file"
    return "memory"


def store_path(store: SessionStore) -> Path | None:
    return store.path if isinstance(store, FileSessionStore) else None


def mask_customer_id(customer_id: str | None) -> str | None:
    """`12345678Z` → `***678Z`: enough to tell two accounts apart, not to identify anyone."""
    if not customer_id:
        return None
    return "***" + customer_id[-VISIBLE_ID_CHARS:] if len(customer_id) > VISIBLE_ID_CHARS else "***"


def iso_local(moment: datetime | None) -> str | None:
    return moment.astimezone().isoformat() if moment else None


@dataclass(frozen=True)
class SessionInfo:
    """Snapshot of the store: the session, whether a password is saved and any active re-login pause."""

    store_kind: str
    store_path: Path | None
    session: Session | None
    password_remembered: bool
    relogin_block: ReloginBlock | None

    @classmethod
    def read(cls, store: SessionStore, now: datetime | None = None) -> SessionInfo:
        """Raises CorruptStoreError/CorruptKeyringError if the store cannot be parsed."""
        session = store.load()
        customer_id = session.customer_id if session else None
        remembered = bool(customer_id and store.load_password(customer_id))
        block = store.load_relogin_block(customer_id) if customer_id else None
        return cls(
            store_kind=store_kind(store),
            store_path=store_path(store),
            session=session,
            password_remembered=remembered,
            relogin_block=block if block is not None and block.is_active(now) else None,
        )

    def with_session(self, session: Session) -> SessionInfo:
        return replace(self, session=session)

    @property
    def has_tokens(self) -> bool:
        return bool(self.session and self.session.refresh_token)

    @property
    def can_relogin(self) -> bool:
        """An expired session can be renewed by logging in again with the saved password."""
        return self.password_remembered and self.relogin_block is None

    def can_refresh(self, now: datetime | None = None) -> bool:
        return bool(self.session and self.has_tokens and self.session.can_refresh(now))

    def is_usable(self, now: datetime | None = None) -> bool:
        """Active, or renewable without a manual `login`."""
        if self.session is None or not self.has_tokens:
            return False
        return not self.session.is_expired(now) or self.can_refresh(now) or self.can_relogin

    def to_dict(self, *, name: str | None = None, verified_online: bool = False) -> dict[str, Any]:
        """The `status -f json` object: no token nor password, the customer id masked."""
        session = self.session
        return {
            "logged_in": self.is_usable(),
            "name": name or (session.name if session else None),
            "customer_id_masked": mask_customer_id(session.customer_id if session else None),
            "device_id": session.device_id if session else None,
            "store": self.store_kind,
            "store_path": str(self.store_path) if self.store_path else None,
            "access_expires_at": iso_local(session.expires_at if session else None),
            "refresh_expires_at": iso_local(session.refresh_expires_at if session else None),
            "password_remembered": self.password_remembered,
            "can_refresh": self.can_refresh(),
            "can_relogin": self.can_relogin,
            "relogin_blocked_until": iso_local(self.relogin_block.until if self.relogin_block else None),
            "last_relogin_error": self.relogin_block.error if self.relogin_block else None,
            "verified_online": verified_online,
        }
