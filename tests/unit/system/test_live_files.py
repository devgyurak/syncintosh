# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the filesystem `LiveFiles` implementation."""

from __future__ import annotations

import os
import stat

import pytest

import syncintosh.system.live_files as live_files
from syncintosh.system.live_files import SystemLiveFiles


def test_reading_a_missing_file_is_none(tmp_path) -> None:
    # given
    live = SystemLiveFiles()

    # when & then
    assert live.read(str(tmp_path / "missing.txt")) is None


def test_write_creates_parent_directories_and_reads_back(tmp_path) -> None:
    # given
    live = SystemLiveFiles()
    target = tmp_path / "nested" / "settings.json"

    # when
    live.write(str(target), b"{}")

    # then
    assert live.read(str(target)) == b"{}"
    assert live.exists(str(target)) is True


def test_remove_deletes_a_file_and_tolerates_a_missing_one(tmp_path) -> None:
    # given
    live = SystemLiveFiles()
    target = tmp_path / "a.txt"
    live.write(str(target), b"x")

    # when
    live.remove(str(target))
    live.remove(str(target))

    # then
    assert live.exists(str(target)) is False


def test_is_symlink_and_exists_treat_a_broken_link_as_existing(tmp_path) -> None:
    # given — a link whose target does not exist
    live = SystemLiveFiles()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "nowhere")

    # when & then — `apply` must see it and refuse to write through it
    assert live.is_symlink(str(link)) is True
    assert live.exists(str(link)) is True


def test_a_failed_write_keeps_the_previous_file_and_leaves_no_temp(tmp_path, monkeypatch) -> None:
    # given
    live = SystemLiveFiles()
    target = tmp_path / "settings.json"
    live.write(str(target), b"original")

    def _fail(descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", _fail)

    # when & then
    with pytest.raises(OSError):
        live.write(str(target), b"new")
    assert live.read(str(target)) == b"original"
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


def test_overwriting_preserves_the_file_mode(tmp_path) -> None:
    # given — an executable file, as an applied shell script would be
    live = SystemLiveFiles()
    target = tmp_path / "script.sh"
    live.write(str(target), b"#!/bin/sh\n")
    target.chmod(0o755)

    # when
    live.write(str(target), b"#!/bin/sh\necho hi\n")

    # then — the write must not silently drop the executable bit
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_a_new_file_gets_the_umask_default(tmp_path) -> None:
    # given — `mkstemp` creates 0600, which would leave a new settings file owner-only
    live = SystemLiveFiles()
    current = os.umask(0o022)
    os.umask(current)
    target = tmp_path / "new.json"

    # when
    live.write(str(target), b"{}")

    # then
    assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~current


def test_a_write_flushes_to_storage_and_syncs_the_directory(tmp_path, monkeypatch) -> None:
    # given
    calls: list[str] = []
    monkeypatch.setattr(live_files, "flush_to_storage", lambda descriptor: calls.append("storage"))
    monkeypatch.setattr(live_files, "sync_directory", lambda path: calls.append("dir"))

    # when
    SystemLiveFiles().write(str(tmp_path / "x"), b"data")

    # then
    assert calls == ["storage", "dir"]


def test_list_dir_returns_the_non_directory_entries_sorted(tmp_path) -> None:
    # given — two files and a subdirectory, which a glob never recurses into
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    live = SystemLiveFiles()

    # when
    names = live.list_dir(str(tmp_path))

    # then
    assert names == ("a.json", "b.json")


def test_list_dir_includes_a_symlink_that_points_at_a_directory(tmp_path) -> None:
    # given — a symlink to a directory is a symlink first, so the collector must see it and report it
    (tmp_path / "real").mkdir()
    (tmp_path / "link.json").symlink_to(tmp_path / "real")
    live = SystemLiveFiles()

    # when & then
    assert live.list_dir(str(tmp_path)) == ("link.json",)


def test_list_dir_of_a_missing_or_non_directory_is_empty(tmp_path) -> None:
    # given — the app is not installed, or the path is a file rather than a directory
    (tmp_path / "file.json").write_text("{}", encoding="utf-8")
    live = SystemLiveFiles()

    # when & then
    assert live.list_dir(str(tmp_path / "missing")) == ()
    assert live.list_dir(str(tmp_path / "file.json")) == ()


def test_list_dir_excludes_a_named_pipe(tmp_path) -> None:
    # given — a FIFO is not a settings file, and a blocking read of one would hang the collect
    os.mkfifo(tmp_path / "pipe.json")
    (tmp_path / "real.json").write_text("{}", encoding="utf-8")

    # when & then
    assert SystemLiveFiles().list_dir(str(tmp_path)) == ("real.json",)
