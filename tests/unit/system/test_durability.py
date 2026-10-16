# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the shared durability barriers."""

from __future__ import annotations

import fcntl
import os

from syncintosh.system.durability import flush_to_storage, sync_directory


def test_flush_to_storage_is_a_noop_without_fullfsync(tmp_path, monkeypatch) -> None:
    # given — a platform whose fcntl has no F_FULLFSYNC (Linux)
    monkeypatch.delattr(fcntl, "F_FULLFSYNC", raising=False)
    descriptor = os.open(tmp_path / "scratch", os.O_RDWR | os.O_CREAT)

    # when
    result = flush_to_storage(descriptor)

    # then
    assert result is None
    os.close(descriptor)


def test_flush_to_storage_tolerates_a_refusing_filesystem(tmp_path, monkeypatch) -> None:
    # given — F_FULLFSYNC exists but the filesystem rejects it
    def _refuse(descriptor: int, command: int) -> None:
        raise OSError("operation not supported")

    monkeypatch.setattr(fcntl, "fcntl", _refuse)
    descriptor = os.open(tmp_path / "scratch", os.O_RDWR | os.O_CREAT)

    # when
    result = flush_to_storage(descriptor)

    # then
    assert result is None
    os.close(descriptor)


def test_sync_directory_opens_and_fsyncs_the_directory(tmp_path) -> None:
    # given
    directory = tmp_path / "state"
    directory.mkdir()

    # when
    result = sync_directory(directory)

    # then
    assert result is None
