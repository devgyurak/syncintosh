# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pending state on disk.

The unresolved sides are plaintext, so they must live here and nowhere else on disk
(``AGENTS.md`` §3.3) and must never reach a log — hence the masked `repr` assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncintosh.core.errors import PendingCorrupt
from syncintosh.core.models import PendingConflict, PendingState
from syncintosh.system.pending import DirectoryPendingStore

FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"


def _state() -> PendingState:
    conflict = PendingConflict(
        file_id=FILE_ID,
        path=".zshrc",
        detail="the same region changed on both sides",
        base=b"base\n",
        local=b"local\n",
        remote=b"remote\n",
    )
    return PendingState(local_commit="1" * 40, remote_commit="2" * 40, base_commit="3" * 40, conflicts=(conflict,))


def test_a_directory_with_no_state_is_not_pending(tmp_path: Path) -> None:
    # given
    store = DirectoryPendingStore(tmp_path / "pending")

    # when & then
    assert store.is_pending() is False
    assert store.load() is None


def test_a_recorded_state_round_trips_with_its_plaintext(tmp_path: Path) -> None:
    # given
    store = DirectoryPendingStore(tmp_path / "pending")

    # when
    store.record(_state())

    # then
    assert store.is_pending() is True
    loaded = store.load()
    assert loaded == _state()


def test_clear_removes_the_pending_directory(tmp_path: Path) -> None:
    # given
    store = DirectoryPendingStore(tmp_path / "pending")
    store.record(_state())

    # when
    store.clear()

    # then
    assert store.is_pending() is False


def test_a_conflict_repr_masks_the_plaintext() -> None:
    # given
    conflict = PendingConflict(file_id=FILE_ID, path=".zshrc", detail="x", local=b"secret-token")

    # when
    rendered = repr(conflict)

    # then
    assert "secret-token" not in rendered
    assert "bytes>" in rendered


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"local_commit":"1"}',
        '{"conflicts":[7]}',
        '{"conflicts":[{"file_id":"x"}]}',
        '{"conflicts":[{"file_id":"../evil","path":".zshrc","detail":"x"}]}',
    ],
)
def test_a_malformed_pending_state_is_a_domain_error(tmp_path: Path, payload: str) -> None:
    # given
    root = tmp_path / "pending"
    root.mkdir()
    (root / "state.json").write_text(payload, encoding="utf-8")
    store = DirectoryPendingStore(root)

    # when & then
    with pytest.raises(PendingCorrupt):
        store.load()


def test_a_pending_state_without_a_commit_is_refused(tmp_path: Path) -> None:
    # given — a hand-edited state that names no local commit
    root = tmp_path / "pending"
    root.mkdir()
    (root / "state.json").write_text(
        f'{{"remote_commit":"{"2" * 40}","conflicts":[{{"file_id":"{FILE_ID}","path":".zshrc","detail":"x"}}]}}',
        encoding="utf-8",
    )

    # when & then
    with pytest.raises(PendingCorrupt):
        DirectoryPendingStore(root).load()


def test_clear_on_a_missing_directory_is_a_noop(tmp_path: Path) -> None:
    # given
    store = DirectoryPendingStore(tmp_path / "pending")

    # when
    result = store.clear()

    # then
    assert result is None
