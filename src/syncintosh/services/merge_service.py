# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: merge three typed trees, or three documents of one format, at the key-path level.

This is a thin orchestration over the pure engine so the CLI never calls `core` directly and
`core` stays free of use-case concerns.
"""

from __future__ import annotations

from dataclasses import dataclass

from syncintosh.core.merge import IgnorePaths, merge_tree
from syncintosh.core.models import Conflict, TreeMerge
from syncintosh.core.ports import ContentAdapter
from syncintosh.core.tree import Tree


@dataclass(frozen=True)
class RenderedMerge:
    """A merged document rendered back to text, with the conflicting key paths.

    `conflicts` carries the key paths only (a `Conflict` masks its values in `repr`), so a caller
    can report what collided without printing settings content (``AGENTS.md`` §4).
    """

    text: str
    conflicts: tuple[Conflict, ...]

    @property
    def has_conflicts(self) -> bool:
        """True when at least one key needs a human (or LLM) decision."""
        return bool(self.conflicts)


def merge_documents(
    base: Tree,
    local: Tree,
    remote: Tree,
    *,
    ignore: IgnorePaths = frozenset(),
) -> TreeMerge:
    """Merge base/local/remote trees; conflicts are returned, not raised."""
    return merge_tree(base, local, remote, ignore=ignore)


def merge_text(
    base_text: str,
    local_text: str,
    remote_text: str,
    adapter: ContentAdapter,
    *,
    ignore: IgnorePaths = frozenset(),
) -> RenderedMerge:
    """Parse three documents as `adapter`'s format, merge, and render against the local text.

    A side that does not parse raises `ContentParseError` (the caller reports it as a usage
    error); a both-sides change is returned as a conflict, with the local text rendered as the
    unresolved candidate.
    """
    base = adapter.parse(base_text)
    local = adapter.parse(local_text)
    remote = adapter.parse(remote_text)
    result = merge_tree(base, local, remote, ignore=ignore)
    return RenderedMerge(text=adapter.render(result.merged, local_text), conflicts=result.conflicts)
