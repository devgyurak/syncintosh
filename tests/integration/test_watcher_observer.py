# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a real `watchdog` observer sees an atomic save as a rename onto the target.

An atomic save is a temp file plus a rename, so the event that matters is the **destination** — the
one test a pure handler cannot prove. The consumer runs in a helper thread and the test blocks on a
`threading.Event` rather than polling (``AGENTS.md`` §5.4). No network, no real HOME (§5.6).
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

from syncintosh.core.config import LocalGlob, LocalItem, SyncConfig
from syncintosh.core.watch import WatchFilter
from syncintosh.system.watcher import WatchdogFileWatch

pytestmark = pytest.mark.integration

SETTINGS = "settings.json"


def _consume(watcher: WatchdogFileWatch, targets: WatchFilter, matched: threading.Event) -> None:
    """Set `matched` as soon as the watcher reports an event the filter accepts."""
    while not matched.is_set():
        events = watcher.wait(0.25)
        if watcher.rebuilt():
            # a rebuild drops whatever arrived while it ran; the service treats it as a change
            matched.set()
            return
        for event in events:
            if targets.matches(event):
                matched.set()
                return


def _rebuilt_within(watcher: WatchdogFileWatch, seconds: float) -> bool:
    """Wait for a rebuild, up to `seconds`: how a replaced directory is noticed is platform work.

    On APFS the recreated directory is a **new inode**, so the identity check sees it at once. On
    Linux the freed inode is **reused**, so the identity is unchanged and the only signal left is
    the emitter's own death — inotify reports that the watched directory was deleted, which takes a
    poll of its own. `rebuilt()` latches, so polling it loses nothing.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        watcher.wait(0.25)
        if watcher.rebuilt():
            return True
    return False


def _targets() -> WatchFilter:
    config = SyncConfig.create(
        remote="r",
        machine_id="m",
        items=(LocalItem.create(path=SETTINGS, preset="vscode", scope="common", format="jsonc"),),
    )
    return WatchFilter.create(config)


def _snippet_targets() -> WatchFilter:
    config = SyncConfig.create(
        remote="r",
        machine_id="m",
        items=(),
        globs=(LocalGlob.create(path="snippets/*.json", preset="vscode", scope="common", format="jsonc"),),
    )
    return WatchFilter.create(config)


def test_an_atomic_save_is_seen_as_a_rename_onto_the_target(tmp_path: Path) -> None:
    # given — a watched home directory and a tracked settings file
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / SETTINGS).write_text("{}", encoding="utf-8")
    targets = _targets()
    watcher = WatchdogFileWatch(home=str(home), directories=targets.directories())
    matched = threading.Event()
    watcher.start()
    worker = threading.Thread(target=_consume, args=(watcher, targets, matched), daemon=True)
    worker.start()

    try:
        # when — an atomic save writes a temp file and renames it onto the real path
        temp = home / ".settings.json.tmp"
        temp.write_text('{"a": 1}', encoding="utf-8")
        temp.replace(home / SETTINGS)

        # then — the consumer thread sets the Event; the test waits on it, never in a loop
        assert matched.wait(timeout=10.0) is True
    finally:
        watcher.stop()
        worker.join(timeout=5.0)


def test_a_watched_directory_is_rescheduled_after_it_is_recreated(tmp_path: Path) -> None:
    # given — a watched `snippets` directory (an app reinstalled deletes and recreates it)
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    targets = _snippet_targets()
    watcher = WatchdogFileWatch(home=str(home), directories=targets.directories())
    matched = threading.Event()
    watcher.start()
    worker = threading.Thread(target=_consume, args=(watcher, targets, matched), daemon=True)
    worker.start()

    try:
        # when — the directory is removed, then recreated, and only then a tracked file appears
        shutil.rmtree(home / "snippets")
        time.sleep(0.5)  # let the watcher notice the removal
        (home / "snippets").mkdir()
        time.sleep(1.0)  # let the watcher rebuild and schedule the recreated directory
        (home / "snippets" / "javascript.json").write_text("{}", encoding="utf-8")

        # then — the Event fires: the recreated directory is being watched again
        assert matched.wait(timeout=10.0) is True
    finally:
        watcher.stop()
        worker.join(timeout=5.0)


def test_a_directory_replaced_in_place_is_noticed(tmp_path: Path) -> None:
    # given — a watched directory that is removed and recreated **immediately**, so its name is
    # unchanged and only its identity, or its watch, differs
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    watcher = WatchdogFileWatch(home=str(home), directories=("snippets",))
    watcher.start()

    try:
        # when
        shutil.rmtree(home / "snippets")
        (home / "snippets").mkdir()

        # then — rebuilt rather than left dead, on the platform's own schedule
        assert _rebuilt_within(watcher, 10.0) is True
    finally:
        watcher.stop()


def test_wait_after_stop_does_not_restart_the_observer(tmp_path: Path) -> None:
    # given — a stopped watcher
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    watcher = WatchdogFileWatch(home=str(home), directories=("snippets",))
    watcher.start()
    watcher.stop()

    # when
    watcher.wait(0.1)

    # then — a stray wait does not bring it back to life
    assert watcher.rebuilt() is False
