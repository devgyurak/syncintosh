# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `ProfileIndex` that reads an app's own profile list from disk, in memory, and nothing else.

VS Code's `storage.json` lists the profiles, but it also holds window state and a telemetry machine id.
So the file is **read-only here**: its bytes are parsed and dropped, only the profile list leaves this
module (as a `ProfileMap`), and it is never copied, sealed, logged or written (issue #22, AGENTS.md §3.3).
An error message never echoes the file's content.

Every directory the index names is checked before it can resolve: a profile directory that is a symlink,
a denied path, or one that leaves `$HOME` once resolved is dropped and reported (decision D7). The index
file is the app's own, but its `location` values are still text from disk, and a planted one must not
steer a later write.

The format-specific reading is injected (`read_profiles` for VS Code) so this module names no app.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from syncintosh.core.ports import DenyList
from syncintosh.core.profiles import ProfileMap, ProfileSpec

# The real file is a few KiB. A bound keeps a planted or runaway file from being read whole into memory.
MAX_INDEX_BYTES = 4 * 1024 * 1024


class SystemProfileIndex:
    """Reads the profile list under `home` through `parse`; constructing it does no I/O."""

    def __init__(self, home: str, deny: DenyList, parse: Callable[[Any], ProfileMap]) -> None:
        self._home = home
        self._deny = deny
        self._parse = parse

    def read(self, spec: ProfileSpec) -> ProfileMap:
        """The profiles this machine has under `spec`, or an unreadable map saying why."""
        path = Path(self._home) / spec.index
        if self._deny.is_denied(self._home, spec.index) or self._deny.escapes(self._home, spec.index):
            # A symlinked parent (`Code/`, `globalStorage/`) would otherwise steer the read elsewhere.
            return ProfileMap.unreadable("the profile index is in a protected location")
        try:
            if path.is_symlink():
                # A link could point the read at a file that is not the app's index.
                return ProfileMap.unreadable("the profile index is a symbolic link")
            with path.open("rb") as handle:
                raw = handle.read(MAX_INDEX_BYTES + 1)
            if len(raw) > MAX_INDEX_BYTES:
                return ProfileMap.unreadable("the profile index is too large")
        except FileNotFoundError:
            return ProfileMap.unreadable("the profile index does not exist")
        except OSError:
            return ProfileMap.unreadable("the profile index could not be read")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return ProfileMap.unreadable("the profile index is not valid JSON")
        parsed = self._parse(document)
        if not parsed.readable:
            return parsed
        kept, problems = [], list(parsed.problems)
        for entry in parsed.entries:
            reason = self._unsafe(spec, entry.directory)
            if reason is None:
                kept.append(entry)
            else:
                problems.append(f"skipped profile {entry.name!r}: {reason}")
        return ProfileMap.create(tuple(kept), tuple(problems))

    def _unsafe(self, spec: ProfileSpec, directory: str) -> str | None:
        """Why the profile's directory may not resolve, or `None` when it is safe."""
        relative = f"{spec.home}/{directory}"
        if (Path(self._home) / relative).is_symlink():
            return "its directory is a symbolic link"
        if self._deny.is_denied(self._home, relative):
            return "its directory is a protected path"
        # Resolved with a child so the directory itself, not only its parent, is what is checked.
        if self._deny.escapes(self._home, f"{relative}/x"):
            return "its directory leaves the home directory"
        return None


__all__ = ["MAX_INDEX_BYTES", "SystemProfileIndex"]
