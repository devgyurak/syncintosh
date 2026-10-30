# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the watch loop: one sync per settled burst, and the recoverable outcomes.

The watch port, the clock, and the sync call are all injected, so the loop is driven with plain
values — no real filesystem, no real time (``AGENTS.md`` §5.4, §5.6).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from itertools import count

import pytest

from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.errors import GitError, PendingConflict, SecretScanError, SyncLockError, WatchError
from syncintosh.core.watch import FileEvent, WatchFilter
from syncintosh.services.sync import SyncReport
from syncintosh.services.watch import watch


class _FakeWatch:
    """A `FileWatch` that hands back fixed batches, then sets a stop event."""

    def __init__(self, batches: list[tuple[FileEvent, ...]], stop: threading.Event) -> None:
        self._batches = list(batches)
        self._stop = stop
        self.started = False
        self.stopped = False
        self._rebuilt = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def retarget(self, directories) -> None:
        self.retargeted = tuple(directories)

    def rebuilt(self) -> bool:
        rebuilt, self._rebuilt = self._rebuilt, False
        return rebuilt

    def mark_rebuilt(self) -> None:
        self._rebuilt = True

    def wait(self, timeout: float) -> tuple[FileEvent, ...]:
        if self._batches:
            return self._batches.pop(0)
        self._stop.set()
        return ()


class _Run:
    """A sync call that yields scripted outcomes and counts its invocations."""

    def __init__(self, outcomes: list[SyncReport | BaseException], stop: threading.Event) -> None:
        self._outcomes = list(outcomes)
        self._stop = stop
        self.calls = 0

    def __call__(self) -> SyncReport:
        self.calls += 1
        outcome = self._outcomes.pop(0) if self._outcomes else SyncReport()
        if isinstance(outcome, BaseException):
            raise outcome
        self._stop.set()
        return outcome


def _targets() -> WatchFilter:
    config = SyncConfig.create(
        remote="r",
        machine_id="m",
        items=(LocalItem.create(path=".zshrc", preset="zsh", scope="common", format="text"),),
    )
    return WatchFilter.create(config)


def _watch(
    batches: list[tuple[FileEvent, ...]],
    outcomes: list[SyncReport | BaseException],
    *,
    on_report: Callable[[SyncReport], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> tuple[_FakeWatch, _Run]:
    stop = threading.Event()
    watcher = _FakeWatch(batches, stop)
    run = _Run(outcomes, stop)
    watch(
        watch=watcher,
        targets=_targets(),
        run=run,
        quiet_seconds=0.0,
        clock=lambda: 0.0,
        stop=stop,
        on_report=on_report,
        on_error=on_error,
    )
    return watcher, run


def test_a_settled_burst_runs_one_sync() -> None:
    # given
    reports: list[SyncReport] = []

    # when
    watcher, run = _watch([(FileEvent(".zshrc"),)], [], on_report=reports.append)

    # then — exactly one sync for the burst, and the observer is stopped on the way out
    assert run.calls == 1
    assert len(reports) == 1
    assert watcher.started is True
    assert watcher.stopped is True


def test_a_poll_interval_runs_a_sync_with_no_local_change() -> None:
    # given — a clock that jumps past the poll interval, and no file events
    stop = threading.Event()
    watcher = _FakeWatch([()], stop)
    run = _Run([], stop)
    ticks = count(0, 100)

    # when
    watch(
        watch=watcher,
        targets=_targets(),
        run=run,
        quiet_seconds=0.0,
        poll_seconds=50.0,
        clock=lambda: float(next(ticks)),
        stop=stop,
    )

    # then — one sync ran although nothing changed locally
    assert run.calls == 1


def test_a_sub_second_poll_interval_is_refused() -> None:
    # given — a poll interval that would fetch and push in a loop
    stop = threading.Event()

    # when & then — refused at the service, so a hand-edited plist cannot cause it either
    with pytest.raises(ValueError):
        watch(
            watch=_FakeWatch([], stop),
            targets=_targets(),
            run=lambda: SyncReport(),
            poll_seconds=0.0,
            stop=stop,
        )


def test_an_event_that_is_not_tracked_never_syncs() -> None:
    # given — a change in a watched directory that no item or glob names
    # when
    watcher, run = _watch([(FileEvent("notes.md"),)], [])

    # then
    assert run.calls == 0
    assert watcher.stopped is True


def test_a_held_lock_is_reported_and_retried() -> None:
    # given — the first attempt loses the lock, the second succeeds
    errors: list[BaseException] = []

    # when
    _, run = _watch(
        [(FileEvent(".zshrc"),)],
        [SyncLockError("another sync is running"), SyncReport()],
        on_error=errors.append,
    )

    # then — retried once, and the lock failure was reported rather than swallowed
    assert run.calls == 2
    assert len(errors) == 1


def test_a_pending_conflict_is_reported_and_not_retried() -> None:
    # given
    errors: list[BaseException] = []

    # when
    _, run = _watch([(FileEvent(".zshrc"),)], [PendingConflict("resolve first")], on_error=errors.append)

    # then — only `resolve` may run, so nothing is retried
    assert run.calls == 1
    assert len(errors) == 1


def test_a_network_failure_is_reported_and_the_watch_continues() -> None:
    # given — a push fails, then a later burst succeeds
    errors: list[BaseException] = []

    # when
    _, run = _watch(
        [(FileEvent(".zshrc"),), (FileEvent(".zshrc"),)],
        [GitError("offline"), SyncReport()],
        on_error=errors.append,
    )

    # then — the failure did not stop the loop, and the next burst synced
    assert run.calls == 2
    assert len(errors) == 1


def test_an_unexpected_domain_error_is_reported_and_the_watch_continues() -> None:
    # given — a scan refusal, which is neither the lock, a pending conflict, nor a network failure
    errors: list[BaseException] = []

    # when
    _, run = _watch(
        [(FileEvent(".zshrc"),), (FileEvent(".zshrc"),)],
        [SecretScanError("possible token at line 1"), SyncReport()],
        on_error=errors.append,
    )

    # then — reported, and the watch survived to sync the next burst
    assert run.calls == 2
    assert len(errors) == 1


def test_a_rebuild_counts_as_a_change() -> None:
    # given — the watcher rebuilt (a directory was replaced), so events during the rebuild were lost
    stop = threading.Event()
    watcher = _FakeWatch([(), ()], stop)
    watcher.mark_rebuilt()
    run = _Run([SyncReport()], stop)

    # when
    watch(watch=watcher, targets=_targets(), run=run, quiet_seconds=0.0, clock=lambda: 0.0, stop=stop)

    # then — the rebuild alone triggered a sync
    assert run.calls == 1


def test_an_os_error_is_reported_and_the_watch_continues() -> None:
    # given — a tracked file that cannot be read (permissions, or it became a directory)
    errors: list[BaseException] = []

    # when
    _, run = _watch(
        [(FileEvent(".zshrc"),), (FileEvent(".zshrc"),)],
        [OSError("permission denied"), SyncReport()],
        on_error=errors.append,
    )

    # then — reported, not fatal
    assert run.calls == 2
    assert len(errors) == 1


class _RaisingWatch:
    """A `FileWatch` whose `wait` raises before it delivers events (the observer cannot start)."""

    def __init__(
        self,
        errors: list[BaseException],
        batches: list[tuple[FileEvent, ...]],
        stop: threading.Event,
        start_failure: BaseException | None = None,
    ) -> None:
        self._errors = list(errors)
        self._batches = list(batches)
        self._stop = stop
        self._start_failure = start_failure

    def start(self) -> None:
        if self._start_failure is not None:
            raise self._start_failure

    def stop(self) -> None: ...

    def retarget(self, directories) -> None:
        self.retargeted = tuple(directories)

    def rebuilt(self) -> bool:
        return False

    def wait(self, timeout: float) -> tuple[FileEvent, ...]:
        if self._errors:
            raise self._errors.pop(0)
        if self._batches:
            return self._batches.pop(0)
        self._stop.set()
        return ()


@pytest.mark.parametrize("failure", [OSError("cannot watch"), RuntimeError("already scheduled")])
def test_a_failure_from_the_watch_port_is_reported_and_the_watch_continues(failure) -> None:
    # given — the observer cannot start (a path vanished, permissions, a watchdog internal), then a
    # burst arrives
    errors: list[BaseException] = []
    stop = threading.Event()
    watcher = _RaisingWatch([failure], [(FileEvent(".zshrc"),)], stop)
    run = _Run([SyncReport()], stop)

    # when
    watch(
        watch=watcher,
        targets=_targets(),
        run=run,
        quiet_seconds=0.0,
        clock=lambda: 0.0,
        stop=stop,
        on_error=errors.append,
    )

    # then
    assert run.calls == 1
    assert len(errors) == 1


def test_an_error_from_watch_start_ends_the_command_with_a_domain_error() -> None:
    # given — the observer cannot even start, and the loop is told how to report
    errors: list[BaseException] = []
    stop = threading.Event()
    watcher = _RaisingWatch([], [], stop, start_failure=RuntimeError("cannot start"))
    run = _Run([SyncReport()], stop)

    # when & then — `WatchError` is raised, not reported-and-returned: a watch that observed nothing
    # must not end the command with a success code (`services/watch.py`)
    with pytest.raises(WatchError, match="cannot start"):
        watch(
            watch=watcher,
            targets=_targets(),
            run=run,
            quiet_seconds=0.0,
            clock=lambda: 0.0,
            stop=stop,
            on_error=errors.append,
        )
    assert run.calls == 0
    assert errors == []


def _run_with_refresh(refresh) -> tuple[_FakeWatch, list[BaseException]]:
    stop = threading.Event()
    watcher = _FakeWatch([(FileEvent(".zshrc"),)], stop)
    errors: list[BaseException] = []
    watch(
        watch=watcher,
        targets=_targets(),
        run=_Run([], stop),
        quiet_seconds=0.0,
        clock=lambda: 0.0,
        stop=stop,
        on_error=errors.append,
        refresh=refresh,
    )
    return watcher, errors


def test_a_changed_set_of_targets_retargets_the_observer_after_a_sync() -> None:
    # given — a sync that adds a profile's directory to what is worth watching
    config = SyncConfig.create(
        remote="r",
        machine_id="m",
        items=(LocalItem.create(path="d/.zshrc", preset="zsh", scope="common", format="text"),),
    )

    # when
    watcher, errors = _run_with_refresh(lambda: WatchFilter.create(config))

    # then
    assert (watcher.retargeted, errors) == (("d",), [])


def test_an_unchanged_set_of_targets_does_not_retarget() -> None:
    # when
    watcher, _ = _run_with_refresh(_targets)

    # then
    assert hasattr(watcher, "retargeted") is False


def test_a_refresh_that_fails_is_reported_and_the_watch_continues() -> None:
    # given
    def broken() -> WatchFilter:
        raise OSError("gone")

    # when
    watcher, errors = _run_with_refresh(broken)

    # then
    assert ([str(e) for e in errors], watcher.stopped) == (["gone"], True)
