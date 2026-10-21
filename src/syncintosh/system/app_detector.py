# SPDX-License-Identifier: AGPL-3.0-or-later
"""An `AppDetector` over `mdfind`, the Spotlight query tool macOS ships.

`apply` asks whether the app that owns a missing file is installed, so it can create the file when
it is and **defer** with the download page when it is not, instead of writing into a directory that
does not exist (``docs/design/01-sync-model.md`` step 14). The verdict is three-valued on purpose:
a query that cannot run answers `None` (undeterminable) rather than `False`, so the caller keeps its
directory heuristic instead of deferring a file forever on a machine that actually has the app.

The bundle ID comes from the **local built-in catalog** (`PresetCatalog.preset_for`), never from a
record, so the query text is this project's own data and not untrusted input
(``AGENTS.md`` §3.6). One lookup per bundle ID per cycle is the caller's job — this class answers one
question and does no caching, and building it touches nothing (``AGENTS.md`` §2.6).
"""

from __future__ import annotations

import subprocess

# `mdfind` can be slow when Spotlight is rebuilding its index; a bounded wait keeps a sync from
# hanging, and a timeout is reported as "undeterminable", which falls back to the directory check.
MDFIND_TIMEOUT_SECONDS = 5.0


class MdfindAppDetector:
    """Whether an app bundle with a given ID is installed, by asking Spotlight."""

    def __init__(self, *, timeout_seconds: float = MDFIND_TIMEOUT_SECONDS) -> None:
        # Storing a number is not I/O, so this constructor is safe (``AGENTS.md`` §2.6).
        self._timeout = timeout_seconds

    def installed(self, bundle_id: str) -> bool | None:
        """True/False when Spotlight answers, `None` when the query cannot be made to answer.

        `mdfind` prints one path per matching bundle, so a non-empty stdout means installed. Every
        failure — no `mdfind` (a non-Mac, a stripped system), a nonzero exit, or a timeout — is
        `None`, never `False`: the caller then falls back to the directory heuristic rather than
        deferring the record on a guess.
        """
        query = f"kMDItemCFBundleIdentifier == '{bundle_id}'"
        try:
            result = subprocess.run(
                ["mdfind", query],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return bool(result.stdout.strip())


__all__ = ["MDFIND_TIMEOUT_SECONDS", "MdfindAppDetector"]
