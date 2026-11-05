# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pending-push marker on disk: `pending-push.json`, a generation id and a commit id.

It lives in the state directory, outside the repo (``AGENTS.md`` §3.3), and holds no key. A write is
atomic, because a truncated marker would read as "no rotation to recover" and leave sync stopped on
a generation it could have settled; the durable write is shared with `SystemLiveFiles`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from syncintosh.core.errors import RotationMarkerCorrupt
from syncintosh.core.models import PendingPush
from syncintosh.system.live_files import SystemLiveFiles

MARKER_VERSION = 1


class FileRotationMarker:
    """A `RotationMarkerStore` over one JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> PendingPush | None:
        """Return the marker, or `None` when none was written."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RotationMarkerCorrupt(f"cannot read the pending-push marker {self._path}: {exc}") from exc
        try:
            parsed: Any = json.loads(text)
            generation = parsed["generation"]
            commit = parsed["commit"]
            if not isinstance(generation, int) or isinstance(generation, bool) or not isinstance(commit, str):
                raise TypeError("generation must be an integer and commit a string")
            return PendingPush.create(generation=generation, commit=commit)
        except (ValueError, KeyError, TypeError) as exc:
            raise RotationMarkerCorrupt(f"the pending-push marker {self._path} is not valid: {exc}") from exc

    def record(self, marker: PendingPush) -> None:
        """Replace the marker atomically."""
        payload = json.dumps(
            {"version": MARKER_VERSION, "generation": marker.generation, "commit": marker.commit},
            indent=2,
            sort_keys=True,
        )
        SystemLiveFiles().write(str(self._path), (payload + "\n").encode("utf-8"))

    def clear(self) -> None:
        """Remove the marker; absent is already cleared."""
        self._path.unlink(missing_ok=True)


__all__ = ["MARKER_VERSION", "FileRotationMarker"]
