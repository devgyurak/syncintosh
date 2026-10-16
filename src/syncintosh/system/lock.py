# SPDX-License-Identifier: AGPL-3.0-or-later
"""The advisory sync lock, shared by the CLI and the daemon.

Every mutating operation takes it, so a rotation cannot race a sync and two callers cannot
interleave (``docs/design/01-sync-model.md`` step 1). It is advisory `flock`: a second caller
waits a bounded time, then reports "another sync is running" instead of racing. The lock file is
never unlinked — unlinking a lock is how two processes end up holding different files with the
same name.

No I/O in the constructor: the file is opened on the first `acquire` (``AGENTS.md`` §2.6).
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

from syncintosh.core.errors import SyncLockError

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 0.05


class FileLock:
    """A `SyncLock` over `fcntl.flock` on one file."""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._fd: int | None = None

    @staticmethod
    def _try_lock(descriptor: int) -> bool:
        """Attempt a non-blocking exclusive lock; False when another holder has it."""
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        """Take the lock, or raise `SyncLockError` after the bounded wait."""
        if self._fd is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self._timeout_seconds
        while not self._try_lock(descriptor):
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise SyncLockError(f"another sync is running (lock {self._path.name} is held)")
            time.sleep(self._poll_seconds)
        self._fd = descriptor

    def release(self) -> None:
        """Release the lock. Releasing one this process does not hold is not an error."""
        descriptor = self._fd
        self._fd = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


__all__ = ["DEFAULT_POLL_SECONDS", "DEFAULT_TIMEOUT_SECONDS", "FileLock"]
