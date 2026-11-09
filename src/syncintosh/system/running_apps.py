# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `RunningApps` over `lsappinfo`, the LaunchServices query tool macOS ships.

`lsappinfo find bundleid=<id>` prints the running application's ASN line (`ASN:0x0-0x19019-"Finder":`) and exits 0, and
prints just a newline, also with exit 0, when no such application is running; both were checked on the build this was
written against. Because "not running" and "cannot ask" look alike to a careless reader, only an output that is
**either** an ASN line **or** blank is an answer — a nonzero exit, a timeout, a missing tool or anything else is `None`,
and a caller that must not write under an open app treats `None` as running.

The bundle ID comes from the local built-in catalog, never from a record, and is checked for shape before it is passed
as one argument (no shell is involved). Building this does no I/O (``AGENTS.md`` §2.6).
"""

from __future__ import annotations

import re
import subprocess

# A sync must not hang on LaunchServices; a timeout is "cannot tell", which defers the write.
LSAPPINFO_TIMEOUT_SECONDS = 3.0

_BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")
_ASN_LINE = re.compile(r"ASN:\S+")


class LsappinfoRunningApps:
    """Whether an app with a given bundle ID is running, by asking LaunchServices."""

    def __init__(self, *, timeout_seconds: float = LSAPPINFO_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds

    def running(self, bundle_id: str) -> bool | None:
        """True when an ASN line comes back, False on a blank answer, `None` for anything else."""
        if _BUNDLE_ID.fullmatch(bundle_id) is None:
            return None
        try:
            result = subprocess.run(
                ["lsappinfo", "find", f"bundleid={bundle_id}"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        if output == "":
            return False
        return True if _ASN_LINE.match(output) else None


__all__ = ["LSAPPINFO_TIMEOUT_SECONDS", "LsappinfoRunningApps"]
