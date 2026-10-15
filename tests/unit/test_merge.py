# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure key-level merge engine."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.merge import merge_keys


@given(data=st.dictionaries(st.text(), st.text()))
def test_merging_unchanged_input_is_idempotent(data) -> None:
    # when
    result = merge_keys(data, data, data)

    # then
    assert result.merged == data
    assert result.conflicts == ()


@given(base=st.dictionaries(st.text(), st.text()), local=st.dictionaries(st.text(), st.text()))
def test_one_sided_change_never_conflicts(base, local) -> None:
    # when
    result = merge_keys(base, local, base)

    # then
    assert result.conflicts == ()


@pytest.mark.parametrize(
    ("base", "local", "remote", "expected"),
    [
        # Only local changed.
        ({"a": "1"}, {"a": "2"}, {"a": "1"}, {"a": "2"}),
        # Only remote changed.
        ({"a": "1"}, {"a": "1"}, {"a": "2"}, {"a": "2"}),
        # A key added on local survives.
        ({}, {"a": "1"}, {}, {"a": "1"}),
        # A key deleted on one side is removed.
        ({"a": "1"}, {"a": "1"}, {"a": "1", "b": "2"}, {"a": "1", "b": "2"}),
    ],
)
def test_one_sided_changes_are_applied(base, local, remote, expected) -> None:
    # when
    result = merge_keys(base, local, remote)

    # then
    assert result.merged == expected
    assert result.conflicts == ()


def test_both_sides_changed_is_the_only_conflict() -> None:
    # given
    base = {"theme": "dark"}

    # when
    result = merge_keys(base, {"theme": "light"}, {"theme": "solarized"})

    # then
    assert [conflict.key for conflict in result.conflicts] == ["theme"]
    assert result.merged == {"theme": "light"}  # local wins by default


def test_both_sides_add_the_same_value_is_not_a_conflict() -> None:
    # when
    result = merge_keys({}, {"a": "1"}, {"a": "1"})

    # then
    assert result.conflicts == ()
    assert result.merged == {"a": "1"}


def test_both_sides_delete_a_key_is_not_a_conflict() -> None:
    # given
    base = {"a": "1", "b": "2"}

    # when
    result = merge_keys(base, {"a": "1"}, {"a": "1"})

    # then
    assert result.conflicts == ()
    assert result.merged == {"a": "1"}


def test_conflicts_are_sorted_by_key() -> None:
    # given
    base = {"b": "0", "a": "0"}

    # when
    result = merge_keys(base, {"a": "1", "b": "1"}, {"a": "2", "b": "2"})

    # then
    assert [conflict.key for conflict in result.conflicts] == ["a", "b"]
