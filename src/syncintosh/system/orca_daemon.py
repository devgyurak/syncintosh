# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `RunningApps` that also counts Orca's terminal host, `orcad`, as Orca running.

Orca keeps its terminals in a separate process that outlives the window: `Orca Helper … daemon-entry.js`, started from
inside `Orca.app`, which records itself in `~/Library/Application Support/orca/daemon/daemon-v<N>.pid` (a JSON object
with a `pid`; Orca itself asks the user to close Orca **and** `orcad` before it recovers the database). Quitting the
app therefore does not prove the process that may hold `profile-state.db` is gone, so a write is held back until both
are.

`running` answers for the bundle ID this was built with and defers every other to `inner` unchanged. The answer is
three-valued like `RunningApps`:

* `True` when `inner` says so, or a pid file names a live process whose executable is inside `Orca.app`;
* `None` when it cannot be told (an unreadable pid file or directory, a pid that is not a number, a process whose
  path cannot be read): a caller that must not write under an open app treats that as running;
* `False` only when `inner` says no and every pid file is stale: no such process, or one whose executable is
  somewhere else (the pid was reused).

The pid file is read for its `pid` only; the sibling `.token` and `.sock` are never opened. Building this does no I/O
(``AGENTS.md`` §2.6).
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

from syncintosh.core.ports import RunningApps

DAEMON_DIRECTORY: Final = "Library/Application Support/orca/daemon"
_PID_FILES: Final = "daemon-v*.pid"
_APP_BUNDLE: Final = "Orca.app"
# `PROC_PIDPATHINFO_MAXSIZE` in <libproc.h> is `4 * MAXPATHLEN`.
_PROC_PIDPATHINFO_MAXSIZE: Final = 4096


def executable_path(pid: int) -> str:
    """The path of the executable `pid` is running.

    Raises `ProcessLookupError` when there is no such process and `OSError` when there is one whose path cannot be
    read, so a caller can tell "gone" from "cannot tell". `kill(pid, 0)` sends no signal; a `PermissionError` there
    means the process exists and belongs to someone else.
    """
    import ctypes

    with contextlib.suppress(PermissionError):
        os.kill(pid, 0)
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    buffer = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    length = libproc.proc_pidpath(pid, buffer, ctypes.sizeof(buffer))
    if length <= 0:
        raise OSError(ctypes.get_errno(), "the process's path could not be read")
    return buffer.raw[:length].decode("utf-8", errors="replace")


class OrcaDaemonRunningApps:
    """`inner`'s answer for an app, widened for one bundle ID to include that app's background daemon."""

    def __init__(
        self,
        inner: RunningApps,
        *,
        home: str,
        bundle_id: str,
        process_path: Callable[[int], str] = executable_path,
    ) -> None:
        self._inner = inner
        self._directory = Path(home) / DAEMON_DIRECTORY
        self._bundle_id = bundle_id
        self._process_path = process_path

    def running(self, bundle_id: str) -> bool | None:
        """True when the app or its daemon runs, False when neither does, `None` when that cannot be told."""
        app = self._inner.running(bundle_id)
        if bundle_id != self._bundle_id or app is True:
            return app
        daemon = self._daemon_running()
        if daemon is True:
            return True
        return None if app is None or daemon is None else False

    def _daemon_running(self) -> bool | None:
        """Whether any pid file names a live process that runs from inside the app bundle."""
        try:
            files = sorted(self._directory.glob(_PID_FILES))
        except OSError:
            return None
        verdicts = [self._alive(file) for file in files]
        if True in verdicts:
            return True
        return None if None in verdicts else False

    def _alive(self, pid_file: Path) -> bool | None:
        try:
            pid = json.loads(pid_file.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        try:
            path = self._process_path(pid)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return _APP_BUNDLE in Path(path).parts


__all__ = ["DAEMON_DIRECTORY", "OrcaDaemonRunningApps", "executable_path"]
