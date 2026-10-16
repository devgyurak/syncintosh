# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the filesystem `LiveFiles` implementation."""

from __future__ import annotations

import os

import pytest

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
