# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for `changed_paths`, the pure half of the `diff` command."""

from __future__ import annotations

from syncintosh.core.diff import changed_paths
from syncintosh.core.tree import tree_from_value


def test_identical_trees_have_no_changes() -> None:
    # given
    tree = tree_from_value({"a": 1, "b": {"c": 2}})

    # when & then
    assert changed_paths(tree, tree) == ()


def test_only_the_differing_key_is_reported() -> None:
    # given
    left = tree_from_value({"a": 1, "b": {"c": 2}})
    right = tree_from_value({"a": 1, "b": {"c": 3}})

    # when
    changed = changed_paths(left, right)

    # then
    assert changed == (("b", "c"),)


def test_a_key_present_on_one_side_only_is_a_change() -> None:
    # given
    left = tree_from_value({"a": 1})
    right = tree_from_value({"a": 1, "b": 2})

    # when
    changed = changed_paths(left, right)

    # then
    assert changed == (("b",),)


def test_a_changed_json_kind_is_a_change() -> None:
    # given — `true` and `1` are different leaves, not the same value
    left = tree_from_value({"a": True})
    right = tree_from_value({"a": 1})

    # when
    changed = changed_paths(left, right)

    # then
    assert changed == (("a",),)


def test_an_array_is_compared_whole() -> None:
    # given
    left = tree_from_value({"items": [1, 2, 3]})
    right = tree_from_value({"items": [1, 2, 4]})

    # when
    changed = changed_paths(left, right)

    # then — the array contributes one path (its key), never per-element paths
    assert changed == (("items",),)
