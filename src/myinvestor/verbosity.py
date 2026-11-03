"""How chatty the CLI is on stderr: `--quiet` keeps only errors, `--verbose` adds the request log.

stdout only ever carries the command's result. Rich already drops colours and markup when stderr
is not a terminal or `NO_COLOR` is set, so nothing here deals with that.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

from myinvestor.output import notices

LOGGER = logging.getLogger("myinvestor")


class StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to the current `sys.stderr`, not the one at creation (CliRunner and pytest swap it)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(message)s"))

    @property  # type: ignore[override]
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        pass


def configure(*, quiet: bool, verbose: bool) -> None:
    """Library logs (warnings; with `verbose`, one line per request) to stderr; `quiet` keeps only errors."""
    for handler in [h for h in LOGGER.handlers if isinstance(h, StderrHandler)]:
        LOGGER.removeHandler(handler)
    handler = StderrHandler()
    handler.setLevel(logging.ERROR if quiet else logging.DEBUG if verbose else logging.WARNING)
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.DEBUG if verbose else logging.NOTSET)
    notices.quiet = quiet
