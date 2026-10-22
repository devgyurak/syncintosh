# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure key-level merge engine over typed trees."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.merge import merge_tree
from syncintosh.core.tree import Absent, EmptyObject, Tree, tree_from_value

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
def test_a_conflict_always_reflects_a_real_disagreement(base, local, remote) -> None:
    # when
    result = merge_tree(base, local, remote)

    # then — a conflict is real: the leaf at the path differs, or the subtree beneath it does
    # (a deletion against a restructure conflicts with no leaf on either side)
    spurious = [
        conflict.path
        for conflict in result.conflicts
        if conflict.local == conflict.remote
        and _subtree_view(local, conflict.path) == _subtree_view(remote, conflict.path)
    ]
    assert spurious == []


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


def test_a_local_subtree_wins_over_a_remote_scalar_on_a_structural_conflict() -> None:
    # given — local changed a child of `a`; remote collapsed `a` to a scalar
    base = tree_from_value({"a": {"y": 1}})
    local = tree_from_value({"a": {"y": 2}})
    remote = tree_from_value({"a": 7})

    # when
    result = merge_tree(base, local, remote)

    # then — the conflict is reported and the local subtree is the candidate, not the scalar
    assert [conflict.path for conflict in result.conflicts] == [("a",)]
    assert result.merged == local


def test_a_local_scalar_wins_over_a_remote_subtree_on_a_structural_conflict() -> None:
    # given — local collapsed `a` to a scalar; remote changed a child of `a`
    base = tree_from_value({"a": {"y": 1}})
    local = tree_from_value({"a": 7})
    remote = tree_from_value({"a": {"y": 2}})

    # when
    result = merge_tree(base, local, remote)

    # then
    assert [conflict.path for conflict in result.conflicts] == [("a",)]
    assert result.merged == local


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


def test_a_one_sided_restructure_keeps_the_terminal_without_a_conflict() -> None:
    # given — local collapsed `a` to a scalar; remote left the subtree unchanged
    base = tree_from_value({"a": {"y": 1}})
    local = tree_from_value({"a": 9})
    remote = tree_from_value({"a": {"y": 1}})

    # when
    result = merge_tree(base, local, remote)

    # then — only local changed, so the scalar wins and nothing conflicts
    assert result.merged == local
    assert result.conflicts == ()


def test_a_local_added_child_survives_a_remote_collapse() -> None:
    # given — local added `y` under `a`; remote collapsed `a` to a scalar
    base = tree_from_value({"a": {"x": 1}})
    local = tree_from_value({"a": {"x": 1, "y": 2}})
    remote = tree_from_value({"a": 7})

    # when
    result = merge_tree(base, local, remote)

    # then — the conflict is reported and local's whole subtree is restored, `x` included
    assert [conflict.path for conflict in result.conflicts] == [("a",)]
    assert result.merged == local


def test_a_local_deletion_beats_a_remote_expansion_as_a_conflict() -> None:
    # given — local deleted `a` (a scalar); remote expanded it to an object
    base = tree_from_value({"a": 1})
    local = tree_from_value({})
    remote = tree_from_value({"a": {"x": 1}})

    # when
    result = merge_tree(base, local, remote)

    # then — not a silent remote win: it is a conflict and the local deletion is the candidate
    assert [conflict.path for conflict in result.conflicts] == [("a",)]
    assert result.merged == local


def test_a_local_deletion_beats_a_remote_collapse_as_a_conflict() -> None:
    # given — local deleted an object `a`; remote collapsed it to a scalar
    base = tree_from_value({"a": {"x": 1}})
    local = tree_from_value({})
    remote = tree_from_value({"a": 7})

    # when
    result = merge_tree(base, local, remote)

    # then
    assert [conflict.path for conflict in result.conflicts] == [("a",)]
    assert result.merged == local


def _covers(conflict_paths: list[tuple[str, ...]], path: tuple[str, ...]) -> bool:
    return any(path[: len(conflict)] == conflict for conflict in conflict_paths)


def _leaf_differs(base: Tree, local: Tree, remote: Tree, path: tuple[str, ...]) -> bool:
    values = (base.value(path), local.value(path), remote.value(path))
    return values[0] != values[1] and values[0] != values[2] and values[1] != values[2]


def _subtree_view(tree: Tree, path: tuple[str, ...]) -> dict[tuple[str, ...], object]:
    return {key: value for key, value in tree.entries.items() if len(key) > len(path) and key[: len(path)] == path}


def _shape(tree: Tree, path: tuple[str, ...]) -> str:
    """The kind of value a side has at `path`: a terminal, an object, or nothing.

    An `EmptyObject` counts as an **object**, not a terminal leaf. That is the engine's own rule:
    `merge._is_terminal` excludes it, and `_fold_structure` documents that an `EmptyObject` that
    gained a child "is not a conflict — the object is merely no longer empty". Treating `{}` as a
    third shape here would demand a conflict for `{}` against `{...}`, which the engine resolves
    on purpose (``test_an_empty_object_that_gained_a_child_is_not_a_conflict``).
    """
    node = tree.value(path)
    if isinstance(node, EmptyObject):
        return "subtree"
    if not isinstance(node, Absent):
        return "leaf"
    return "subtree" if any(other[: len(path)] == path and len(other) > len(path) for other in tree) else "absent"


def _shape_changed(base: Tree, local: Tree, remote: Tree, path: tuple[str, ...]) -> bool:
    shapes = (_shape(base, path), _shape(local, path), _shape(remote, path))
    return shapes[0] != shapes[1] and shapes[0] != shapes[2] and shapes[1] != shapes[2]


@given(base=_trees, local=_trees, remote=_trees)
def test_every_triple_leaf_difference_is_covered_by_a_conflict(base, local, remote) -> None:
    # when
    result = merge_tree(base, local, remote)
    paths = sorted({*base, *local, *remote})
    conflict_paths = [conflict.path for conflict in result.conflicts]

    # then — a path all three sides disagree on is a conflict there or at an ancestor
    uncovered = [
        path for path in paths if _leaf_differs(base, local, remote, path) and not _covers(conflict_paths, path)
    ]
    assert uncovered == []


@given(base=_trees, local=_trees, remote=_trees)
def test_a_both_sides_shape_change_is_covered_by_a_conflict(base, local, remote) -> None:
    # when
    result = merge_tree(base, local, remote)
    paths = sorted({*base, *local, *remote})
    conflict_paths = [conflict.path for conflict in result.conflicts]

    # then — both sides changed a path's shape differently ⇒ a conflict there or at an ancestor
    uncovered = [
        path for path in paths if _shape_changed(base, local, remote, path) and not _covers(conflict_paths, path)
    ]
    assert uncovered == []


def test_an_empty_object_that_gained_a_child_is_not_a_conflict() -> None:
    # given — one side writes `{}` at a key and the other fills the same key
    base = tree_from_value({"x": 1})
    local = tree_from_value({"x": 1, "a": {}})
    remote = tree_from_value({"x": 1, "a": {"b": 2}})

    # when
    result = merge_tree(base, local, remote)

    # then — `{}` and `{...}` are the same kind of value, so the populated shape simply wins
    # (docs/design/02-base-and-merge.md; `merge._fold_structure` documents this rule)
    assert result.conflicts == ()
    assert result.merged.to_value() == {"x": 1, "a": {"b": 2}}


def test_a_merge_that_deletes_every_key_is_an_empty_object_document() -> None:
    # given — each key is deleted on one side and left alone on the other, so the result is empty
    base = tree_from_value({"": None, "0": None})
    local = tree_from_value({"": None})
    remote = tree_from_value({"0": None})

    # when
    result = merge_tree(base, local, remote)

    # then — the root is an `EmptyObject`, the shape a parse of `{}` produces, so the rendered
    # result re-parses to the same tree instead of a bare `{}` that no parse can return
    assert result.merged == tree_from_value({})
    assert result.merged.entries == {(): EmptyObject()}
