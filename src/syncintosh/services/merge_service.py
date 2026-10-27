# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: merge three documents of one format at the key-path level.

Thin orchestration over the pure engine so the CLI never calls `core` directly and `core` stays
free of use-case concerns.
"""

from __future__ import annotations

from dataclasses import dataclass

from syncintosh.core.content_merge import render_merged
from syncintosh.core.merge import IgnorePaths, merge_tree
from syncintosh.core.models import Conflict
from syncintosh.core.ports import ContentAdapter, PresetCatalog


@dataclass(frozen=True)
class RenderedMerge:
    """A merged document rendered back to text, with the conflicting key paths.

    `conflicts` carries the key paths only (a `Conflict` masks its values in `repr`), so a caller
    can report what collided without printing settings content (``AGENTS.md`` §4). `conflicted`
    is set when the render could not be trusted even though no key path conflicts — the caller
    must treat it as a conflict too, never as a successful merge.
    """

    text: str
    conflicts: tuple[Conflict, ...] = ()
    conflicted: bool = False

    @property
    def has_conflicts(self) -> bool:
        """True when the merge needs a human (or LLM) decision, or could not be rendered."""
        return self.conflicted or bool(self.conflicts)


def merge_text(
    base_text: str,
    local_text: str,
    remote_text: str,
    adapter: ContentAdapter,
    *,
    ignore: IgnorePaths = frozenset(),
    catalog: PresetCatalog | None = None,
    path: str | None = None,
) -> RenderedMerge:
    """Parse three documents as `adapter`'s format, merge, and render against the local text.

    A side that does not parse raises `ContentParseError` (the caller reports it as a usage
    error). A both-sides change is returned as a conflict with the local text as the candidate.
    The rendered text is re-parsed and compared with the merged tree; when it does not round-trip
    the merge is reported as conflicted rather than emitting text that is not the merge.
    """
    base = adapter.parse(base_text)
    local = adapter.parse(local_text)
    remote = adapter.parse(remote_text)
    policy = None if catalog is None or path is None else catalog.policy_for(path)
    arrays = None if policy is None else policy.array_rules
    result = merge_tree(base, local, remote, ignore=ignore, keyed=arrays)
    if result.has_conflicts:
        rendered = render_merged(adapter, result.merged, local_text)
        return RenderedMerge(text=local_text if rendered is None else rendered, conflicts=result.conflicts)
    rendered = render_merged(adapter, result.merged, local_text)
    if rendered is None:
        return RenderedMerge(text=local_text, conflicted=True)
    return RenderedMerge(text=rendered)
