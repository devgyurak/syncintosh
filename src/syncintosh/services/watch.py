# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: watch the tracked paths and sync once a change has settled.

The loop is small on purpose, and every piece that could be a judgement lives in `core.watch` (the
filter and the debounce) or in `sync` (the cycle). What is left is the orchestration the design note
asks for (``docs/design/01-sync-model.md``):

* a burst of events becomes **one** `sync` after the filesystem has been quiet for `quiet_seconds`;
* a **held lock** (`SyncLockError`) is not an error — the daemon or another terminal is syncing — so
  it is reported and retried after another quiet window;
* a **pending conflict** means only `resolve` may run, so nothing is attempted and it is reported;
* a **network failure** (`GitError`) is reported and the watch continues, because the user may be
  offline and the next change will try again;
* a watch that cannot **start** is the one failure that ends the command (`WatchError`): there is
  nothing to wait on, and a watch that reported success while observing nothing would be restarted
  by no supervisor and noticed by no user.

The clock and the stop signal are injected so the loop is testable without real time or threads
(``AGENTS.md`` §5.4, §5.6); `core.watch.Debounce` never reads a clock itself.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from syncintosh.core.errors import DomainError, PendingConflict, SyncLockError, WatchError
from syncintosh.core.ports import FileWatch
from syncintosh.core.watch import DEFAULT_QUIET_SECONDS, Debounce, WatchFilter
from syncintosh.services.sync import SyncReport

# How long one `wait` blocks before the loop re-checks the clock and the stop signal. It bounds the
# reaction time to a stop request; the debounce decides when a sync actually runs.
DEFAULT_POLL_SECONDS = 0.25

__all__ = ["DEFAULT_POLL_SECONDS", "watch"]


def watch(
    *,
    watch: FileWatch,
    targets: WatchFilter,
    run: Callable[[], SyncReport],
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    stop: threading.Event | None = None,
    on_report: Callable[[SyncReport], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Watch until `stop` is set (or forever), syncing after each settled burst.

    `run` is the sync call, supplied by the caller so this loop needs no ports of its own: the CLI
    passes a closure over `sync`, and a test passes a plain callable. `on_report`/`on_error` render
    an outcome or a failure; every failure of a **cycle** is reported rather than raised, and the one
    failure that is raised is a watch that could not start (`WatchError`).
    """
    debounce = Debounce(quiet_seconds=quiet_seconds)
    try:
        try:
            watch.start()
        except (OSError, RuntimeError) as exc:
            # Nothing to recover to: retrying would spin on a `wait` with no observer behind it, so
            # the watch would report success while observing nothing. Raise instead — the command
            # reports it and exits non-zero, which is what a supervisor restarts on (#8).
            raise WatchError(f"the watcher could not start: {str(exc) or type(exc).__name__}") from exc
        while stop is None or not stop.is_set():
            try:
                events = watch.wait(poll_seconds)
            except (OSError, RuntimeError) as exc:
                # The observer could not start (a path vanished, permissions, a watchdog
                # internal): report it and try again rather than ending the watch with a traceback.
                _report_error(on_error, exc)
                continue
            # Stamp the events with the time *after* the wait: an event's own time is not available,
            # and the pre-wait clock would understate how long the burst has been running.
            now = clock()
            if watch.rebuilt():
                # A rebuild drops whatever arrived while it ran, so it counts as a change on its own.
                debounce.touch(now)
            for event in events:
                if targets.matches(event):
                    debounce.touch(now)
            if not debounce.due(clock()):
                continue
            debounce.clear()
            _run_once(run, debounce=debounce, clock=clock, on_report=on_report, on_error=on_error)
    finally:
        watch.stop()


def _run_once(
    run: Callable[[], SyncReport],
    *,
    debounce: Debounce,
    clock: Callable[[], float],
    on_report: Callable[[SyncReport], None] | None,
    on_error: Callable[[Exception], None] | None,
) -> None:
    """Run one sync, turning a recoverable outcome into a report rather than a crash.

    The three the design note names are special-cased; **any** other domain error is reported too,
    because a watch that exits on the first surprise (`SecretScanError`, a corrupt manifest, a
    refused projection) leaves the user thinking it is still running. Nothing here propagates: the
    watch keeps going, and the failure is surfaced on the next render.
    """
    try:
        report = run()
    except SyncLockError as exc:
        # Another sync holds the lock. Re-arm the debounce so it is retried after another quiet
        # window instead of spinning on the lock.
        _report_error(on_error, exc)
        debounce.touch(clock())
        return
    except PendingConflict as exc:
        # A pending conflict: only `resolve` may run, so do nothing and say so.
        _report_error(on_error, exc)
        return
    except DomainError as exc:
        # A network failure, or any other domain error: report it and keep watching.
        _report_error(on_error, exc)
        return
    except OSError as exc:
        # A tracked file that cannot be read — permissions, or it became a directory — is a failure
        # for one file, not a reason to end the watch.
        _report_error(on_error, exc)
        return
    if on_report is not None:
        on_report(report)


def _report_error(on_error: Callable[[Exception], None] | None, exc: Exception) -> None:
    if on_error is not None:
        on_error(exc)
