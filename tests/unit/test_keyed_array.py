# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and property tests for the element-level merge of identified arrays (keybindings)."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from syncintosh.core.diff import changed_addresses
from syncintosh.core.keyed_array import (
    ArrayRule,
    accepts_group,
    group_leaves,
    merge_keyed_array,
)
from syncintosh.core.merge import merge_tree
from syncintosh.core.tree import ABSENT, Array, Node, Tree, element_label, tree_from_value

# The rule a VS Code `keybindings.json` declares: an element is its key, command and when; the key and
# the command must be present; the group is the (key, when) pair a binding is bound under.
RULE = ArrayRule.create(identity=("key", "command", "when"), required=("key", "command"), group=("key", "when"))


def _array(elements: list[object]) -> Array:
    """A Python list of bindings as the array leaf the merge reads."""
    leaf = tree_from_value(elements).value(())
    assert isinstance(leaf, Array)
    return leaf


def _plain(node: Node) -> Any:
    """An array leaf back as plain JSON, for comparing a merged result with an expected list."""
    return Tree.create({(): node}).to_value()


def _binding(key: str, command: str, when: str | None = None, **extra: object) -> dict[str, object]:
    """A binding; a `when` of None is an absent field, which is not the same as `""`."""
    binding: dict[str, object] = {"key": key, "command": command, **extra}
    if when is not None:
        binding["when"] = when
    return binding


# --- the group rule: one side wins, same change is one, different changes on a group conflict ------------


def test_a_binding_added_on_one_side_is_kept_alongside_the_other_sides_binding() -> None:
    # given
    base = _array([_binding("ctrl+a", "a")])
    local = _array([_binding("ctrl+a", "a"), _binding("ctrl+b", "b")])
    remote = _array([_binding("ctrl+a", "a"), _binding("ctrl+c", "c")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert _plain(result.merged) == [_binding("ctrl+a", "a"), _binding("ctrl+c", "c"), _binding("ctrl+b", "b")]
    assert result.conflicts == ()


def test_a_binding_added_on_the_same_key_with_a_different_when_is_kept_as_well() -> None:
    # given
    base = _array([_binding("tab", "indent")])
    local = _array([_binding("tab", "indent"), _binding("tab", "suggest", when="suggestWidgetVisible")])
    remote = _array([_binding("tab", "indent"), _binding("tab", "snippet", when="inSnippetMode")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert result.conflicts == ()
    assert len(_plain(result.merged)) == 3


def test_two_different_commands_on_the_same_key_and_when_are_a_conflict_on_that_group() -> None:
    # given
    base = _array([_binding("ctrl+k", "old")])
    local = _array([_binding("ctrl+k", "local")])
    remote = _array([_binding("ctrl+k", "remote")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert [conflict.element for conflict in result.conflicts] == [(("key", "ctrl+k"), ("when", None))]
    assert _plain(result.merged) == [_binding("ctrl+k", "local")]


def test_the_same_binding_added_on_both_sides_is_one_binding() -> None:
    # given
    base = _array([])
    local = _array([_binding("ctrl+d", "d")])
    remote = _array([_binding("ctrl+d", "d")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert _plain(result.merged) == [_binding("ctrl+d", "d")]
    assert result.conflicts == ()


def test_a_removal_on_one_side_with_the_other_side_unchanged_removes_the_binding() -> None:
    # given
    base = _array([_binding("ctrl+a", "a"), _binding("ctrl+b", "b")])
    local = _array([_binding("ctrl+b", "b")])
    remote = _array([_binding("ctrl+a", "a"), _binding("ctrl+b", "b"), _binding("ctrl+c", "c")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert _plain(result.merged) == [_binding("ctrl+b", "b"), _binding("ctrl+c", "c")]
    assert result.conflicts == ()


def test_a_removal_that_overlaps_an_edit_of_the_same_group_is_a_conflict() -> None:
    # given
    base = _array([_binding("ctrl+a", "a")])
    local = _array([])
    remote = _array([_binding("ctrl+a", "a", args=[1])])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert [conflict.element for conflict in result.conflicts] == [(("key", "ctrl+a"), ("when", None))]


def test_a_change_to_one_binding_and_a_removal_of_another_in_the_same_group_is_a_conflict() -> None:
    # given — the group (ctrl+a, no when) holds two bindings; local edits one, remote removes the other
    base = _array([_binding("ctrl+a", "a"), _binding("ctrl+a", "b")])
    local = _array([_binding("ctrl+a", "a", args=[1]), _binding("ctrl+a", "b")])
    remote = _array([_binding("ctrl+a", "a")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert len(result.conflicts) == 1


def test_a_missing_when_and_an_empty_when_name_different_groups() -> None:
    # given — `when` missing and `"when": ""` are different values, so they never share a group
    base = _array([])
    local = _array([_binding("ctrl+e", "missing")])
    remote = _array([_binding("ctrl+e", "empty", when="")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert result.conflicts == ()
    assert len(_plain(result.merged)) == 2


def test_keys_that_differ_only_in_case_are_different_groups_and_are_not_normalized() -> None:
    # given — VS Code's key parser lowercases; this merge pins the exact-string identity instead
    base = _array([])
    local = _array([_binding("Ctrl+A", "one")])
    remote = _array([_binding("ctrl+a", "two")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert result.conflicts == ()
    assert len(_plain(result.merged)) == 2


# --- order: local order kept, remote additions after their preceding remote sibling ---------------------


def test_a_remote_addition_lands_after_its_preceding_remote_sibling() -> None:
    # given
    base = _array([_binding("a", "1"), _binding("b", "2")])
    local = _array([_binding("a", "1"), _binding("b", "2"), _binding("d", "4")])
    remote = _array([_binding("a", "1"), _binding("c", "3"), _binding("b", "2")])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert _plain(result.merged) == [
        _binding("a", "1"),
        _binding("c", "3"),
        _binding("b", "2"),
        _binding("d", "4"),
    ]


def test_a_reorder_on_one_side_alone_is_adopted() -> None:
    # given
    base = _array([_binding("a", "1"), _binding("b", "2"), _binding("c", "3")])
    local = _array([_binding("c", "3"), _binding("a", "1"), _binding("b", "2")])
    remote = base

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then
    assert result is not None
    assert _plain(result.merged) == [_binding("c", "3"), _binding("a", "1"), _binding("b", "2")]


def test_both_sides_reordering_shared_bindings_differently_falls_back() -> None:
    # given — both sides moved away from the base order, and to different ones
    base = _array([_binding("a", "1"), _binding("b", "2"), _binding("c", "3")])
    local = _array([_binding("b", "2"), _binding("a", "1"), _binding("c", "3")])
    remote = _array([_binding("a", "1"), _binding("c", "3"), _binding("b", "2")])

    # when & then
    assert merge_keyed_array(base, local, remote, RULE) is None


def test_a_remote_addition_alongside_a_local_reorder_is_not_an_order_conflict() -> None:
    # given — only local reordered the shared bindings; remote merely added one
    base = _array([_binding("a", "1"), _binding("b", "2")])
    local = _array([_binding("b", "2"), _binding("a", "1")])
    remote = _array([_binding("a", "1"), _binding("b", "2"), _binding("c", "3")])

    # when & then
    assert merge_keyed_array(base, local, remote, RULE) is not None


# --- fallback: the whole-array compare decides when an element cannot be identified -------------------


@pytest.mark.parametrize(
    "element",
    [
        pytest.param("not-an-object", id="scalar-element"),
        pytest.param({"command": "a"}, id="missing-key"),
        pytest.param({"key": "a"}, id="missing-command"),
        pytest.param({"key": 1, "command": "a"}, id="non-string-key"),
        pytest.param({"key": "a", "command": "a", "when": None}, id="null-when"),
        pytest.param({"key": "a", "command": ["a"]}, id="array-command"),
    ],
)
def test_an_unidentifiable_element_on_any_side_falls_back(element: object) -> None:
    # given
    base = _array([_binding("a", "1")])
    local = _array([_binding("a", "1"), _binding("b", "2")])
    remote = _array([_binding("a", "1"), element])

    # when & then
    assert merge_keyed_array(base, local, remote, RULE) is None


def test_a_command_removal_without_a_key_falls_back() -> None:
    # given — VS Code accepts `{"command": "-foo"}`, but this rule has no identity for it
    base = _array([_binding("a", "1")])
    local = _array([_binding("a", "1"), {"command": "-foo"}])
    remote = _array([_binding("a", "1"), _binding("b", "2")])

    # when & then
    assert merge_keyed_array(base, local, remote, RULE) is None


def test_two_elements_with_one_identity_fall_back() -> None:
    # given
    base = _array([_binding("a", "1")])
    local = _array([_binding("a", "1", args=[1]), _binding("a", "1", args=[2])])
    remote = _array([_binding("a", "1")])

    # when & then
    assert merge_keyed_array(base, local, remote, RULE) is None


def test_a_side_that_is_not_an_array_falls_back() -> None:
    # given
    base = _array([_binding("a", "1")])
    local = _array([_binding("a", "1"), _binding("b", "2")])

    # when & then
    assert merge_keyed_array(base, local, tree_from_value("scalar").value(()), RULE) is None


def test_an_array_added_on_both_sides_has_no_base_to_group_against_and_falls_back() -> None:
    # given
    local = _array([_binding("a", "1")])
    remote = _array([_binding("b", "2")])

    # when & then
    assert merge_keyed_array(ABSENT, local, remote, RULE) is None


def test_a_rule_refuses_a_group_field_that_is_not_part_of_the_identity() -> None:
    # given
    identity = ("key", "command")

    # when & then
    with pytest.raises(ValueError, match="identity"):
        ArrayRule.create(identity=identity, required=("key",), group=("key", "when"))


# --- merge_tree: a keyed path records group conflicts and keeps the rest of the array --------------------


def test_merge_tree_keeps_an_unrelated_binding_when_another_group_conflicts() -> None:
    # given
    base = tree_from_value({"keys": [_binding("a", "old")]})
    local = tree_from_value({"keys": [_binding("a", "local-a"), _binding("b", "local-b")]})
    remote = tree_from_value({"keys": [_binding("a", "remote-a"), _binding("c", "remote-c")]})

    # when
    result = merge_tree(base, local, remote, keyed={("keys",): RULE})

    # then
    assert [(conflict.path, conflict.element) for conflict in result.conflicts] == [
        (("keys",), (("key", "a"), ("when", None)))
    ]
    # the conflicting group keeps local's binding; the remote addition has no surviving remote sibling
    # to follow (its predecessor was a different command, so a different identity), and is appended
    assert result.merged.to_value() == {
        "keys": [_binding("a", "local-a"), _binding("b", "local-b"), _binding("c", "remote-c")],
    }


def test_merge_tree_records_a_whole_array_conflict_when_the_rule_falls_back() -> None:
    # given
    base = tree_from_value({"keys": [_binding("a", "old")]})
    local = tree_from_value({"keys": [_binding("a", "local")]})
    remote = tree_from_value({"keys": [_binding("a", "remote"), "not-an-object"]})

    # when
    result = merge_tree(base, local, remote, keyed={("keys",): RULE})

    # then
    assert [(conflict.path, conflict.element) for conflict in result.conflicts] == [(("keys",), None)]


def test_merge_tree_without_a_rule_keeps_the_whole_array_conflict() -> None:
    # given
    base = tree_from_value({"keys": [_binding("a", "old")]})
    local = tree_from_value({"keys": [_binding("a", "old"), _binding("b", "2")]})
    remote = tree_from_value({"keys": [_binding("a", "old"), _binding("c", "3")]})

    # when
    result = merge_tree(base, local, remote)

    # then
    assert [(conflict.path, conflict.element) for conflict in result.conflicts] == [(("keys",), None)]


# --- properties ---------------------------------------------------------------------------------------------

_KEYS = st.sampled_from(["a", "b", "c"])
_COMMANDS = st.sampled_from(["x", "y", "z"])
_WHENS = st.sampled_from([None, "", "p"])
_ARGS = st.integers(min_value=0, max_value=2)


def _identity_of(binding: dict[str, object]) -> tuple[object, ...]:
    return (binding["key"], binding["command"], binding.get("when"))


_binding_strategy = st.builds(
    lambda key, command, when, args: _binding(key, command, when, args=args),
    _KEYS,
    _COMMANDS,
    _WHENS,
    _ARGS,
)
# Bindings are unique by identity, which is what a real keybindings.json holds; a duplicate falls back.
_bindings = st.lists(_binding_strategy, max_size=5, unique_by=_identity_of)


def _groups(elements: list[dict[str, object]]) -> dict[tuple[object, ...], list[dict[str, object]]]:
    """The elements of each (key, when) group, the grouping the rule applies."""
    grouped: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for element in elements:
        grouped.setdefault((element["key"], element.get("when")), []).append(element)
    return grouped


@given(elements=_bindings)
def test_merging_unchanged_bindings_is_idempotent(elements) -> None:
    # given
    array = _array(elements)

    # when
    result = merge_keyed_array(array, array, array, RULE)

    # then
    assert result is not None
    assert result.merged == array
    assert result.conflicts == ()


@given(base=_bindings, local=_bindings)
def test_a_one_sided_change_always_wins(base, local) -> None:
    # given
    base_array, local_array = _array(base), _array(local)

    # when
    result = merge_keyed_array(base_array, local_array, base_array, RULE)

    # then
    assume(result is not None)
    assert result is not None
    assert _plain(result.merged) == _plain(local_array)
    assert result.conflicts == ()


@given(base=_bindings, remote=_bindings)
def test_a_one_sided_change_on_the_remote_always_wins(base, remote) -> None:
    # given
    base_array, remote_array = _array(base), _array(remote)

    # when
    result = merge_keyed_array(base_array, base_array, remote_array, RULE)

    # then
    assume(result is not None)
    assert result is not None
    assert _plain(result.merged) == _plain(remote_array)
    assert result.conflicts == ()


@given(base=_bindings, local=_bindings, remote=_bindings)
def test_a_conflict_is_exactly_a_group_both_sides_changed_to_different_contents(base, local, remote) -> None:
    # given
    base_array, local_array, remote_array = _array(base), _array(local), _array(remote)

    # when
    result = merge_keyed_array(base_array, local_array, remote_array, RULE)

    # then
    assume(result is not None)
    assert result is not None
    base_groups, local_groups, remote_groups = _groups(base), _groups(local), _groups(remote)
    expected = {
        (("key", key), ("when", when))
        for key, when in set(base_groups) | set(local_groups) | set(remote_groups)
        if _group_of(local_groups, key, when) != _group_of(remote_groups, key, when)
        and _group_of(local_groups, key, when) != _group_of(base_groups, key, when)
        and _group_of(remote_groups, key, when) != _group_of(base_groups, key, when)
    }
    assert {conflict.element for conflict in result.conflicts} == expected


@given(base=_bindings, local=_bindings, remote=_bindings)
def test_swapping_the_sides_keeps_the_conflicting_groups(base, local, remote) -> None:
    # given
    base_array, local_array, remote_array = _array(base), _array(local), _array(remote)

    # when
    forward = merge_keyed_array(base_array, local_array, remote_array, RULE)
    backward = merge_keyed_array(base_array, remote_array, local_array, RULE)

    # then
    assume(forward is not None and backward is not None)
    assert forward is not None and backward is not None
    assert {conflict.element for conflict in forward.conflicts} == {conflict.element for conflict in backward.conflicts}


@given(base=_bindings, local=_bindings)
def test_the_same_change_on_both_sides_is_taken_once_without_a_conflict(base, local) -> None:
    # given
    base_array, local_array = _array(base), _array(local)

    # when
    result = merge_keyed_array(base_array, local_array, local_array, RULE)

    # then
    assume(result is not None)
    assert result is not None
    assert _plain(result.merged) == _plain(local_array)
    assert result.conflicts == ()


def _group_of(groups: dict[tuple[object, ...], list[dict[str, object]]], key: object, when: object) -> list[object]:
    """A group's elements as plain values, or an empty list, for comparing one group across sides."""
    return [dict(element) for element in groups.get((key, when), [])]


# --- picks: a conflicting group settled by one side, or by a proposed array of its own bindings ------------


def test_a_side_pick_settles_a_conflicting_group_to_that_side() -> None:
    # given
    base = _array([_binding("ctrl+k", "old")])
    local = _array([_binding("ctrl+k", "local")])
    remote = _array([_binding("ctrl+k", "remote")])
    group = (("key", "ctrl+k"), ("when", None))

    # when
    result = merge_keyed_array(base, local, remote, RULE, picks={group: "remote"})

    # then
    assert result is not None
    assert result.conflicts == ()
    assert _plain(result.merged) == [_binding("ctrl+k", "remote")]


def test_a_proposed_array_of_the_same_group_settles_that_group() -> None:
    # given
    base = _array([_binding("ctrl+k", "old")])
    local = _array([_binding("ctrl+k", "local")])
    remote = _array([_binding("ctrl+k", "remote")])
    group = (("key", "ctrl+k"), ("when", None))
    proposal = _array([_binding("ctrl+k", "both")])

    # when
    result = merge_keyed_array(base, local, remote, RULE, picks={group: proposal})

    # then
    assert result is not None
    assert result.conflicts == ()
    assert _plain(result.merged) == [_binding("ctrl+k", "both")]


def test_a_proposal_naming_another_group_stays_a_conflict() -> None:
    # given — the proposal tries to move a binding into a group other than the one being settled
    base = _array([_binding("ctrl+k", "old")])
    local = _array([_binding("ctrl+k", "local")])
    remote = _array([_binding("ctrl+k", "remote")])
    group = (("key", "ctrl+k"), ("when", None))
    proposal = _array([_binding("ctrl+k", "ok"), _binding("ctrl+x", "moved")])

    # when
    result = merge_keyed_array(base, local, remote, RULE, picks={group: proposal})

    # then
    assert result is not None
    assert [conflict.element for conflict in result.conflicts] == [group]


def test_accepts_group_refuses_a_non_array_and_admits_an_empty_one() -> None:
    # given
    group = (("key", "ctrl+k"), ("when", None))

    # when & then
    assert accepts_group(_array([]), RULE, group) is True
    assert accepts_group(_array([_binding("ctrl+k", "a")]), RULE, group) is True
    assert accepts_group(_array([_binding("ctrl+x", "a")]), RULE, group) is False
    assert accepts_group(tree_from_value("scalar").value(()), RULE, group) is False


def test_changed_addresses_names_only_the_binding_that_changed() -> None:
    # given
    left = tree_from_value({"keys": [_binding("a", "1"), _binding("b", "2")]})
    right = tree_from_value({"keys": [_binding("a", "1"), _binding("b", "9")]})

    # when
    addresses = changed_addresses(left, right, {("keys",): RULE})

    # then
    assert addresses == ((("keys",), (("key", "b"), ("when", None))),)


def test_a_picked_group_keeps_the_order_of_the_side_it_was_picked_from() -> None:
    # given — local edits the first binding's args; remote edits the same group and moves it after the other
    base = _array([_binding("ctrl+k", "a"), _binding("ctrl+k", "b")])
    local = _array([_binding("ctrl+k", "a", args=1), _binding("ctrl+k", "b")])
    remote = _array([_binding("ctrl+k", "b", args=2), _binding("ctrl+k", "a")])
    group = (("key", "ctrl+k"), ("when", None))

    # when
    picked_local = merge_keyed_array(base, local, remote, RULE, picks={group: "local"})

    # then — local's group, in local's order, without a whole-array conflict or a stuck resolve
    assert picked_local is not None
    assert picked_local.conflicts == ()
    assert _plain(picked_local.merged) == [_binding("ctrl+k", "a", args=1), _binding("ctrl+k", "b")]


def test_a_pick_of_the_side_whose_order_is_kept_settles_without_a_conflict() -> None:
    # given
    base = _array([_binding("ctrl+k", "a"), _binding("ctrl+k", "b")])
    local = _array([_binding("ctrl+k", "a", args=1), _binding("ctrl+k", "b")])
    remote = _array([_binding("ctrl+k", "b", args=2), _binding("ctrl+k", "a")])
    group = (("key", "ctrl+k"), ("when", None))

    # when
    picked_remote = merge_keyed_array(base, local, remote, RULE, picks={group: "remote"})

    # then — remote's group, in remote's order
    assert picked_remote is not None
    assert _plain(picked_remote.merged) == [_binding("ctrl+k", "b", args=2), _binding("ctrl+k", "a")]


def test_a_proposed_binding_takes_the_place_of_the_group_it_replaces() -> None:
    # given — the group for `ctrl+a` sits between `ctrl+b` and `ctrl+x`; the model proposes a new binding for it
    base = _array([_binding("ctrl+b", "c"), _binding("ctrl+a", "a"), _binding("ctrl+x", "b")])
    local = _array([_binding("ctrl+b", "c"), _binding("ctrl+a", "a", args=1), _binding("ctrl+x", "b")])
    remote = _array([_binding("ctrl+b", "c"), _binding("ctrl+a", "a", args=2), _binding("ctrl+x", "b")])
    group = (("key", "ctrl+a"), ("when", None))

    # when
    result = merge_keyed_array(base, local, remote, RULE, picks={group: _array([_binding("ctrl+a", "p")])})

    # then — it stays between its neighbours; it is not appended to the end
    assert result is not None
    assert [binding["command"] for binding in _plain(result.merged)] == ["c", "p", "b"]


def test_a_proposal_keeps_the_key_order_of_the_binding_it_replaces() -> None:
    # given — a model that returns the keys sorted; the user's own binding has `key` first
    base = _array([_binding("ctrl+a", "a", args=1)])
    local = _array([_binding("ctrl+a", "a2", args=1)])
    remote = _array([_binding("ctrl+a", "a3", args=1)])
    group = (("key", "ctrl+a"), ("when", None))
    sorted_proposal = _array([{"args": 1, "command": "p", "key": "ctrl+a"}])

    # when
    result = merge_keyed_array(base, local, remote, RULE, picks={group: sorted_proposal})

    # then
    assert result is not None
    assert list(_plain(result.merged)[0]) == ["key", "command", "args"]


def test_a_when_clause_is_labelled_by_a_digest_so_its_text_is_not_printed() -> None:
    # given
    element = (("key", "ctrl+k"), ("when", "resourcePath =~ /corp\\.internal/"))

    # when
    label = element_label(element)

    # then
    assert "corp" not in label
    assert label.startswith('[key="ctrl+k", when=sha256:')


def test_group_leaves_indexes_each_group_once_with_its_own_bindings() -> None:
    # given
    array = _array([_binding("a", "1"), _binding("a", "2"), _binding("b", "3", when="p")])
    group = (("key", "a"), ("when", None))

    # when
    leaves = group_leaves(array, RULE)

    # then
    assert leaves is not None
    assert leaves[group] == _array([_binding("a", "1"), _binding("a", "2")])
    assert set(leaves) == {((("key", "a"), ("when", None))), ((("key", "b"), ("when", "p")))}


def test_a_proposal_for_a_group_the_local_side_removed_stays_between_its_neighbours() -> None:
    # given — local removed the `ctrl+k` binding; remote changed it; the model proposes a replacement for it
    base = _array([_binding("ctrl+x", "x"), _binding("ctrl+k", "k"), _binding("ctrl+y", "y")])
    local = _array([_binding("ctrl+x", "x"), _binding("ctrl+y", "y")])
    remote = _array([_binding("ctrl+x", "x"), _binding("ctrl+k", "k", args=1), _binding("ctrl+y", "y")])
    group = (("key", "ctrl+k"), ("when", None))

    # when
    result = merge_keyed_array(base, local, remote, RULE, picks={group: _array([_binding("ctrl+k", "p")])})

    # then — the proposal sits where the group was, not after `ctrl+y`
    assert result is not None
    assert [binding["command"] for binding in _plain(result.merged)] == ["x", "p", "y"]


def test_two_adjacent_removed_groups_get_their_proposals_in_order() -> None:
    # given — local removed both neighbouring groups; remote edited both; the model proposes one binding for each
    base = _array([_binding("ctrl+x", "x"), _binding("ctrl+k", "k"), _binding("ctrl+m", "m"), _binding("ctrl+y", "y")])
    local = _array([_binding("ctrl+x", "x"), _binding("ctrl+y", "y")])
    remote = _array(
        [
            _binding("ctrl+x", "x"),
            _binding("ctrl+k", "k", args=1),
            _binding("ctrl+m", "m", args=1),
            _binding("ctrl+y", "y"),
        ]
    )
    k_group = (("key", "ctrl+k"), ("when", None))
    m_group = (("key", "ctrl+m"), ("when", None))

    # when
    result = merge_keyed_array(
        base,
        local,
        remote,
        RULE,
        picks={k_group: _array([_binding("ctrl+k", "p1")]), m_group: _array([_binding("ctrl+m", "p2")])},
    )

    # then — the first proposal is not placed after the second
    assert result is not None
    assert [binding["command"] for binding in _plain(result.merged)] == ["x", "p1", "p2", "y"]


def test_an_unsettled_conflict_does_not_turn_an_unrelated_reorder_into_a_whole_array_conflict() -> None:
    # given — `ctrl+k` conflicts (both sides changed it), and the two sides moved its bindings differently
    base = _array([_binding("ctrl+k", "p"), _binding("ctrl+k", "q"), _binding("ctrl+z", "r")])
    local = _array([_binding("ctrl+k", "q"), _binding("ctrl+k", "p", args=1), _binding("ctrl+z", "r")])
    remote = _array([_binding("ctrl+z", "r"), _binding("ctrl+k", "p"), _binding("ctrl+k", "q", args=2)])

    # when
    result = merge_keyed_array(base, local, remote, RULE)

    # then — the conflict is that one group, and it can still be picked; the array is not a whole-array conflict
    assert result is not None
    assert [conflict.element for conflict in result.conflicts] == [(("key", "ctrl+k"), ("when", None))]


def test_a_proposal_for_a_group_both_sides_added_goes_to_the_end() -> None:
    # given — neither base nor a side held a binding in this group before both added one
    base = _array([_binding("ctrl+x", "x")])
    local = _array([_binding("ctrl+x", "x"), _binding("ctrl+k", "a1")])
    remote = _array([_binding("ctrl+x", "x"), _binding("ctrl+k", "a2")])
    group = (("key", "ctrl+k"), ("when", None))

    # when
    result = merge_keyed_array(base, local, remote, RULE, picks={group: _array([_binding("ctrl+k", "p")])})

    # then — there is no earlier place for it, so it is the last binding
    assert result is not None
    assert [binding["command"] for binding in _plain(result.merged)] == ["x", "p"]


def test_a_settled_conflict_is_placed_in_the_chosen_side_order_even_when_both_sides_reordered() -> None:
    # given — the two sides reordered the same group differently, and each changed a binding in it
    base = _array([_binding("ctrl+k", "a"), _binding("ctrl+k", "b"), _binding("ctrl+z", "c")])
    local = _array([_binding("ctrl+k", "b"), _binding("ctrl+k", "a", args=1), _binding("ctrl+z", "c")])
    remote = _array([_binding("ctrl+k", "a"), _binding("ctrl+z", "c"), _binding("ctrl+k", "b", args=2)])
    group = (("key", "ctrl+k"), ("when", None))

    # when
    picked = merge_keyed_array(base, local, remote, RULE, picks={group: "local"})

    # then — no whole-array conflict: the chosen side's order holds for its group
    assert picked is not None
    assert picked.conflicts == ()
    assert [binding["command"] for binding in _plain(picked.merged)] == ["b", "a", "c"]
    assert _plain(picked.merged)[1]["args"] == 1


def test_the_merge_is_the_same_whatever_order_the_identities_are_visited_in() -> None:
    # given — several groups conflict at once, so the order they are placed in could depend on how they are visited
    base = _array([_binding("ctrl+a", "1"), _binding("ctrl+b", "2"), _binding("ctrl+c", "3"), _binding("ctrl+d", "4")])
    local = _array(
        [
            _binding("ctrl+a", "1", args=1),
            _binding("ctrl+b", "2", args=1),
            _binding("ctrl+c", "3"),
            _binding("ctrl+d", "4"),
        ]
    )
    remote = _array(
        [
            _binding("ctrl+a", "1", args=2),
            _binding("ctrl+b", "2", args=2),
            _binding("ctrl+c", "3"),
            _binding("ctrl+d", "4"),
        ]
    )

    # when
    first = merge_keyed_array(base, local, remote, RULE)
    second = merge_keyed_array(base, local, remote, RULE)

    # then
    assert first is not None and second is not None
    assert _plain(first.merged) == _plain(second.merged)
    assert [conflict.element for conflict in first.conflicts] == [conflict.element for conflict in second.conflicts]
