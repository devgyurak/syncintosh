# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure key-level merge engine over typed trees."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.merge import merge_tree
from syncintosh.core.tree import tree_from_value

# A recursive JSON strategy, then flatten each value into a Tree. Generating values (not paths)
# keeps every drawn tree a document a real parser could produce.
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(), children, max_size=3),
)
_trees = _json_values.map(tree_from_value)


@given(value=_trees)
def test_merging_unchanged_input_is_idempotent(value) -> None:
    # when
    result = merge_tree(value, value, value)

    # then
    assert result.merged == value
    assert list(result.merged) == list(value)
    assert result.conflicts == ()


@given(base=_trees, local=_trees)
def test_one_sided_change_never_conflicts(base, local) -> None:
    # when
    result = merge_tree(base, local, base)

    # then
    assert result.merged == local
    assert result.conflicts == ()


@given(base=_trees, local=_trees, remote=_trees)
def test_swapping_local_and_remote_keeps_the_conflicting_paths(base, local, remote) -> None:
    # when
    forward = merge_tree(base, local, remote)
    backward = merge_tree(base, remote, local)

    # then
    assert [conflict.path for conflict in forward.conflicts] == [conflict.path for conflict in backward.conflicts]


@given(base=_trees, local=_trees, remote=_trees)
def test_a_conflict_always_differs_between_the_two_sides(base, local, remote) -> None:
    # when
    result = merge_tree(base, local, remote)

    # then
    identical = [conflict.path for conflict in result.conflicts if conflict.local == conflict.remote]
    assert identical == []


@given(base=_trees, local=_trees, remote=_trees)
def test_the_merged_tree_has_no_leaf_above_a_child(base, local, remote) -> None:
    # when
    result = merge_tree(base, local, remote)

    # then
    paths = list(result.merged)
    stacked = [path for path in paths if any(other[: len(path)] == path and len(other) > len(path) for other in paths)]
    assert stacked == []


@given(base=_trees, local=_trees, remote=_trees, data=st.data())
def test_an_ignored_path_is_kept_from_local_and_never_conflicts(base, local, remote, data) -> None:
    # given
    paths = sorted({*base, *local, *remote})
    ignore = frozenset(data.draw(st.sets(st.sampled_from(paths)))) if paths else frozenset()

    # when
    result = merge_tree(base, local, remote, ignore=ignore)

    # then
    leaked = [
        conflict.path
        for conflict in result.conflicts
        if any(conflict.path[: len(prefix)] == prefix for prefix in ignore)
    ]
    kept = [path for path in ignore if result.merged.value(path) != local.value(path)]
    assert leaked == []
    assert kept == []


@pytest.mark.parametrize(
    ("base", "local", "remote", "expected", "conflicts"),
    [
        # A one-sided change wins.
        ({"k": 1}, {"k": 2}, {"k": 1}, {"k": 2}, []),
        # A key added on one side survives.
        ({}, {}, {"a": 1}, {"a": 1}, []),
        # A key deleted on one side is removed.
        ({"a": None}, {"a": None}, {}, {}, []),
        # A null added on one side is present, not absent.
        ({}, {"a": None}, {}, {"a": None}, []),
        # An empty object that gained a child is no longer empty.
        ({"o": {}}, {"o": {}}, {"o": {"a": 1}}, {"o": {"a": 1}}, []),
        # Both sides add the same nested key.
        ({}, {"x": {"y": 1}}, {"x": {"y": 1}}, {"x": {"y": 1}}, []),
        # Both sides change an array differently: one conflict, local wins.
        ({"a": [1]}, {"a": [1, 2]}, {"a": [1, 3]}, {"a": [1, 2]}, [("a",)]),
        # A scalar against a subtree is a single structural conflict, local wins.
        ({"a": {}}, {"a": 5}, {"a": {"b": 1}}, {"a": 5}, [("a",)]),
    ],
)
def test_one_sided_changes_and_single_conflicts(base, local, remote, expected, conflicts) -> None:
    # when
    result = merge_tree(tree_from_value(base), tree_from_value(local), tree_from_value(remote))

    # then
    assert result.merged == tree_from_value(expected)
    assert [conflict.path for conflict in result.conflicts] == conflicts


def test_a_bool_and_an_int_are_not_the_same_value() -> None:
    # given
    base = tree_from_value({"a": 0})

    # when
    result = merge_tree(base, tree_from_value({"a": True}), tree_from_value({"a": 1}))

    # then — true and 1 differ, so this is a both-sides change, not a no-op
    assert result.merged == tree_from_value({"a": True})
    assert [conflict.path for conflict in result.conflicts] == [("a",)]


def test_a_remote_added_key_keeps_the_remote_sibling_order() -> None:
    # given
    base = tree_from_value({})
    local = tree_from_value({"a": 1, "c": 3})
    remote = tree_from_value({"a": 1, "b": 2, "c": 3})

    # when
    result = merge_tree(base, local, remote)

    # then — `b` lands between `a` and `c`, where the remote document had it
    assert list(result.merged) == [("a",), ("b",), ("c",)]


def test_an_ignored_key_survives_a_restructure_of_its_parent() -> None:
    # given — remote collapses `a` to a scalar while local keeps an ignored child under it
    base = tree_from_value({"a": {"noise": 1}})
    local = tree_from_value({"a": {"noise": 1}})
    remote = tree_from_value({"a": 5})

    # when
    result = merge_tree(base, local, remote, ignore=frozenset({("a", "noise")}))

    # then — the ignored key keeps local's shape instead of being erased
    assert result.merged == tree_from_value({"a": {"noise": 1}})
    assert result.conflicts == ()


def test_nested_structural_conflicts_are_subsumed_into_the_shallowest() -> None:
    # given — local replaces `a` with a scalar; remote extends both `a.b` and `a.b.c`
    base = tree_from_value({"a": {"b": {"c": 1}}})
    local = tree_from_value({"a": 9})
    remote = tree_from_value({"a": {"b": {"c": 2, "d": 3}}})

    # when
    result = merge_tree(base, local, remote)

    # then
    assert result.merged == tree_from_value({"a": 9})
    assert [conflict.path for conflict in result.conflicts] == [("a",)]
