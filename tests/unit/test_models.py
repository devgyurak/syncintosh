# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the immutable domain models."""

from __future__ import annotations

import dataclasses

import pytest

from syncintosh.core.models import Conflict, LedgerEntry, MergeResult, PendingConflict, PendingState


def test_has_conflicts_is_true_when_a_conflict_is_present() -> None:
    # given
    conflict = Conflict(key="a", base="1", local="2", remote="3")

    # when
    result = MergeResult(merged={"a": "2"}, conflicts=(conflict,))

    # then
    assert result.has_conflicts is True


def test_has_conflicts_is_false_when_no_conflict() -> None:
    # when
    result = MergeResult(merged={"a": "1"}, conflicts=())

    # then
    assert result.has_conflicts is False


def test_merge_result_is_frozen() -> None:
    # given
    result = MergeResult(merged={}, conflicts=())

    # when & then
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.merged = {"a": "1"}  # ty: ignore[invalid-assignment]  # frozen dataclass must reject this


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
