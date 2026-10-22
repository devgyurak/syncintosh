# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `doctor` checks.

Each check reports rather than raises, and a failure in one must not hide the others. The ports
are stubbed, so no test runs `launchctl`, touches the network, or holds the real lock.
"""

from __future__ import annotations

from typing import cast

from syncintosh.core.errors import SyncLockError
from syncintosh.core.ports import GitTransport, KeyStore, PendingStore, SyncLock
from syncintosh.services.doctor import doctor

KEY = bytes(range(32))
CHECK_NAMES = ("daemon", "pending", "lock", "remote", "generations")


class _Transport:
    """The `GitTransport` calls `doctor` makes, with each outcome fixed."""

    def __init__(self, *, remote_tip: str | None = "a" * 40, reachable: bool = True, generations=(1,)) -> None:
        self._remote_tip = remote_tip
        self._reachable = reachable
        self._generations = generations

    def fetch(self, remote: str, branch: str) -> None:
        return None

    def head(self) -> str | None:
        return "a" * 40

    def resolve(self, rev: str) -> str | None:
        return self._remote_tip

    def generations_in_reachable_objects(self, tips: tuple[str, ...]) -> tuple[int, ...]:
        return self._generations

    def remote_reachable(self, remote: str) -> bool:
        return self._reachable


class _Pending:
    def __init__(self, *, pending: bool = False) -> None:
        self._pending = pending

    def is_pending(self) -> bool:
        return self._pending

    def record(self, state) -> None:
        return None

    def clear(self) -> None:
        return None


class _Lock:
    def __init__(self, *, held: bool = False) -> None:
        self._held = held

    def acquire(self) -> None:
        if self._held:
            raise SyncLockError("held")

    def release(self) -> None:
        return None


def _doctor(*, transport=None, store=None, lock=None, pending=None, plist=None, expected=None, job_loaded=None):
    return doctor(
        transport=cast(GitTransport, transport or _Transport()),
        store=cast(KeyStore, store),
        lock=cast(SyncLock, lock or _Lock()),
        pending=cast(PendingStore, pending or _Pending()),
        remote="a remote",
        branch="main",
        plist_entry_point=plist,
        expected_entry_point=expected,
        job_loaded=job_loaded,
    )


def test_a_healthy_install_passes_every_check(memory_key_store) -> None:
    # given — a machine that holds the one reachable generation
    store = memory_key_store({1: KEY})

    # when
    report = _doctor(store=store, plist="/bin/syncintosh", expected="/bin/syncintosh")

    # then
    assert report.healthy is True
    assert tuple(check.name for check in report.checks) == CHECK_NAMES


def test_a_stale_plist_is_detected(memory_key_store) -> None:
    # given — the plist runs an older path than this build
    store = memory_key_store({1: KEY})

    # when
    report = _doctor(store=store, plist="/usr/bin/python-old", expected="/opt/homebrew/bin/syncintosh")

    # then
    daemon = next(check for check in report.checks if check.name == "daemon")
    assert daemon.ok is False
    assert "/usr/bin/python-old" in daemon.detail
    assert report.healthy is False


def test_a_missing_daemon_is_not_a_failure(memory_key_store) -> None:
    # given — no plist was ever installed
    store = memory_key_store({1: KEY})

    # when
    report = _doctor(store=store, plist=None, expected="/bin/syncintosh")

    # then
    daemon = next(check for check in report.checks if check.name == "daemon")
    assert daemon.ok is True


def test_an_installed_but_unloaded_job_fails(memory_key_store) -> None:
    # given — the plist is current, but launchd has not loaded the job
    store = memory_key_store({1: KEY})

    # when
    report = _doctor(store=store, plist="/bin/syncintosh", expected="/bin/syncintosh", job_loaded=False)

    # then
    daemon = next(check for check in report.checks if check.name == "daemon")
    assert daemon.ok is False


def test_a_generation_reachable_history_needs_but_this_machine_lacks_fails(memory_key_store) -> None:
    # given — reachable objects use generation 2, but the ring holds only generation 1
    store = memory_key_store({1: KEY})
    transport = _Transport(generations=(1, 2))

    # when
    report = _doctor(transport=transport, store=store)

    # then
    generations = next(check for check in report.checks if check.name == "generations")
    assert generations.ok is False
    assert "2" in generations.detail


def test_an_unreachable_remote_fails(memory_key_store) -> None:
    # given
    store = memory_key_store({1: KEY})

    # when
    report = _doctor(transport=_Transport(reachable=False), store=store)

    # then
    remote = next(check for check in report.checks if check.name == "remote")
    assert remote.ok is False


def test_a_held_lock_fails(memory_key_store) -> None:
    # given — another sync holds the lock
    store = memory_key_store({1: KEY})

    # when
    report = _doctor(store=store, lock=_Lock(held=True))

    # then
    lock = next(check for check in report.checks if check.name == "lock")
    assert lock.ok is False


def test_a_pending_conflict_fails(memory_key_store) -> None:
    # given
    store = memory_key_store({1: KEY})

    # when
    report = _doctor(store=store, pending=_Pending(pending=True))

    # then
    pending = next(check for check in report.checks if check.name == "pending")
    assert pending.ok is False
