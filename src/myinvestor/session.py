"""Session model. Storage backends live in `myinvestor.stores`."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from myinvestor.config import TOKEN_EXPIRY_SKEW


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode the JWT payload without verifying it (only used for expiry scheduling)."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def jwt_expiry(token: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(jwt_claims(token)["exp"]), tz=UTC)
    except (KeyError, ValueError, TypeError):
        return None


def _expired(token: str | None, now: datetime | None) -> bool:
    if not token:
        return True
    expires_at = jwt_expiry(token)
    if expires_at is None:
        return False
    now = now or datetime.now(tz=UTC)
    return now >= expires_at - timedelta(seconds=TOKEN_EXPIRY_SKEW)


def new_device_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Session:
    """Keycloak tokens plus the device id they were issued to.

    The access token lives 15 min and the refresh token 30 min; every refresh returns a new pair,
    so the session survives as long as it is used at least every half hour. The device id goes in
    `x-device-id` on every request: MyInvestor asks for a second factor on unknown devices, so it is
    kept across logins (also after `logout`).
    """

    device_id: str = field(default_factory=new_device_id)
    customer_id: str | None = None
    access_token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Session:
        """Tolerate sessions from other versions: unknown fields are dropped."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def expires_at(self) -> datetime | None:
        return jwt_expiry(self.access_token) if self.access_token else None

    @property
    def refresh_expires_at(self) -> datetime | None:
        return jwt_expiry(self.refresh_token) if self.refresh_token else None

    @property
    def name(self) -> str | None:
        """Display name from the access token (`name` claim), for `status`."""
        if not self.access_token:
            return None
        claims = jwt_claims(self.access_token)
        value = claims.get("given_name") or claims.get("name")
        return str(value) if value else None

    def is_expired(self, now: datetime | None = None) -> bool:
        return _expired(self.access_token, now)

    def can_refresh(self, now: datetime | None = None) -> bool:
        return not _expired(self.refresh_token, now)

    def with_tokens(self, access_token: str, refresh_token: str | None) -> Session:
        return replace(self, access_token=access_token, refresh_token=refresh_token or self.refresh_token)

    def without_tokens(self) -> Session:
        """Logout keeps the device so the next login can skip the second factor."""
        return replace(self, access_token=None, refresh_token=None)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Session:
        return cls.from_dict(json.loads(raw))
