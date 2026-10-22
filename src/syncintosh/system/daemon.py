# SPDX-License-Identifier: AGPL-3.0-or-later
"""The launchd job that runs `syncintosh watch` in the background.

The plist embeds the **absolute path** of the installed entry point, so
`syncintosh daemon install` rewrites it with the current path every time: if Python or the install
method changes, a stale path breaks the daemon silently (``docs/design/README.md``, launchd). The
path is taken as `shutil.which` returns it and never resolved — a Homebrew shim is a legitimate
entry point, and following it would pin the daemon to the versioned Cellar path that the next
upgrade deletes.

`KeepAlive` restarts the job on a **failed** exit, which is why `watch` reports a failed start as a
non-zero exit and recovers every cycle failure itself (``services/watch.py``). `plistlib` writes
the file, so no XML is hand-built. No I/O happens in the constructor (``AGENTS.md`` §2.6): the
plist path is derived from `home`, and `launchctl` runs only when a method is called.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from syncintosh.core.errors import DaemonError

# The label is the job's identity to launchd and the plist's filename stem.
PLIST_LABEL = "io.github.devgyurak.syncintosh"

# How often the daemon also runs a remote-polling sync even with no local change: a change on the
# other Mac must arrive without a local edit to trigger it. Five minutes matches a background job.
DEFAULT_POLL_SECONDS = 300

# launchd starts a job with a bare environment, so `git` must be found by an explicit PATH rather
# than the user's shell profile. Homebrew's two prefixes plus the system directories.
_DAEMON_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# `subprocess.run`'s shape, injected so a test never calls launchctl.
Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]

# `launchctl bootstrap` can fail with a transient "5: Input/output error" right after a `bootout`,
# because the previous job is still tearing down. A few retries settle it.
_BOOTSTRAP_ATTEMPTS = 3
_BOOTSTRAP_RETRY_SECONDS = 0.2


def entry_point() -> str | None:
    """The installed `syncintosh` entry point on `PATH`, or `None` when it is not found.

    The **one** place `install` and `doctor` decide what "this build's entry point" is, so the two
    cannot disagree and report a healthy install that is not the one running. The path is returned
    as found and never resolved — a Homebrew shim is a legitimate entry point.
    """
    return shutil.which("syncintosh") or shutil.which("snt")


def build_plist(*, entry_point: str, poll_seconds: int) -> bytes:
    """The launchd plist bytes for running `entry_point watch --poll <n>` in the background."""
    payload = {
        "Label": PLIST_LABEL,
        "ProgramArguments": [entry_point, "watch", "--poll", str(poll_seconds)],
        "RunAtLoad": True,
        # Restart on a failed exit, never on a clean one: `watch` exits 0 only on a stop request.
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"PATH": _DAEMON_PATH},
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)


class LaunchdDaemon:
    """Installs, inspects, and removes the per-user launchd job."""

    def __init__(self, home: Path, *, run: Runner = subprocess.run) -> None:
        self._home = home
        self._run = run

    @property
    def plist_path(self) -> Path:
        """`~/Library/LaunchAgents/<label>.plist` — the user agent launchd reads."""
        return self._home / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

    def install(self, *, entry_point: str, poll_seconds: int) -> None:
        """Write the plist with the **current** entry point and (re)load the job.

        The plist is rewritten unconditionally, so a stale path from an earlier install (or a
        different Python) can never survive. `bootout` is tried first and its failure ignored: a
        job that is not loaded fails it, and that is the common case on a first install.
        """
        try:
            self.plist_path.parent.mkdir(parents=True, exist_ok=True)
            self.plist_path.write_bytes(build_plist(entry_point=entry_point, poll_seconds=poll_seconds))
        except OSError as exc:
            raise DaemonError(f"could not write the plist {self.plist_path}: {exc}") from exc
        self._launchctl("bootout", f"gui/{os.getuid()}/{PLIST_LABEL}")
        result = self._bootstrap()
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[:400]
            raise DaemonError(f"`launchctl bootstrap` failed: {detail or 'no detail'}")

    def _bootstrap(self) -> subprocess.CompletedProcess[bytes]:
        """Bootstrap the plist, retrying a transient failure after the `bootout` teardown."""
        result = self._launchctl("bootstrap", f"gui/{os.getuid()}", str(self.plist_path))
        for _ in range(_BOOTSTRAP_ATTEMPTS - 1):
            if result.returncode == 0:
                return result
            time.sleep(_BOOTSTRAP_RETRY_SECONDS)
            result = self._launchctl("bootstrap", f"gui/{os.getuid()}", str(self.plist_path))
        return result

    def is_loaded(self) -> bool | None:
        """Whether launchd has the job loaded: `True`/`False`, or `None` when it cannot be told.

        `doctor` uses this: a plist on disk that launchd did not accept leaves the daemon off while
        every other check passes.
        """
        try:
            result = self._launchctl("print", f"gui/{os.getuid()}/{PLIST_LABEL}")
        except DaemonError:
            return None
        return result.returncode == 0

    def uninstall(self) -> None:
        """Stop the job and remove its plist. Removing a job that is not there is not an error."""
        self._launchctl("bootout", f"gui/{os.getuid()}/{PLIST_LABEL}")
        try:
            self.plist_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DaemonError(f"could not remove the plist {self.plist_path}: {exc}") from exc

    def recorded_entry_point(self) -> str | None:
        """The absolute entry point the installed plist runs, or `None` when there is no plist.

        `doctor` compares this with the current entry point to catch a path that broke silently.
        """
        try:
            payload = plistlib.loads(self.plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            return None
        arguments = payload.get("ProgramArguments")
        if isinstance(arguments, list) and arguments and isinstance(arguments[0], str):
            return arguments[0]
        return None

    def _launchctl(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        argv: Sequence[str] = ["launchctl", *arguments]
        try:
            return self._run(argv, capture_output=True, check=False)
        except OSError as exc:
            # `launchctl` is missing or not executable; a raw `FileNotFoundError` would surface as a
            # traceback, so wrap it in the domain error the CLI renders.
            raise DaemonError(f"could not run launchctl: {exc}") from exc


__all__ = ["DEFAULT_POLL_SECONDS", "PLIST_LABEL", "LaunchdDaemon", "build_plist", "entry_point"]
