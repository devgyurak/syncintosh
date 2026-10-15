# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the filesystem `RepoFiles` implementation."""

from __future__ import annotations

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
