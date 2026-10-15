# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the immutable domain models."""

from __future__ import annotations

import dataclasses

import pytest

from syncintosh.core.models import Conflict, MergeResult


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
