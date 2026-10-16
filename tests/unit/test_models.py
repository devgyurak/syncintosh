# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the immutable domain models."""

from __future__ import annotations

import dataclasses

import pytest

from syncintosh.core.models import Conflict, LedgerEntry, PendingConflict, PendingState, TreeMerge
from syncintosh.core.tree import ABSENT, Scalar, tree_from_value


def test_has_conflicts_is_true_when_a_conflict_is_present() -> None:
    # given
    conflict = Conflict(path=("a",), base=ABSENT, local=Scalar("int", 2), remote=Scalar("int", 3))

    # when
    result = TreeMerge(merged=tree_from_value({"a": 2}), conflicts=(conflict,))

    # then
    assert result.has_conflicts is True


def test_has_conflicts_is_false_when_no_conflict() -> None:
    # when
    result = TreeMerge(merged=tree_from_value({"a": 1}), conflicts=())

    # then
    assert result.has_conflicts is False


def test_tree_merge_is_frozen() -> None:
    # given
    result = TreeMerge(merged=tree_from_value({}), conflicts=())

    # when & then
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.merged = tree_from_value({"a": 1})  # ty: ignore[invalid-assignment]  # frozen dataclass


def test_a_conflict_repr_hides_the_values() -> None:
    # given
    conflict = Conflict(
        path=("token",),
        base=ABSENT,
        local=Scalar("str", "hunter2"),
        remote=Scalar("str", "swordfish"),
    )

    # when
    text = repr(conflict)

    # then — the shape shows, the secrets do not
    assert "('token',)" in text
    assert "scalar" in text
    assert "hunter2" not in text
    assert "swordfish" not in text


def test_pending_state_has_conflicts_is_true_when_a_record_is_unresolved() -> None:
    # given
    conflict = PendingConflict(file_id="a" * 32, path=".zshrc", detail="both sides changed")

    # when
    state = PendingState(local_commit="1" * 40, remote_commit="2" * 40, base_commit=None, conflicts=(conflict,))

    # then
    assert state.has_conflicts is True


def test_pending_state_has_conflicts_is_false_when_only_the_commits_are_recorded() -> None:
    # when
    state = PendingState(local_commit="1" * 40, remote_commit="2" * 40, base_commit="3" * 40, conflicts=())

    # then
    assert state.has_conflicts is False


def test_ledger_entry_is_frozen_and_suppression_is_explicit() -> None:
    # when
    entry = LedgerEntry(sha256="a" * 64, commit="b" * 40, suppressed=True)

    # then — the suppression flag is stored, never inferred from the file's absence
    assert entry.suppressed is True


def test_pending_state_create_requires_both_commits() -> None:
    # given
    conflict = PendingConflict(file_id="a" * 32, path=".zshrc", detail="both sides changed")

    # when & then
    with pytest.raises(ValueError):
        PendingState.create(local_commit="", remote_commit="2" * 40, base_commit=None, conflicts=(conflict,))
    with pytest.raises(ValueError):
        PendingState.create(local_commit="1" * 40, remote_commit="", base_commit=None, conflicts=(conflict,))


def test_pending_state_create_requires_a_conflict() -> None:
    # when & then
    with pytest.raises(ValueError):
        PendingState.create(local_commit="1" * 40, remote_commit="2" * 40, base_commit=None, conflicts=())


def test_pending_state_create_builds_a_valid_state() -> None:
    # given
    conflict = PendingConflict(file_id="a" * 32, path=".zshrc", detail="both sides changed")

    # when
    state = PendingState.create(
        local_commit="1" * 40, remote_commit="2" * 40, base_commit="3" * 40, conflicts=(conflict,)
    )

    # then
    assert state.has_conflicts is True
    assert state.base_commit == "3" * 40
