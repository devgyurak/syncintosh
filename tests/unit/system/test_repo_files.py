# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the filesystem `RepoFiles` implementation."""

from __future__ import annotations

import fcntl
import os

import pytest

import syncintosh.system.repo_files as repo_files
from syncintosh.system.repo_files import FileRepo


def test_reading_a_missing_file_is_none(tmp_path) -> None:
    # given
    repo = FileRepo(tmp_path)

    # when & then
    assert repo.read("objects/missing.enc") is None


def test_write_creates_parent_directories_and_reads_back(tmp_path) -> None:
    # given
    repo = FileRepo(tmp_path)

    # when
    repo.write("objects/ab.enc", b"ciphertext")

    # then
    assert repo.read("objects/ab.enc") == b"ciphertext"


def test_delete_removes_a_file_and_tolerates_a_missing_one(tmp_path) -> None:
    # given
    repo = FileRepo(tmp_path)
    repo.write("manifest.enc", b"m")

    # when
    repo.delete("manifest.enc")
    repo.delete("manifest.enc")

    # then
    assert repo.read("manifest.enc") is None


def test_list_paths_is_sorted_and_empty_for_a_missing_prefix(tmp_path) -> None:
    # given
    repo = FileRepo(tmp_path)
    repo.write("objects/b.enc", b"2")
    repo.write("objects/a.enc", b"1")
    repo.write("manifest.enc", b"m")

    # when
    objects = repo.list_paths("objects")
    keys = repo.list_paths("keys")

    # then
    assert objects == ("objects/a.enc", "objects/b.enc")
    assert keys == ()


def test_a_failed_write_keeps_the_previous_file_and_leaves_no_temp(tmp_path, monkeypatch) -> None:
    # given — the atomic write fails after the temp file is created
    repo = FileRepo(tmp_path)
    repo.write("manifest.enc", b"original")

    def _fail(descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", _fail)

    # when & then
    with pytest.raises(OSError):
        repo.write("manifest.enc", b"new")
    assert repo.read("manifest.enc") == b"original"
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


def test_a_successful_write_flushes_to_storage_and_syncs_the_directory(tmp_path, monkeypatch) -> None:
    # given — the two durability barriers are instrumented
    calls: list[str] = []
    monkeypatch.setattr(repo_files, "_flush_to_storage", lambda descriptor: calls.append("storage"))
    monkeypatch.setattr(repo_files, "_sync_directory", lambda path: calls.append("dir"))
    repo = FileRepo(tmp_path)

    # when
    repo.write("objects/a.enc", b"data")

    # then
    assert calls == ["storage", "dir"]


def test_flush_to_storage_is_a_noop_without_fullfsync(tmp_path, monkeypatch) -> None:
    # given — a platform whose fcntl has no F_FULLFSYNC (Linux)
    monkeypatch.delattr(fcntl, "F_FULLFSYNC", raising=False)
    descriptor = os.open(tmp_path / "scratch", os.O_RDWR | os.O_CREAT)

    # when
    result = repo_files._flush_to_storage(descriptor)

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
    result = repo_files._flush_to_storage(descriptor)

    # then
    assert result is None
    os.close(descriptor)
