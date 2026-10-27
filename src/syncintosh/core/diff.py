# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which key paths differ between two trees.

The `diff` command shows *where* two versions of a file differ without revealing the values, so the
report can be read aloud or pasted in an issue without leaking a setting. This is the pure half:
given two :class:`~syncintosh.core.tree.Tree` values, return the paths that disagree
(``AGENTS.md`` §2.1). Standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping

from syncintosh.core.keyed_array import ArrayRule, group_ids, group_leaf
from syncintosh.core.models import ConflictAddress
from syncintosh.core.tree import ElementId, KeyPath, Leaf, Tree


def changed_addresses(
    left: Tree, right: Tree, arrays: Mapping[KeyPath, ArrayRule] | None = None
) -> tuple[ConflictAddress, ...]:
    """The changed places, with an identified array's changed bindings named instead of the whole array.

    An array with a rule reports each binding group that differs (`(path, group)`), so a diff shows the
    binding that changed rather than the file's whole array. An array with no rule, one the rule cannot
    read, or one whose bindings are the same but whose order changed, reports `(path, None)` as before.
    """
    addresses: list[ConflictAddress] = []
    for path in changed_paths(left, right):
        rule = (arrays or {}).get(path)
        groups = None if rule is None else _changed_groups(left.value(path), right.value(path), rule)
        if groups:
            addresses.extend((path, group) for group in groups)
        else:
            addresses.append((path, None))
    return tuple(addresses)


def _changed_groups(left: Leaf, right: Leaf, rule: ArrayRule) -> tuple[ElementId, ...] | None:
    """The groups whose bindings differ between two arrays, or `None` when either cannot be identified."""
    left_ids = group_ids(left, rule)
    right_ids = group_ids(right, rule)
    if left_ids is None or right_ids is None:
        return None
    groups = tuple(dict.fromkeys((*left_ids, *right_ids)))
    return tuple(group for group in groups if group_leaf(left, rule, group) != group_leaf(right, rule, group))


def changed_paths(left: Tree, right: Tree) -> tuple[KeyPath, ...]:
    """The key paths where `left` and `right` differ, in document order.

    A path present on only one side counts as changed (a key added or removed); a path whose leaf
    differs by value or by JSON kind (``true`` vs ``1``) counts too. Order is `left`'s order first,
    then any path only `right` has, so the same pair always prints the same way.
    """
    paths = [*left.entries, *(path for path in right.entries if path not in left.entries)]
    return tuple(path for path in paths if left.value(path) != right.value(path))


__all__ = ["changed_addresses", "changed_paths"]
