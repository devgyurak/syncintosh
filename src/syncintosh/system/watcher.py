# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `FileWatch` over `watchdog`, emitting `$HOME`-relative `FileEvent`s.

`services.watch` decides *what* to do with an event; this module only turns filesystem activity into
events. It observes the **parent directories** of the tracked paths (one scheduling per directory,
never recursive), so a change anywhere in a watched directory is seen and the filter rejects the ones
that do not matter.

`watchdog` is not stdlib, so it sits behind the port (``AGENTS.md`` §2.9) and is imported lazily here
— the CLI imports this module only inside the `watch` command, keeping startup fast
(``AGENTS.md`` §4). `start` and `stop` are separate from the constructor, and the home is resolved in
`start`, so building it touches nothing (``AGENTS.md`` §2.6).
"""

from __future__ import annotations

import os
import queue
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from syncintosh.core.watch import FileEvent

# A single `wait` returns at most this many events, so a storm in a watched directory cannot make the
# service allocate without bound between two syncs; the rest stay queued for the next call.
MAX_EVENTS_PER_WAIT = 1024

# How long `stop` waits for the observer thread to end before giving up on it.
OBSERVER_JOIN_TIMEOUT_SECONDS = 5.0


class _RelativeHandler(FileSystemEventHandler):
    """Turns watchdog's absolute-path events into `$HOME`-relative `FileEvent`s.

    Two absolute paths can name one tracked file: the configured one, and the resolved one. FSEvents
    reports the **resolved** path, and `$HOME` or an intermediate directory may itself be a symlink (a
    `stow`-managed `~/.config` → `~/dotfiles/config`), so the handler is given the resolved home and,
    for each watched directory, its resolved form next to its configured one. An event under a resolved
    directory is mapped back to the configured prefix; one under the resolved home is relative to it.
    """

    def __init__(self, home: str, emit: Callable[[FileEvent], None], mappings: tuple[tuple[str, str], ...]) -> None:
        self._home = home
        self._emit = emit
        self._mappings = mappings

    def _rel(self, path: str) -> str | None:
        """The path relative to `$HOME`, or `None` when it is not under a watched directory.

        The watched-directory mappings are checked **first**, longest resolved prefix first: a
        `stow`-managed `~/.config` → `~/dotfiles/config` resolves *inside* `$HOME`, so without that
        order the event would be reported as `dotfiles/config/…` and never match the tracked path.
        """
        resolved = os.path.realpath(path)
        for real_dir, relative in self._mappings:
            if resolved == real_dir:
                return None
            if resolved.startswith(real_dir + os.sep):
                rest = resolved[len(real_dir) + 1 :]
                return rest if not relative else f"{relative}/{rest}"
        if resolved == self._home:
            return None
        if resolved.startswith(self._home + os.sep):
            return resolved[len(self._home) + 1 :]
        return None

    def _emit_change(self, source: str, dest: str | None) -> None:
        source_rel = self._rel(source)
        dest_rel = None if dest is None else self._rel(dest)
        if source_rel is None and dest_rel is None:
            return
        # A rename's destination is the target an atomic save actually names, so `path` falls back to
        # it when the source lies outside the home tree (a temp file in `/tmp` moved in).
        self._emit(FileEvent(path=source_rel or dest_rel or "", dest=dest_rel if source_rel else None))

    def on_created(self, event: FileSystemEvent) -> None:
        self._emit_change(os.fsdecode(event.src_path), None)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._emit_change(os.fsdecode(event.src_path), None)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._emit_change(os.fsdecode(event.src_path), None)

    def on_moved(self, event: FileSystemEvent) -> None:
        dest = getattr(event, "dest_path", None)
        self._emit_change(os.fsdecode(event.src_path), None if dest is None else os.fsdecode(dest))


class WatchdogFileWatch:
    """A `FileWatch` over the directories a set of tracked paths live in.

    A watched directory can stop producing events **without** the set of directory names changing: an
    app can delete and recreate it, or replace it in place, and watchdog's emitter for the old path
    stops on its own. The snapshot is therefore keyed by `(st_dev, st_ino)` as well as by name, so a
    replaced directory is noticed even when its name is unchanged, and the observer is rebuilt.

    A rebuild is reported through `rebuilt()`, because events that arrive while the observer is being
    rebuilt are lost — the caller syncs once and picks them up. The same re-check is what picks up a
    directory that did not exist when the watch started.
    """

    def __init__(self, *, home: str, directories: Iterable[str]) -> None:
        # Storing strings and a queue is not I/O, so this constructor is safe (§2.6); the home is
        # resolved in `start`, not here.
        self._home = home
        self._resolved = home
        self._directories = tuple(directories)
        self._events: queue.SimpleQueue[FileEvent] = queue.SimpleQueue()
        self._observer: BaseObserver | None = None
        self._seen: dict[str, tuple[int, int]] = {}
        self._rebuilt = False
        self._stopped = True

    def _snapshot(self) -> dict[str, tuple[int, int]]:
        """The watched directories that exist, keyed to the identity of the directory itself."""
        found: dict[str, tuple[int, int]] = {}
        for relative in self._directories:
            try:
                status = (Path(self._resolved) / relative).stat()
            except OSError:
                continue
            if stat.S_ISDIR(status.st_mode):
                found[relative] = (status.st_dev, status.st_ino)
        return found

    def _rebuild(self) -> None:
        """Teardown and re-schedule when the watched set changed or an emitter went silent.

        `_rebuilt` is set only when there is something to catch up on — the set changed, or an emitter
        died — so a quiet tree never reports a change and a watch with **no** directories reports
        nothing. Nothing is rebuilt on a timer: tearing the observer down loses any write that lands
        in the gap (measured at up to half of them), and the emitter check below notices a silent
        watch without one.
        """
        snapshot = self._snapshot()
        stale = self._observer is not None and (
            not self._observer.is_alive()
            # Per-emitter: when a watched directory is renamed away and back — the same name, the
            # same inode — the observer thread stays alive while the OS drops that watch for good.
            # Only the emitter knows.
            or any(not emitter.is_alive() for emitter in self._observer.emitters)
        )
        replaced = snapshot != self._seen
        if self._observer is not None and not stale and not replaced:
            return
        self._stop_observer()
        self._seen = snapshot
        if replaced or stale:
            self._rebuilt = True
        if not snapshot:
            return
        observer = Observer()
        mappings = tuple(
            sorted(
                ((os.path.realpath(str(Path(self._resolved) / relative)), relative) for relative in snapshot),
                key=lambda mapping: len(mapping[0]),
                reverse=True,
            )
        )
        handler = _RelativeHandler(self._resolved, self._events.put, mappings)
        try:
            for relative in snapshot:
                observer.schedule(handler, str(Path(self._resolved) / relative), recursive=False)
            observer.start()
        except BaseException:
            # A half-started observer keeps the emitters it already started, and the next rebuild then
            # fails with "already scheduled" — leaving that directory silent for good. Tear it down,
            # but never let the cleanup mask the failure that caused it: joining a thread that never
            # started raises exactly here.
            try:
                observer.stop()
                observer.join(timeout=OBSERVER_JOIN_TIMEOUT_SECONDS)
            except Exception:
                pass
            raise
        self._observer = observer

    def _stop_observer(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            # Bounded: a wedged emitter thread must not hang `watch` on the way out.
            observer.join(timeout=OBSERVER_JOIN_TIMEOUT_SECONDS)

    def start(self) -> None:
        """Schedule every watched directory that exists and start the observer."""
        self._resolved = os.path.realpath(self._home)
        self._seen = {}
        self._stopped = False
        self._rebuild()
        # The first scheduling is not a change: there was nothing to miss before it.
        self._rebuilt = False

    def retarget(self, directories: Iterable[str]) -> None:
        """Watch `directories` instead; the next `wait` re-schedules the observer if the set changed."""
        self._directories = tuple(directories)

    def stop(self) -> None:
        """Stop the observer and join its thread. Safe to call when it was never started."""
        self._stop_observer()
        self._seen = {}
        self._stopped = True

    def rebuilt(self) -> bool:
        """True when the watched set changed since the previous call; the caller syncs once."""
        rebuilt, self._rebuilt = self._rebuilt, False
        return rebuilt

    def wait(self, timeout: float) -> tuple[FileEvent, ...]:
        """Block up to `timeout` seconds, returning the events seen (at most `MAX_EVENTS_PER_WAIT`).

        The directory set is re-checked first, so a directory deleted and recreated between calls is
        scheduled again instead of staying silent forever. After `stop` it does nothing, so a stray
        call cannot bring the observer back.
        """
        if not self._stopped:
            self._rebuild()
        events: list[FileEvent] = []
        deadline = time.monotonic() + timeout
        while len(events) < MAX_EVENTS_PER_WAIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                events.append(self._events.get(timeout=remaining))
            except queue.Empty:
                break
        return tuple(events)


__all__ = ["MAX_EVENTS_PER_WAIT", "OBSERVER_JOIN_TIMEOUT_SECONDS", "WatchdogFileWatch"]
