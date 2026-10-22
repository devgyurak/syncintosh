# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the watchdog event adapter: absolute paths to `$HOME`-relative events."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest
from watchdog.events import FileMovedEvent, FileSystemEvent
from watchdog.observers.api import BaseObserver

import syncintosh.system.watcher as watcher_module
from syncintosh.core.watch import FileEvent
from syncintosh.system.watcher import WatchdogFileWatch, _RelativeHandler


def test_a_change_inside_home_is_reported_relative(tmp_path) -> None:
    # given
    home = str(tmp_path / "home")
    emitted: list[FileEvent] = []
    handler = _RelativeHandler(os.path.realpath(home), emitted.append, ())

    # when
    handler.on_created(FileSystemEvent(f"{home}/.zshrc.tmp"))

    # then
    assert emitted == [FileEvent(path=".zshrc.tmp")]


def test_a_rename_reports_its_destination(tmp_path) -> None:
    # given
    home = str(tmp_path / "home")
    emitted: list[FileEvent] = []
    handler = _RelativeHandler(os.path.realpath(home), emitted.append, ())

    # when
    handler.on_moved(FileMovedEvent(f"{home}/.zshrc.tmp", f"{home}/.zshrc"))

    # then — the destination an atomic save names is carried so the filter can check it
    assert emitted == [FileEvent(path=".zshrc.tmp", dest=".zshrc")]


def test_an_event_outside_home_is_ignored(tmp_path) -> None:
    # given
    home = str(tmp_path / "home")
    emitted: list[FileEvent] = []
    handler = _RelativeHandler(os.path.realpath(home), emitted.append, ())

    # when
    handler.on_created(FileSystemEvent("/elsewhere/file"))

    # then
    assert emitted == []


def test_the_home_directory_itself_is_not_a_file_event(tmp_path) -> None:
    # given
    home = str(tmp_path / "home")
    handler = _RelativeHandler(os.path.realpath(home), lambda _event: None, ())

    # when & then — `.` names the directory, not a tracked path
    assert handler._rel(home) is None


def test_an_unstarted_watcher_waits_cleanly_and_stops(tmp_path) -> None:
    # given — a watcher that was never started, so its queue is empty
    watcher = WatchdogFileWatch(home=str(tmp_path), directories=())

    # when
    events = watcher.wait(0.0)

    # then — no events, and stopping it (never started) is not an error
    assert events == ()
    watcher.stop()


def test_a_home_reached_through_a_symlink_still_matches(tmp_path) -> None:
    # given — `$HOME` is a symlink, so the filesystem reports events under the target's path
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linkhome"
    link.symlink_to(real)
    emitted: list[FileEvent] = []
    handler = _RelativeHandler(os.path.realpath(str(link)), emitted.append, ())

    # when
    handler.on_created(FileSystemEvent(str(real / ".zshrc.tmp")))

    # then — the resolved comparison recognises it as inside the home tree
    assert emitted == [FileEvent(path=".zshrc.tmp")]


def test_an_event_under_a_symlinked_watched_directory_is_mapped_back(tmp_path) -> None:
    # given — `~/.config` is a symlink to `~/dotfiles/config` (a `stow` layout), so the filesystem
    # reports the resolved path rather than the configured one
    home = tmp_path / "home"
    (home / "dotfiles" / "config").mkdir(parents=True)
    (home / ".config").symlink_to(home / "dotfiles" / "config")
    resolved_dir = os.path.realpath(str(home / ".config"))
    emitted: list[FileEvent] = []
    handler = _RelativeHandler(os.path.realpath(str(home)), emitted.append, ((resolved_dir, ".config"),))

    # when
    handler.on_created(FileSystemEvent(str(Path(resolved_dir) / "settings.json")))

    # then — it maps back to the configured prefix, so the filter can match it
    assert emitted == [FileEvent(path=".config/settings.json")]


class _DeadObserver:
    """An observer whose thread (or one of whose emitters) has ended, so it must be rebuilt."""

    def __init__(self, *, emitters: tuple = ()) -> None:
        self.emitters = emitters

    def is_alive(self) -> bool:
        return False

    def stop(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...


class _DeadEmitter:
    """An emitter that watchdog still reports while the OS has dropped its watch."""

    def is_alive(self) -> bool:
        return False


def test_a_dead_observer_is_rebuilt(tmp_path) -> None:
    # given — the observer thread has died without the watched set changing
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    watcher = WatchdogFileWatch(home=str(home), directories=("snippets",))
    watcher.start()
    try:
        watcher._seen = watcher._snapshot()
        watcher._observer = cast(BaseObserver, _DeadObserver())

        # when
        watcher.wait(0.1)

        # then — it is rebuilt and reported, so the service catches up on the missed window
        assert watcher.rebuilt() is True
    finally:
        watcher.stop()


def test_a_replaced_watched_directory_is_rebuilt_from_its_identity_alone(tmp_path, monkeypatch) -> None:
    # given — a directory removed and recreated under the same name. Whether that shows up as a new
    # inode is the platform's business (Linux reuses the freed one), so the identity is stubbed here;
    # the real check is exercised end to end in `tests/integration/test_watcher_observer.py`.
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    watcher = WatchdogFileWatch(home=str(home), directories=("snippets",))
    watcher.start()
    try:
        watcher._seen = watcher._snapshot()
        monkeypatch.setattr(watcher, "_snapshot", lambda: {"snippets": (1, 2)})

        # when
        watcher.wait(0.1)

        # then — reported, so the service catches up on whatever the rebuild dropped
        assert watcher.rebuilt() is True
    finally:
        watcher.stop()


def test_no_watched_directories_report_no_change(tmp_path) -> None:
    # given — a watch whose only directory does not exist
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    watcher = WatchdogFileWatch(home=str(home), directories=("missing",))
    watcher.start()
    try:
        # when — several waits with nothing to watch
        [watcher.wait(0.01) for _ in range(3)]

        # then — no rebuild is reported, so the service does not sync on a timer
        assert watcher.rebuilt() is False
    finally:
        watcher.stop()


def test_a_dead_emitter_is_rebuilt_even_though_the_observer_lives(tmp_path) -> None:
    # given — a directory renamed away and back: the observer thread lives, its emitter does not
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    watcher = WatchdogFileWatch(home=str(home), directories=("snippets",))
    watcher.start()
    try:
        watcher._seen = watcher._snapshot()
        watcher._observer = cast(BaseObserver, _DeadObserver(emitters=(_DeadEmitter(),)))

        # when
        watcher.wait(0.1)

        # then
        assert watcher.rebuilt() is True
    finally:
        watcher.stop()


class _UnjoinableObserver:
    """An observer whose cleanup raises, the way joining a thread that never started does."""

    def __init__(self) -> None:
        self.stopped = False

    def schedule(self, handler, path: str, recursive: bool) -> None: ...

    def start(self) -> None:
        raise RuntimeError("already scheduled")

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        raise RuntimeError("cannot join thread before it is started")

    def is_alive(self) -> bool:
        return True

    emitters: tuple = ()


class _FailingObserver:
    """An observer whose `schedule` works but whose `start` raises, as watchdog can on macOS."""

    def __init__(self) -> None:
        self.scheduled: list[str] = []
        self.stopped = False
        self.joined = False
        self.emitters: tuple = ()

    def schedule(self, handler, path: str, recursive: bool) -> None:
        self.scheduled.append(path)

    def start(self) -> None:
        raise RuntimeError("already scheduled")

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True

    def is_alive(self) -> bool:
        return True


@pytest.mark.parametrize("observer_class", [_FailingObserver, _UnjoinableObserver])
def test_a_failing_observer_start_is_cleaned_up_and_re_raised(tmp_path, monkeypatch, observer_class) -> None:
    # given — an observer whose `start` fails partway (the shape that leaves "already scheduled"),
    # and one whose cleanup itself raises ("cannot join thread before it is started")
    created: list = []

    def _factory():
        observer = observer_class()
        created.append(observer)
        return observer

    monkeypatch.setattr(watcher_module, "Observer", _factory)
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    watcher = WatchdogFileWatch(home=str(home), directories=("snippets",))

    # when & then — the **original** failure is raised, and the observer was asked to stop
    with pytest.raises(RuntimeError, match="already scheduled"):
        watcher.start()
    assert created[0].stopped is True
