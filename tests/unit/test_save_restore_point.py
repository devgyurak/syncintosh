# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for sealing an overwritten first-sight file into the local-only restore ref."""

from __future__ import annotations

import hashlib
from typing import cast

import pytest

from syncintosh.core.errors import SecretScanError
from syncintosh.core.manifest import ManifestRecord
from syncintosh.core.ports import Cipher, GitTransport, SecretScanner
from syncintosh.core.repo import object_path
from syncintosh.services.sync import Backup, Refusal, _save_restore_point

FILE_ID = "2" * 32
CYCLE = "c" * 40
RESTORE = "d" * 40


class _Recorder:
    """Stands in for the cipher, the scanner, and the transport, recording what each was asked."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.scanned: list[tuple[bytes, str, str]] = []
        self.sealed: list[tuple[bytes, str, str, int]] = []
        self.commits: list[tuple[str | None, dict[str, bytes]]] = []

    def scan(self, plaintext: bytes, *, subject: str, format: str) -> None:
        self.scanned.append((plaintext, subject, format))
        if self.refuse:
            raise SecretScanError("refused")

    def seal(self, plaintext: bytes, *, domain: str, file_id: str, key_id: int) -> bytes:
        self.sealed.append((plaintext, domain, file_id, key_id))
        return b"sealed:" + plaintext

    def commit_restore(self, cycle_commit: str | None, blobs, message: str) -> str:
        self.commits.append((cycle_commit, dict(blobs)))
        return RESTORE


def _record() -> ManifestRecord:
    return ManifestRecord.create(
        file_id=FILE_ID,
        path=".zshrc",
        preset="zsh",
        scope="common",
        format="text",
        sha256=hashlib.sha256(b"remote\n").hexdigest(),
    )


def _save(recorder: _Recorder, view: bytes | None, backup: Backup | None = None) -> str | Refusal:
    return _save_restore_point(
        transport=cast(GitTransport, recorder),
        cipher=cast(Cipher, recorder),
        scanner=cast(SecretScanner, recorder),
        backup=Backup.create(CYCLE) if backup is None else backup,
        key_generation=3,
        record=_record(),
        view=view,
    )


def test_the_bytes_are_scanned_then_sealed_under_the_object_domain_and_committed() -> None:
    # given
    recorder = _Recorder()

    # when
    saved = _save(recorder, b"local\n")

    # then — scan before seal, the AAD inputs are the object domain and the record's file ID, the cycle
    # commit is the parent, and only the sealed bytes reach the transport
    assert saved == RESTORE
    assert recorder.scanned == [(b"local\n", ".zshrc", "text")]
    assert recorder.sealed == [(b"local\n", "object", FILE_ID, 3)]
    assert recorder.commits == [(CYCLE, {object_path(FILE_ID): b"sealed:local\n"})]


def test_a_cycle_with_no_commit_still_saves_the_bytes() -> None:
    # given
    recorder = _Recorder()

    # when
    _save(recorder, b"local\n", Backup.create(None))

    # then
    assert [parent for parent, _ in recorder.commits] == [None]


@pytest.mark.parametrize(
    ("refuse", "view", "reason"),
    [(True, b"secret\n", "scan allow"), (False, None, "no commit")],
    ids=["scan-refused", "not-projectable"],
)
def test_a_file_that_cannot_be_saved_is_refused_and_nothing_is_sealed(refuse: bool, view, reason: str) -> None:
    # given
    recorder = _Recorder(refuse=refuse)

    # when
    saved = _save(recorder, view)

    # then
    assert isinstance(saved, Refusal)
    assert reason in saved.reason
    assert (recorder.sealed, recorder.commits) == ([], [])
