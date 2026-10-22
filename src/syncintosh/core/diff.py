# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which key paths differ between two trees.

The `diff` command shows *where* two versions of a file differ without revealing the values, so the
report can be read aloud or pasted in an issue without leaking a setting. This is the pure half:
given two :class:`~syncintosh.core.tree.Tree` values, return the paths that disagree
(``AGENTS.md`` §2.1). Standard library only.
"""

from __future__ import annotations

from syncintosh.core.tree import KeyPath, Tree


def changed_paths(left: Tree, right: Tree) -> tuple[KeyPath, ...]:
    """The key paths where `left` and `right` differ, in document order.

    A path present on only one side counts as changed (a key added or removed); a path whose leaf
    differs by value or by JSON kind (``true`` vs ``1``) counts too. Order is `left`'s order first,
    then any path only `right` has, so the same pair always prints the same way.
    """
    paths = [*left.entries, *(path for path in right.entries if path not in left.entries)]
    return tuple(path for path in paths if left.value(path) != right.value(path))


__all__ = ["changed_paths"]
