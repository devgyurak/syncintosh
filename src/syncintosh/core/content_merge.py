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

from collections.abc import Mapping

from syncintosh.core.errors import ContentParseError, RenderRefused
from syncintosh.core.keyed_array import ArrayRule
from syncintosh.core.merge import merge_tree
from syncintosh.core.models import ConflictAddress, ContentMerge
from syncintosh.core.ports import ContentAdapter, MergeDriver
from syncintosh.core.resolution import Proposal, Side
from syncintosh.core.tree import Array, ElementId, KeyPath, Tree


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
    *,
    choice: Side | Proposal | None = None,
    key_choices: Mapping[KeyPath, Side | Proposal] | None = None,
    arrays: Mapping[KeyPath, ArrayRule] | None = None,
    element_picks: Mapping[tuple[KeyPath, ElementId], Side | Proposal] | None = None,
) -> ContentMerge:
    """Three-way merge one record's plaintext bytes.

    A one-sided change is taken directly, without invoking any merge; only a both-sides change
    is merged, through the adapter when one is given and otherwise through the driver. An
    undecodable, unparseable, or non-round-tripping both-sides change is a conflict with the
    local side as the unresolved candidate.

    ``choice`` settles a both-sides change to one side's whole file, or to a proposed file, and skips
    the merge. ``key_choices`` settles individual key paths of a structural merge, to a side or to a
    proposed value; it has no effect without an adapter, which is the only path that has keys. A
    proposal must parse in the record's format, or it does not settle anything and the conflict stays.
    """
    if local == remote:
        return ContentMerge(data=local, conflicted=False)
    if local == base:
        return ContentMerge(data=remote, conflicted=False)
    if remote == base:
        return ContentMerge(data=local, conflicted=False)
    if isinstance(choice, Proposal):
        return _proposed_file(choice, adapter, local)
    if choice is not None:
        return ContentMerge(data=remote if choice == "remote" else local, conflicted=False)
    decoded = _decode_all(base, local, remote)
    if decoded is None:
        return ContentMerge(data=local, conflicted=True)
    base_text, local_text, remote_text = decoded
    if adapter is not None:
        merged_text, addresses, reason = _merge_via_adapter(
            adapter, base_text, local_text, remote_text, key_choices or {}, arrays or {}, element_picks or {}
        )
        if merged_text is None:
            return ContentMerge(data=local, conflicted=True, conflict_addresses=addresses, reason=reason)
        return ContentMerge(data=merged_text.encode("utf-8"), conflicted=False)
    result = driver.merge(base_text, local_text, remote_text)
    return ContentMerge(data=result.text.encode("utf-8"), conflicted=result.conflicted)


def _proposed_file(proposal: Proposal, adapter: ContentAdapter | None, local: bytes) -> ContentMerge:
    """A proposed whole file, or a conflict when it cannot be encoded or does not parse in this format."""
    try:
        data = proposal.text.encode("utf-8")
    except UnicodeEncodeError:
        return ContentMerge(data=local, conflicted=True)
    if adapter is not None and _parsed_or_none(adapter, proposal.text) is None:
        return ContentMerge(data=local, conflicted=True)
    return ContentMerge(data=data, conflicted=False)


def _parsed_or_none(adapter: ContentAdapter, text: str) -> Tree | None:
    """A text as the adapter's tree, or `None` when the adapter does not accept it."""
    try:
        return adapter.parse(text)
    except ContentParseError:
        return None


def render_merged(
    adapter: ContentAdapter, merged: Tree, template: str, arrays: Mapping[KeyPath, ArrayRule] | None = None
) -> str | None:
    """Render `merged` and re-parse it; `None` when it does not round-trip to the same tree.

    This is the guard that keeps an adapter from publishing a partially rendered file: the caller
    treats `None` as a conflict rather than writing text that says it is the merge but is not.
    Shared by the byte-level content merge and the `merge` command so the two cannot drift.
    """
    return render_checked(adapter, merged, template, arrays)[0]


# The reasons a render can refuse, as fixed texts: a reason that echoed the adapter's message could carry a key name,
# and a conflict must never show a value (AGENTS.md §4).
RENDER_REFUSED = "this change cannot be written to this file safely"
ROUND_TRIP_REFUSED = "the merged result does not render back to itself"


def render_checked(
    adapter: ContentAdapter, merged: Tree, template: str, arrays: Mapping[KeyPath, ArrayRule] | None
) -> tuple[str | None, str]:
    """`render_merged`, with a fixed reason when it refuses.

    An adapter that names its refusal (`RenderRefused`) passes its own fixed text through, so the person learns what
    would have to change; any other refusal gets the generic text. Neither ever carries a value.
    """
    try:
        rendered = adapter.render(merged, template, arrays)
        if adapter.parse(rendered) != merged:
            return None, ROUND_TRIP_REFUSED
    except RenderRefused as exc:
        return None, exc.reason
    except ContentParseError:
        return None, RENDER_REFUSED
    return rendered, ""


def _settled_keys(
    adapter: ContentAdapter, key_choices: Mapping[KeyPath, Side | Proposal]
) -> dict[KeyPath, Side | Tree]:
    """Each key pick as a value the merge can take.

    A proposal that does not parse is left out, so its key stays a conflict rather than taking a value
    the format never accepted.
    """
    settled: dict[KeyPath, Side | Tree] = {}
    for path, pick in key_choices.items():
        if not isinstance(pick, Proposal):
            settled[path] = pick
            continue
        proposed = _parsed_or_none(adapter, pick.text)
        if proposed is not None:
            settled[path] = proposed
    return settled


def _settled_elements(
    adapter: ContentAdapter, element_picks: Mapping[tuple[KeyPath, ElementId], Side | Proposal]
) -> dict[tuple[KeyPath, ElementId], Side | Array]:
    """Each element pick as a value the merge can take: a proposed array must parse, or the pick is dropped.

    The merge checks the rest (every element belongs to its group), so a proposal that parses but names the
    wrong group still stays a conflict.
    """
    settled: dict[tuple[KeyPath, ElementId], Side | Array] = {}
    for address, pick in element_picks.items():
        if not isinstance(pick, Proposal):
            settled[address] = pick
            continue
        parsed = _parsed_or_none(adapter, pick.text)
        root = parsed.value(()) if parsed is not None else None
        if isinstance(root, Array):
            settled[address] = root
    return settled


def _merge_via_adapter(
    adapter: ContentAdapter,
    base_text: str,
    local_text: str,
    remote_text: str,
    key_choices: Mapping[KeyPath, Side | Proposal],
    arrays: Mapping[KeyPath, ArrayRule],
    element_picks: Mapping[tuple[KeyPath, ElementId], Side | Proposal],
) -> tuple[str | None, tuple[ConflictAddress, ...], str]:
    """The adapter merge as ``(text, ())``, or ``(None, conflict paths)`` when it cannot be trusted.

    ``None`` the text covers three failures the caller treats identically: a side that does not
    parse, a merged tree the engine leaves conflicted (the tree's key paths are returned so the
    pending report can name them), and a render that does not re-parse to the tree that was
    merged.
    """
    try:
        base_tree = adapter.parse(base_text)
        local_tree = adapter.parse(local_text)
        remote_tree = adapter.parse(remote_text)
    except ContentParseError:
        return None, (), ""
    result = merge_tree(
        base_tree,
        local_tree,
        remote_tree,
        choices=_settled_keys(adapter, key_choices),
        keyed=arrays,
        element_picks=_settled_elements(adapter, element_picks),
    )
    if result.has_conflicts:
        # The first fixed reason a conflict carries (an order conflict), so the person is told why it is a whole array.
        reason = next((conflict.reason for conflict in result.conflicts if conflict.reason), "")
        addresses = tuple(dict.fromkeys((conflict.path, conflict.element) for conflict in result.conflicts))
        return None, addresses, reason
    rendered, reason = render_checked(adapter, result.merged, local_text, arrays)
    if rendered is None:
        return None, (), reason
    return rendered, (), ""
