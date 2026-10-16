# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content merge for one record, key-level when a format adapter exists.

The adapter's key-level rules run on decoded text, so the text must first decode as UTF-8. A
blob that does not (a `binary` record, or a text file that picked up a stray byte) cannot go
through a structural merge: when both sides changed it, the only honest verdict is a conflict
(``docs/design/03-manifest.md``). A one-sided change never needs the merge, so it is still
applied — that is the whole point of routing only a *both-sides* change into the merge.

For a both-sides change the adapter route is `parse → merge_tree → render`, and the rendered
text is re-parsed and compared with the merged tree. That check is what keeps a partially
rendered file from being published as "merged": if the render does not round-trip to the exact
tree the engine produced, the result is a conflict, not a silently wrong file. A parse failure
(malformed JSONC, a duplicate key) is a conflict too. Without an adapter the line-level
`MergeDriver` runs, as before.

This module is pure: it takes plain bytes and ports and returns a frozen `ContentMerge`, so it
is testable with no adapter and no filesystem (``AGENTS.md`` §2.1, §2.9).
"""

from __future__ import annotations

from syncintosh.core.errors import ContentParseError
from syncintosh.core.merge import merge_tree
from syncintosh.core.models import ContentMerge
from syncintosh.core.ports import ContentAdapter, MergeDriver


def _decode_all(base: bytes, local: bytes, remote: bytes) -> tuple[str, str, str] | None:
    """Decode all three blobs, or `None` when any of them is not UTF-8."""
    try:
        return base.decode("utf-8"), local.decode("utf-8"), remote.decode("utf-8")
    except UnicodeDecodeError:
        return None


def merge_content(
    base: bytes,
    local: bytes,
    remote: bytes,
    driver: MergeDriver,
    adapter: ContentAdapter | None = None,
) -> ContentMerge:
    """Three-way merge one record's plaintext bytes.

    A one-sided change is taken directly, without invoking any merge; only a both-sides change
    is merged, through the adapter when one is given and otherwise through the driver. An
    undecodable, unparseable, or non-round-tripping both-sides change is a conflict with the
    local side as the unresolved candidate.
    """
    if local == remote:
        return ContentMerge(data=local, conflicted=False)
    if local == base:
        return ContentMerge(data=remote, conflicted=False)
    if remote == base:
        return ContentMerge(data=local, conflicted=False)
    decoded = _decode_all(base, local, remote)
    if decoded is None:
        return ContentMerge(data=local, conflicted=True)
    base_text, local_text, remote_text = decoded
    if adapter is not None:
        merged_text = _merge_via_adapter(adapter, base_text, local_text, remote_text)
        if merged_text is None:
            return ContentMerge(data=local, conflicted=True)
        return ContentMerge(data=merged_text.encode("utf-8"), conflicted=False)
    result = driver.merge(base_text, local_text, remote_text)
    return ContentMerge(data=result.text.encode("utf-8"), conflicted=result.conflicted)


def _merge_via_adapter(
    adapter: ContentAdapter,
    base_text: str,
    local_text: str,
    remote_text: str,
) -> str | None:
    """The adapter merge, or `None` when it cannot be trusted (a conflict).

    `None` covers three failures the caller treats identically: a side that does not parse, a
    merged tree the engine leaves conflicted, and a render that does not re-parse to the tree
    that was merged.
    """
    try:
        base_tree = adapter.parse(base_text)
        local_tree = adapter.parse(local_text)
        remote_tree = adapter.parse(remote_text)
    except ContentParseError:
        return None
    result = merge_tree(base_tree, local_tree, remote_tree)
    if result.has_conflicts:
        return None
    try:
        rendered = adapter.render(result.merged, local_text)
        if adapter.parse(rendered) != result.merged:
            return None
    except ContentParseError:
        return None
    return rendered
