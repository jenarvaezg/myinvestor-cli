"""Owner-only files, replaced atomically and serialized across processes (session store, state, export).

A reader never sees a half-written file: the new content goes to a temporary file in the same
directory (created 0600 by `mkstemp`), is fsynced and then renamed over the old one. `locked` takes
an exclusive `flock` on a sibling lock file so two processes (cron jobs) don't interleave a
read-modify-write.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


def ensure_private_dir(path: Path) -> None:
    """Create `path` (and its parents) if missing; the new directory is 0700. Existing ones are left alone."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


@contextlib.contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `lock_path` (created 0600) for the duration of the block."""
    ensure_private_dir(lock_path.parent)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_atomic(path: Path, text: str, *, prefix: str = ".tmp-") -> None:
    """Replace `path` with `text` (UTF-8) in one rename; the file ends up 0600."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")  # unique, 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
