# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: merge three typed trees at the key-path level.

This is a thin orchestration over the pure engine so the CLI never calls `core` directly and
`core` stays free of use-case concerns.
"""

from __future__ import annotations

from syncintosh.core.merge import IgnorePaths, merge_tree
from syncintosh.core.models import TreeMerge
from syncintosh.core.tree import Tree


def merge_documents(
    base: Tree,
    local: Tree,
    remote: Tree,
    *,
    ignore: IgnorePaths = frozenset(),
) -> TreeMerge:
    """Merge base/local/remote trees; conflicts are returned, not raised."""
    return merge_tree(base, local, remote, ignore=ignore)
