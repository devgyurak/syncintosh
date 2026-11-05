# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the restore-ref plumbing: a scratch index, a private ref, and nothing else moved."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncintosh.core.ports import RESTORE_REF
from syncintosh.vcs.git import GitCLITransport

SEALED = b"\x01\x00\x00\x00\x01sealed"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[GitCLITransport, Path, str]:
    transport = GitCLITransport(tmp_path)
    transport.init()
    (tmp_path / "manifest.enc").write_bytes(b"manifest")
    commit = transport.commit((), "first", ("manifest.enc",))
    transport.update_ref("main", commit)
    return transport, tmp_path, commit


def test_a_restore_commit_holds_the_blob_and_moves_only_the_restore_ref(repo) -> None:
    # given
    transport, path, head = repo
    status_before = _git(path, "status", "--porcelain")

    # when
    restore = transport.commit_restore(head, {"objects/a.enc": SEALED}, "restore")

    # then — the ref names it, the blob is in its tree, and the branch, index, and working tree are as they were
    assert _git(path, "rev-parse", RESTORE_REF) == restore
    assert _git(path, "ls-tree", "-r", "--name-only", restore) == "objects/a.enc"
    assert _git(path, "rev-parse", "main") == head
    assert _git(path, "status", "--porcelain") == status_before
    assert (path / "objects").exists() is False


def test_a_second_restore_commit_chains_and_keeps_the_earlier_blob(repo) -> None:
    # given
    transport, path, head = repo
    first = transport.commit_restore(head, {"objects/a.enc": SEALED}, "one")

    # when
    second = transport.commit_restore(head, {"objects/b.enc": SEALED + b"2"}, "two")

    # then — parents are the cycle commit and the previous tip; the newest tree still lists the older blob
    assert _git(path, "rev-list", "--parents", "-n1", second).split()[1:] == [head, first]
    assert _git(path, "ls-tree", "-r", "--name-only", second).splitlines() == ["objects/a.enc", "objects/b.enc"]


def test_a_cycle_commit_equal_to_the_previous_tip_is_one_parent(repo) -> None:
    # given
    transport, path, head = repo
    first = transport.commit_restore(None, {"objects/a.enc": SEALED}, "one")

    # when
    second = transport.commit_restore(first, {"objects/a.enc": SEALED + b"2"}, "two")

    # then
    assert _git(path, "rev-list", "--parents", "-n1", second).split()[1:] == [first]
    assert _git(path, "rev-parse", "main") == head
