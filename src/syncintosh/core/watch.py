# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure half of file watching: which events matter, and when a burst has settled.

`watchdog` supplies the raw events, but the two decisions the watcher makes are pure and testable
on their own (``AGENTS.md`` §2.1):

* **which paths matter** — the exact paths this machine tracks and the directory globs its config
  names (`PresetGlob`). A rename's **destination** counts as a target in its own right, because an
  atomic save writes a temporary file and renames it onto the real path: the create lands on the temp
  name, which is not a tracked path, and only the destination is.
* **when a burst has settled** — a debounce state machine, with **time passed in as an argument** and
  never read from a clock, so its property tests are plain arithmetic instead of sleeps.

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from syncintosh.core.config import SyncConfig
from syncintosh.core.paths import collision_key
from syncintosh.core.presets import PresetGlob

# How long the watcher waits for the filesystem to go quiet before it syncs. An editor save is not
# one write — it is a temp file, a rename, and often a follow-up metadata touch — and a sync per
# event would race the application for the file it is still writing.
DEFAULT_QUIET_SECONDS = 1.0

# The longest a burst may keep the watcher waiting. A directory under continuous churn (a build
# output, a log) would otherwise reset the quiet window forever and no sync would ever run.
DEFAULT_MAX_SECONDS = 30.0


@dataclass(frozen=True)
class FileEvent:
    """One filesystem change, as `$HOME`-relative paths.

    `dest` is set for a rename — the destination an atomic save actually names — and is checked as a
    target in its own right, since the source of a temp-file rename is not a tracked path.
    """

    path: str
    dest: str | None = None


@dataclass(frozen=True)
class WatchFilter:
    """The paths a watch cares about: this machine's exact items and its directory globs.

    The exact paths keep the **original** spelling. Directory names on macOS commonly differ in case
    (`Library/Application Support/…`), and a watcher scheduled on a lower-cased path never matches the
    events the filesystem reports, so the original is what `directories()` must hand back.
    """

    paths: tuple[str, ...]
    globs: tuple[PresetGlob, ...]

    @classmethod
    def create(cls, config: SyncConfig) -> WatchFilter:
        """Build the filter from the machine's own config; never from a record."""
        return cls(
            paths=tuple(item.path for item in config.items),
            globs=tuple(entry.glob for entry in config.globs),
        )

    def matches(self, event: FileEvent) -> bool:
        """True when `event` touched a path this machine tracks, source or rename destination."""
        return self._names(event.path) or (event.dest is not None and self._names(event.dest))

    def _names(self, path: str) -> bool:
        folded = collision_key(path)
        if any(collision_key(candidate) == folded for candidate in self.paths):
            return True
        return any(entry.matches(path) for entry in self.globs)

    def directories(self) -> tuple[str, ...]:
        """The `$HOME`-relative directories to observe, sorted and de-duplicated.

        The parent of each exact item plus each glob's own directory; an item at the home root has
        the empty string, which is `$HOME` itself. The **original** spelling is kept (only the
        de-duplication key is folded), so a directory is scheduled under the name the filesystem
        reports events with.
        """
        seen: dict[str, str] = {}
        for path in self.paths:
            parent = path.rpartition("/")[0]
            seen.setdefault(collision_key(parent), parent)
        for entry in self.globs:
            seen.setdefault(collision_key(entry.directory), entry.directory)
        return tuple(seen[key] for key in sorted(seen))


@dataclass
class Debounce:
    """Collapse a burst of events into one action, `quiet_seconds` after the last of them.

    Time is a **parameter**, never read here (``AGENTS.md`` §2.1): `touch(now)` records an event and
    `due(now)` asks whether the action should run. The action runs when the burst has been quiet for
    `quiet_seconds` **or** when it has been running for `max_seconds` — the second bound is what stops
    a directory under constant churn from deferring the sync forever. This is a state machine, not a
    boundary value, so it is mutable on purpose.
    """

    quiet_seconds: float
    max_seconds: float = DEFAULT_MAX_SECONDS
    first: float | None = field(default=None)
    last: float | None = field(default=None)
    pending: bool = field(default=False)

    def touch(self, now: float) -> None:
        """Record an event at `now`, extending the quiet window but not the maximum one."""
        if not self.pending or self.first is None:
            self.first = now
        self.last = now
        self.pending = True

    def due(self, now: float) -> bool:
        """True when an action is pending and either bound has been reached."""
        if not self.pending or self.first is None or self.last is None:
            return False
        return now - self.last >= self.quiet_seconds or now - self.first >= self.max_seconds

    def clear(self) -> None:
        """Mark the pending action as taken, so `due` is False until the next event."""
        self.pending = False
        self.first = None
        self.last = None


__all__ = ["DEFAULT_MAX_SECONDS", "DEFAULT_QUIET_SECONDS", "Debounce", "FileEvent", "WatchFilter"]
