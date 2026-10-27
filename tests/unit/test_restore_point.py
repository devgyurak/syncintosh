# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the restore-point check a model's proposal must pass before it overwrites a file."""

from __future__ import annotations

import hashlib

import pytest

from syncintosh.core.manifest import ManifestRecord
from syncintosh.services.sync import Backup, _in_restore_point

FILE_ID = "0" * 32


def _record(content: bytes) -> ManifestRecord:
    return ManifestRecord.create(
        file_id=FILE_ID,
        path=".zshrc",
        preset="zsh",
        scope="common",
        format="text",
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_a_file_whose_current_content_is_in_the_restore_point_may_be_overwritten() -> None:
    # given
    backup = Backup.create("c" * 40, records={FILE_ID: _record(b"one\n")})

    # when & then
    assert _in_restore_point(backup, FILE_ID, b"one\n") is True


@pytest.mark.parametrize(
    ("records", "file_id", "live_view"),
    [
        (None, FILE_ID, b"one\n"),
        ({}, FILE_ID, b"one\n"),
        ({FILE_ID: _record(b"other\n")}, FILE_ID, b"one\n"),
        ({FILE_ID: _record(b"one\n")}, "f" * 32, b"one\n"),
        ({FILE_ID: _record(b"one\n")}, FILE_ID, None),
    ],
    ids=["no-manifest", "not-recorded", "different-content", "other-file", "not-projectable"],
)
def test_a_file_not_in_the_restore_point_is_refused(records, file_id: str, live_view: bytes | None) -> None:
    # given
    backup = Backup.create("c" * 40, records=records)

    # when & then
    assert _in_restore_point(backup, file_id, live_view) is False


def test_a_backup_without_a_commit_holds_nothing() -> None:
    # when & then
    assert _in_restore_point(Backup.create(None), FILE_ID, b"one\n") is False
