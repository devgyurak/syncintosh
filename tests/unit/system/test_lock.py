# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the advisory sync lock.

The load-bearing behaviour: a second caller waits a bounded time, then reports rather than
racing (``docs/design/01-sync-model.md`` step 1).
"""

from __future__ import annotations

import pytest

from syncintosh.core.errors import SyncLockError
from syncintosh.system.lock import FileLock


def test_acquire_then_release_then_reacquire(tmp_path) -> None:
    # given
    lock = FileLock(tmp_path / "sync.lock", timeout_seconds=0.2)

    # when
    lock.acquire()
    lock.release()
    lock.acquire()

    # then — released and re-taken without a timeout
    lock.release()


def test_acquiring_twice_is_idempotent(tmp_path) -> None:
    # given
    lock = FileLock(tmp_path / "sync.lock", timeout_seconds=0.2)

    # when
    lock.acquire()
    lock.acquire()

    # then
    lock.release()
    lock.release()


def test_releasing_without_acquiring_is_not_an_error(tmp_path) -> None:
    # given
    lock = FileLock(tmp_path / "sync.lock")

    # when
    result = lock.release()

    # then
    assert result is None


def test_a_second_caller_reports_after_the_bounded_wait(tmp_path) -> None:
    # given — the first lock is held for the duration
    path = tmp_path / "sync.lock"
    holder = FileLock(path, timeout_seconds=0.2)
    holder.acquire()
    waiter = FileLock(path, timeout_seconds=0.05, poll_seconds=0.005)

    # when & then
    with pytest.raises(SyncLockError, match="another sync is running"):
        waiter.acquire()
    holder.release()


def test_the_lock_is_taken_once_the_holder_releases(tmp_path) -> None:
    # given
    path = tmp_path / "sync.lock"
    holder = FileLock(path, timeout_seconds=0.2)
    holder.acquire()
    waiter = FileLock(path, timeout_seconds=0.2, poll_seconds=0.005)

    # when
    holder.release()
    waiter.acquire()

    # then
    waiter.release()


def test_the_lock_creates_its_parent_directory(tmp_path) -> None:
    # given — the state directory does not exist yet
    path = tmp_path / "state" / "sync.lock"

    # when
    lock = FileLock(path, timeout_seconds=0.2)
    lock.acquire()

    # then
    assert path.exists()
    lock.release()
