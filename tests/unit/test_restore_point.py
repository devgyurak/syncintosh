# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the per-file restore point an overwrite must have before apply replaces a file."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import pytest

from syncintosh.core.manifest import ManifestRecord
from syncintosh.services.sync import Backup, RestorePoint, SyncReport
from syncintosh.services.sync.apply import _restore_point

FILE_ID = "0" * 32
CYCLE = "c" * 40
LEDGER = "d" * 40


def _record(content: bytes, file_id: str = FILE_ID) -> ManifestRecord:
    return ManifestRecord.create(
        file_id=file_id,
        path=".zshrc",
        preset="zsh",
        scope="common",
        format="text",
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _backup(commit: str | None, history: Mapping[str, Mapping[str, ManifestRecord]]) -> Backup:
    return Backup.create(commit, records_at=history.get)


@pytest.mark.parametrize(
    ("history", "view", "ledger_commit", "expected"),
    [
        ({CYCLE: {FILE_ID: _record(b"one\n")}}, b"one\n", None, CYCLE),
        ({CYCLE: {FILE_ID: _record(b"one\n")}, LEDGER: {FILE_ID: _record(b"one\n")}}, b"one\n", LEDGER, CYCLE),
        ({CYCLE: {FILE_ID: _record(b"new\n")}, LEDGER: {FILE_ID: _record(b"one\n")}}, b"one\n", LEDGER, LEDGER),
        ({CYCLE: {FILE_ID: _record(b"new\n")}, LEDGER: {FILE_ID: _record(b"old\n")}}, b"one\n", LEDGER, None),
        ({CYCLE: {FILE_ID: _record(b"new\n")}}, b"one\n", None, None),
        ({CYCLE: {}, LEDGER: {}}, b"one\n", LEDGER, None),
        ({CYCLE: {"f" * 32: _record(b"one\n", "f" * 32)}}, b"one\n", None, None),
        ({LEDGER: {FILE_ID: _record(b"one\n")}}, b"one\n", LEDGER, LEDGER),
        ({CYCLE: {FILE_ID: _record(b"one\n")}}, None, None, None),
    ],
    ids=[
        "cycle-commit-holds-it",
        "cycle-commit-wins-over-ledger",
        "published-but-never-applied",
        "in-no-commit",
        "no-ledger-commit",
        "not-recorded",
        "other-file",
        "cycle-commit-has-no-manifest",
        "not-projectable",
    ],
)
def test_the_restore_point_is_the_first_commit_holding_the_current_content(
    history, view: bytes | None, ledger_commit: str | None, expected: str | None
) -> None:
    # given
    backup = _backup(CYCLE, history)

    # when
    found = _restore_point(backup, FILE_ID, view, ledger_commit)

    # then
    assert found == expected


@pytest.mark.parametrize(
    "backup",
    [Backup.create(CYCLE), Backup.create(None, records_at=lambda _commit: {FILE_ID: _record(b"one\n")})],
    ids=["no-reader", "no-commit"],
)
def test_a_backup_that_cannot_read_a_commit_holds_nothing(backup: Backup) -> None:
    # when & then
    assert _restore_point(backup, FILE_ID, b"one\n", None) is None


@pytest.mark.parametrize(
    ("applied", "points", "backup_commit", "expected"),
    [
        ((), (), CYCLE, ()),
        ((".zshrc",), (), CYCLE, ((CYCLE, ()),)),
        ((".zshrc",), (), None, ()),
        ((".a", ".b"), (RestorePoint(".a", CYCLE), RestorePoint(".b", CYCLE)), CYCLE, ((CYCLE, ()),)),
        ((".a",), (RestorePoint(".a", LEDGER),), CYCLE, ((LEDGER, ()),)),
        (
            (".a", ".b", ".c"),
            (RestorePoint(".a", CYCLE), RestorePoint(".b", LEDGER), RestorePoint(".c", CYCLE)),
            CYCLE,
            ((CYCLE, (".a", ".c")), (LEDGER, (".b",))),
        ),
    ],
    ids=["nothing-applied", "created-only", "created-no-commit", "one-commit", "ledger-only", "mixed"],
)
def test_a_report_names_each_file_only_when_its_restore_commits_differ(
    applied, points, backup_commit, expected
) -> None:
    # given
    report = SyncReport(applied=applied, restore_points=points, backup_commit=backup_commit)

    # when & then
    assert report.restore_groups == expected
