"""Second-factor codes when nobody can type them at the CLI's stdin (#11).

- `MYINVESTOR_OTP`: answers the first code asked, once; a code already sent is never sent again.
- `--code-stdin` / `--code-fd N`: one line per code, read only when MyInvestor asks for it (after the
  password of `--password-stdin`), so whoever drives the CLI can wait for the SMS before writing it.
- `/dev/tty`: when stdin is not a terminal but the process has one, ask codes there (like ssh or gpg);
  never app approvals, which still need stdin to be a terminal or `--approve`.

Sources answer codes in order; app approvals, and codes once the sources run out, go to the fallback
UI (terminal, `--approve` or nothing). Without a fallback, an approval in the app is never started.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TextIO

from myinvestor.errors import InputError, SignatureError, SignatureRequired
from myinvestor.signature import OTP_METHODS, Challenge, SignatureRequest, SignatureUI, can_start

TTY_PATH = "/dev/tty"


@dataclass(frozen=True)
class CodeSource:
    """Where codes come from: `read` returns the next one ('' when there is none); `once` answers a single time."""

    label: str
    read: Callable[[], str]
    once: bool = False


def preset_code(code: str, label: str) -> CodeSource:
    return CodeSource(label, lambda: code.strip(), once=True)


def line_codes(stream: TextIO, label: str) -> CodeSource:
    return CodeSource(label, lambda: stream.readline().strip())


def fd_codes(fd: int) -> CodeSource:
    """Lines of an inherited file descriptor (`--code-fd 3`), opened the first time a code is asked."""
    opened: list[TextIO] = []

    def read() -> str:
        if not opened:
            try:
                opened.append(os.fdopen(fd, "r", encoding="utf-8", closefd=False))
            except OSError as exc:
                raise InputError(f"--code-fd {fd}: no se puede leer ese descriptor ({exc.strerror}).") from exc
        return opened[0].readline().strip()

    return CodeSource(f"el descriptor {fd}", read)


def open_tty() -> TextIO | None:
    """The controlling terminal, or None (cron, systemd, CI and detached agents have none)."""
    try:
        return open(TTY_PATH, "r+", encoding="utf-8")
    except OSError:
        return None


def code_prompt(challenge: Challenge, attempt: int) -> str:
    where = f" enviado a {challenge.destination}" if challenge.destination else ""
    retry = " (incorrecto, prueba otra vez)" if attempt > 1 else ""
    return f"Código {challenge.method}{where}{retry}"


def sent_to(challenge: Challenge) -> str:
    where = f" a {challenge.destination}" if challenge.destination else ""
    return f"MyInvestor ha enviado un código {challenge.method}{where}"


class CodeSourcesUI:
    """A `SignatureUI` that takes codes from `sources` before asking `fallback`."""

    def __init__(
        self, sources: Iterable[CodeSource], fallback: SignatureUI | None, announce: Callable[[str], None]
    ) -> None:
        self._sources = list(sources)
        self._fallback = fallback
        self._announce = announce

    def supports(self, method: str) -> bool:
        return method in OTP_METHODS or can_start(self._fallback, method)

    def ask_code(self, challenge: Challenge, attempt: int) -> str | None:
        while self._sources:
            source = self._sources[0]
            if source.once:
                self._sources.pop(0)  # used once, whatever MyInvestor says about it
            self._announce(f"{sent_to(challenge)}; leo el código de {source.label}.")
            code = source.read()
            if code:
                return code
            if not source.once:
                self._sources.pop(0)  # end of the stream
        if self._fallback is None or not can_start(self._fallback, challenge.method):
            raise SignatureError(
                f"{sent_to(challenge)}, pero no tengo {'otro ' if attempt > 1 else ''}código que darle. "
                "Pásalo con MYINVESTOR_OTP, --code-stdin o --code-fd, o usa `myinvestor login --direct --start`."
            )
        return self._fallback.ask_code(challenge, attempt)

    def approve_in_app(self, request: SignatureRequest) -> None:
        if self._fallback is None:  # `supports` keeps the Signer from getting here
            raise SignatureRequired(sorted({m for group in request.pending_methods for m in group}))
        self._fallback.approve_in_app(request)
