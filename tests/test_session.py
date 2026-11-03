from __future__ import annotations

import json
import stat
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from myinvestor import envelope
from myinvestor.browser_login import DEVICE_KEY, FORM_KEY, REFRESH_KEY, TOKEN_KEY, session_from_storage
from myinvestor.errors import AuthError, CorruptStoreError
from myinvestor.output import format_cell, format_eur
from myinvestor.session import Session
from myinvestor.stores import FileSessionStore
from tests.conftest import DEVICE, make_jwt


def test_expiry_and_refresh_window() -> None:
    live = Session(device_id=DEVICE, access_token=make_jwt(), refresh_token=make_jwt(timedelta(minutes=30)))
    assert not live.is_expired() and live.can_refresh()

    stale = Session(device_id=DEVICE, access_token=make_jwt(timedelta(seconds=10)), refresh_token=make_jwt())
    assert stale.is_expired()  # inside the safety skew

    assert not Session(device_id=DEVICE).can_refresh()


def test_logout_keeps_device_and_old_sessions_load() -> None:
    session = Session(device_id=DEVICE, customer_id="X", access_token="a", refresh_token="r")
    assert session.without_tokens() == Session(device_id=DEVICE, customer_id="X")
    assert Session.from_dict({"device_id": DEVICE, "future_field": 1}).device_id == DEVICE


def test_tokens_hidden_from_repr() -> None:
    assert "secret-token" not in repr(
        Session(device_id=DEVICE, access_token="secret-token", refresh_token="secret-token")
    )


def test_file_store_is_private(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "cfg" / "session.json")
    session = Session(device_id=DEVICE, access_token="a", refresh_token="r")
    store.save(session)

    assert store.load() == session
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    store.clear()
    assert store.load() is None


def test_file_store_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{nope")
    with pytest.raises(CorruptStoreError):
        FileSessionStore(path).load()


def test_session_from_web_local_storage() -> None:
    access, refresh = make_jwt(), make_jwt(timedelta(minutes=30))
    storage = {
        TOKEN_KEY: json.dumps(access),  # the web stores the tokens JSON-quoted
        REFRESH_KEY: refresh,
        FORM_KEY: json.dumps({"customerId": "00000000T"}),
        DEVICE_KEY: DEVICE,
    }
    assert session_from_storage(storage) == Session(
        device_id=DEVICE, customer_id="00000000T", access_token=access, refresh_token=refresh
    )


def test_session_from_storage_waits_for_login_and_needs_device() -> None:
    assert session_from_storage({TOKEN_KEY: None, REFRESH_KEY: None}) is None
    with pytest.raises(AuthError, match="_capuid"):
        session_from_storage({TOKEN_KEY: "a", REFRESH_KEY: "r"})
    form_device = session_from_storage({TOKEN_KEY: "a", REFRESH_KEY: "r", FORM_KEY: json.dumps({"deviceId": "d1"})})
    assert form_device is not None and form_device.device_id == "d1"


def test_decode_keeps_decimals_exact() -> None:
    response = httpx.Response(200, json={"payload": {"data": {"amount": 0.1}}})
    assert envelope.data(envelope.decode(response)) == {"amount": Decimal("0.1")}


def test_spanish_formatting() -> None:
    assert format_eur(Decimal("-1128.04")) == "-1.128,04 €"
    assert format_eur(Decimal("5"), "USD") == "5,00 USD"
    assert format_cell("rentab", Decimal("10.061")) == "10,06 %"
    assert format_cell("participaciones", Decimal("12.3456780")) == "12,345678"
    assert format_cell("participaciones", Decimal("3")) == "3"
    assert format_cell("nombre", "x") == "x"
