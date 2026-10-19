# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the typed tree the merge engine consumes."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.tree import ABSENT, Array, EmptyObject, Node, Object, Scalar, Tree, path_label, tree_from_value


def test_a_nested_object_flattens_to_key_paths() -> None:
    # when
    tree = tree_from_value({"a": {"b": 1, "c": {"d": "x"}}})

    # then
    assert tree.entries == {("a", "b"): Scalar("int", 1), ("a", "c", "d"): Scalar("str", "x")}


def test_a_value_round_trips_back_to_the_same_document() -> None:
    # given
    value = {"a": [1, True, None, "s"], "b": {"c": {}}, "d": []}

    # when
    tree = tree_from_value(value)

    # then
    assert tree.to_value() == value


def test_true_and_one_are_different_leaves() -> None:
    # when
    boolean = tree_from_value(True)
    integer = tree_from_value(1)

    # then
    assert boolean != integer
    assert (boolean.value(()) == Scalar("bool", True)) is True
    assert (integer.value(()) == Scalar("int", 1)) is True


def test_an_int_and_a_float_are_different_leaves() -> None:
    # when
    integer = tree_from_value(1)
    floating = tree_from_value(1.0)

    # then
    assert integer != floating


def test_an_empty_object_survives_flattening() -> None:
    # when
    tree = tree_from_value({"a": {}})

    # then
    assert tree.entries == {("a",): EmptyObject()}
    assert tree.to_value() == {"a": {}}


def test_the_empty_root_object_survives_flattening() -> None:
    # when
    tree = tree_from_value({})

    # then
    assert tree.entries == {(): EmptyObject()}
    assert tree.to_value() == {}


def test_an_array_is_a_single_leaf() -> None:
    # when
    tree = tree_from_value({"a": [1, 2]})

    # then
    assert tree.entries == {("a",): Array((Scalar("int", 1), Scalar("int", 2)))}
    assert tree.to_value() == {"a": [1, 2]}


def test_an_object_inside_an_array_is_kept_whole() -> None:
    # when
    tree = tree_from_value({"a": [{"b": 1}]})

    # then
    assert tree.entries == {("a",): Array((Object((("b", Scalar("int", 1)),)),))}
    assert tree.to_value() == {"a": [{"b": 1}]}


def test_a_missing_key_is_absent_not_null() -> None:
    # given
    tree = tree_from_value({"a": None})

    # when & then
    assert tree.value(("a",)) == Scalar("null", None)
    assert tree.value(("missing",)) is ABSENT


def test_a_top_level_scalar_is_the_root_leaf() -> None:
    # when
    tree = tree_from_value("hello")

    # then
    assert tree.entries == {(): Scalar("str", "hello")}
    assert tree.to_value() == "hello"


def test_a_top_level_array_is_the_root_leaf() -> None:
    # when
    tree = tree_from_value([1, 2])

    # then
    assert tree.entries == {(): Array((Scalar("int", 1), Scalar("int", 2)))}
    assert tree.to_value() == [1, 2]


def test_iteration_follows_document_order() -> None:
    # when
    tree = tree_from_value({"b": 1, "a": 2})

    # then
    assert list(tree) == [("b",), ("a",)]


def test_a_tree_reports_its_size() -> None:
    # when
    tree = tree_from_value({"a": 1, "b": 2})

    # then
    assert len(tree) == 2


def test_a_frozen_tree_rejects_mutation() -> None:
    # given
    tree = tree_from_value({"a": 1})

    # when & then
    with pytest.raises(TypeError):
        tree.entries[("b",)] = Scalar("int", 2)  # ty: ignore[invalid-assignment]  # a proxy is read-only


def test_a_non_json_scalar_is_rejected() -> None:
    # when & then
    with pytest.raises(TypeError):
        tree_from_value({"a": object()})


@given(
    value=st.recursive(
        st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
        lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(), children, max_size=3),
    )
)
def test_flatten_then_rebuild_round_trips(value) -> None:
    # when
    rebuilt = tree_from_value(value).to_value()

    # then
    assert tree_from_value(rebuilt) == tree_from_value(value)


def test_a_tree_is_hashable() -> None:
    # given
    tree = tree_from_value({"a": 1, "b": [1, 2]})

    # when
    bucket = {tree, tree_from_value({"b": [1, 2], "a": 1})}

    # then
    assert len(bucket) == 1


def test_create_freezes_the_mapping() -> None:
    # given
    entries: dict[tuple[str, ...], Node] = {("a",): Scalar("int", 1)}

    # when
    tree = Tree.create(entries)
    entries[("b",)] = Scalar("int", 2)

    # then
    assert list(tree) == [("a",)]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ((), "(document)"),  # the whole document
        (("editor", "fontSize"), "editor.fontSize"),  # nested keys read as a dotted setting
        (("editor.fontSize",), '"editor.fontSize"'),  # one key whose *name* holds a dot
        (("a", ""), 'a.""'),  # an unnamed step keeps its place instead of trailing a dot
        (("",), '""'),  # and is not the same as the document
        (("a.b", "c"), '"a.b".c'),
        (('"a', 'b"'), '"\\"a"."b\\""'),  # a quoted step cannot be read as another step's text
        (("(document)",), '"(document)"'),  # and the document's own word is not a key's label
        (("a,b",), '"a,b"'),  # labels are joined with `, `, so a bare comma would read as two
        (["éditeur"], "éditeur"),  # a non-ASCII key stays readable
    ],
)
def test_a_path_labels_each_step_so_two_paths_cannot_print_alike(path, expected) -> None:
    # when
    label = path_label(path)

    # then
    assert label == expected


@pytest.mark.parametrize(
    "pair",
    [
        ((("a.b",), ("a", "b"))),  # a flat dotted key against the nested pair
        ((("(document)",), ())),  # a key named for the document against the document
        ((('"a', 'b"'), ("a.b",))),  # a step that would spell itself like a quoted one
        ((("",), ())),  # an unnamed key against the document
    ],
)
def test_two_different_paths_never_share_a_label(pair) -> None:
    # when
    first, second = (path_label(path) for path in pair)

    # then
    assert first != second
