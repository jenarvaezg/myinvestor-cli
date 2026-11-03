"""The suite's own isolation: no real session store, no dependency on the terminal running pytest."""

from __future__ import annotations

import importlib
import os
import pkgutil
from types import ModuleType

import pytest
from rich.console import Console

import myinvestor
from myinvestor import cli_common
from myinvestor.stores import StoreKind, resolve_store
from tests.conftest import MODULE_CONSOLES, TERMINAL_ENV, RealStoreAccessError


def _package_modules() -> list[ModuleType]:
    return [importlib.import_module(m.name) for m in pkgutil.walk_packages(myinvestor.__path__, "myinvestor.")]


def test_commands_get_the_store_through_cli_common() -> None:
    """A `from cli_common import get_store` copy escapes the single patch the CLI fixtures apply."""
    copies = [m.__name__ for m in _package_modules() if m is not cli_common and "get_store" in vars(m)]
    assert copies == []


def test_the_real_keyring_is_refused(real_store_guard: list[str]) -> None:
    with pytest.raises(RealStoreAccessError, match="real keyring"):
        resolve_store(StoreKind.auto).load()
    assert real_store_guard == ["keyring.get_password('myinvestor-cli', 'session')"]
    real_store_guard.clear()


def test_the_default_session_file_is_refused(real_store_guard: list[str]) -> None:
    with pytest.raises(RealStoreAccessError, match="default session file"):
        resolve_store(StoreKind.file)
    assert real_store_guard == ["stores.default_store_path()"]
    real_store_guard.clear()


def test_terminal_environment_is_cleared() -> None:
    assert not [name for name in TERMINAL_ENV if name in os.environ]


def test_every_import_time_console_is_reset() -> None:
    """A module-level Console the fixture does not know would keep FORCE_COLOR/COLUMNS from pytest's start."""
    consoles = [v for m in _package_modules() for v in vars(m).values() if isinstance(v, Console)]
    assert consoles and all(any(c is known for known in MODULE_CONSOLES) for c in consoles)
    # without FORCE_COLOR, colour only when actually writing to a terminal (never under pytest's capture)
    assert all((c.color_system is None or c.file.isatty()) and not c.no_color for c in MODULE_CONSOLES)
