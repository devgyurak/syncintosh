# SPDX-License-Identifier: AGPL-3.0-or-later
"""Key-level three-way merge over typed trees.

A line-level git merge can break JSON/XML syntax, so the unit of comparison is a **key path**
(``core/tree.py``). The rules are:

* a key changed on only one side always wins (idempotent when nothing changed);
* a key changed on **both** sides to **different** values is the only conflict;
* both sides deleting a key, or both adding the same value, is not a conflict;
* when one side replaces a key with a scalar while the other keeps or extends the subtree below
  it, that is **one** structural conflict recorded at the shallowest path — not one conflict per
  descendant.

When a both-sides change conflicts, the local value is kept in ``merged`` and the conflict is
recorded so the resolver can override it (``AGENTS.md`` §5.3). An ``ignore`` path keeps the
local value at that path and every path under it, never raising a conflict there.

This module is pure: it takes and returns plain values so it can be property-checked with no
setup (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from syncintosh.core.models import Conflict, TreeMerge
from syncintosh.core.tree import ABSENT, Absent, Array, EmptyObject, KeyPath, Leaf, Node, Scalar, Tree

# The paths a merge must never decide — noise keys the collector knows are machine-specific.
IgnorePaths = frozenset[KeyPath]


def merge_tree(
    base: Tree,
    local: Tree,
    remote: Tree,
    *,
    ignore: IgnorePaths = frozenset(),
) -> TreeMerge:
    """Merge three typed trees; conflicts are returned, not raised.

    ``base`` is the common ancestor; ``local`` wins a both-sides change. A path under ``ignore``
    keeps the local value and is never a conflict.
    """
    base_entries = base.entries
    local_entries = local.entries
    remote_entries = remote.entries

    merged: dict[KeyPath, Node] = {}
    conflicts: list[Conflict] = []
    for path in sorted({*base_entries, *local_entries, *remote_entries}):
        if _is_ignored(path, ignore):
            local_value = local_entries.get(path, ABSENT)
            if not isinstance(local_value, Absent):
                merged[path] = local_value
            continue
        base_value = base_entries.get(path, ABSENT)
        local_value = local_entries.get(path, ABSENT)
        remote_value = remote_entries.get(path, ABSENT)
        chosen, conflicted = _resolve(base_value, local_value, remote_value)
        if conflicted:
            conflicts.append(Conflict(path=path, base=base_value, local=local_value, remote=remote_value))
        if not isinstance(chosen, Absent):
            merged[path] = chosen

    conflicts = _fold_structure(merged, base_entries, local_entries, remote_entries, ignore, conflicts)
    ordered = _ordered_entries(merged, local_entries, remote_entries)
    return TreeMerge(merged=Tree.create(ordered), conflicts=tuple(conflicts))


def _resolve(base_value: Leaf, local_value: Leaf, remote_value: Leaf) -> tuple[Leaf, bool]:
    """Three-way pick for one path: ``(chosen, changed_on_both)``.

    ``changed_on_both`` is True only when both sides differ from base **and** from each other —
    the one case a single path cannot resolve automatically.
    """
    if local_value == remote_value:
        return local_value, False
    if local_value == base_value:
        return remote_value, False
    if remote_value == base_value:
        return local_value, False
    return local_value, True


def _is_ignored(path: KeyPath, ignore: IgnorePaths) -> bool:
    """True when `path` is an ignored path or sits under one."""
    return any(path[: len(prefix)] == prefix for prefix in ignore)


def _fold_structure(
    merged: dict[KeyPath, Node],
    base_entries: Mapping[KeyPath, Node],
    local_entries: Mapping[KeyPath, Node],
    remote_entries: Mapping[KeyPath, Node],
    ignore: IgnorePaths,
    conflicts: list[Conflict],
) -> list[Conflict]:
    """Resolve a terminal-versus-subtree clash, one conflict at the shallowest path.

    A scalar (or array) and a key path beneath it cannot both survive. The per-path pass already
    picked the right leaf at such a path; this pass makes the tree consistent by dropping the
    losing side, and records **one** structural conflict at the shallowest path when both sides
    changed incompatibly:

    * local collapsed a subtree to a terminal while remote edited inside it → conflict at the
      path, local's terminal wins;
    * the symmetric remote-versus-local case;
    * a one-sided restructure (the other side exactly matches base) is not a conflict — the
      changed side simply wins.

    An ``EmptyObject`` that gained a child is not a conflict — the object is merely no longer
    empty — so it is dropped without a conflict. Deeper conflicts are subsumed so the user sees
    one decision, not a cascade.
    """
    for path in [p for p, node in merged.items() if isinstance(node, EmptyObject) and _has_descendant(merged, p)]:
        del merged[path]

    conflicts_by_path = list(conflicts)
    structural: list[Conflict] = []
    roots: list[KeyPath] = []
    candidates = sorted(
        p
        for p in {*merged, *local_entries, *remote_entries}
        if _is_terminal(local_entries.get(p)) or _is_terminal(remote_entries.get(p)) or _is_terminal(merged.get(p))
    )
    for path in candidates:
        if any(path[: len(root)] == root for root in roots):
            continue
        if not _is_ignored(path, ignore) and _shape_conflict(path, base_entries, local_entries, remote_entries, ignore):
            roots.append(path)
            structural.append(
                Conflict(
                    path=path,
                    base=base_entries.get(path, ABSENT),
                    local=local_entries.get(path, ABSENT),
                    remote=remote_entries.get(path, ABSENT),
                )
            )
        if _is_terminal(merged.get(path)):
            if _keeps_local_subtree(path, ignore, local_entries):
                # A local key under this path is ignored, so local's subtree must survive; the
                # terminal that won here gives way instead of erasing the ignored key.
                del merged[path]
            else:
                # A terminal won at this path: nothing may live beneath it.
                for deeper in [p for p in merged if _strictly_under(p, path)]:
                    del merged[deeper]

    subsumed = {conflict.path for conflict in structural}
    kept = [
        conflict
        for conflict in conflicts_by_path
        if not any(conflict.path == root or _strictly_under(conflict.path, root) for root in subsumed)
    ]
    return sorted([*kept, *structural], key=lambda conflict: conflict.path)


def _is_terminal(node: Leaf | None) -> bool:
    """A leaf that cannot hold a key path: a scalar or an array."""
    return isinstance(node, (Scalar, Array))


def _keeps_local_subtree(path: KeyPath, ignore: IgnorePaths, local_entries: Mapping[KeyPath, Node]) -> bool:
    """True when local holds an ignored path strictly beneath `path`."""
    return any(
        _strictly_under(candidate, path) and any(entry[: len(candidate)] == candidate for entry in local_entries)
        for candidate in ignore
    )


def _shape_conflict(
    path: KeyPath,
    base_entries: Mapping[KeyPath, Node],
    local_entries: Mapping[KeyPath, Node],
    remote_entries: Mapping[KeyPath, Node],
    ignore: IgnorePaths,
) -> bool:
    """True when one side collapsed `path` while the other changed the subtree beneath it.

    Both halves are required: the collapsing side must have actually changed the path, and the
    expanding side must have actually changed something below it. Otherwise the change is
    one-sided and the changed side simply wins. An ignored path is not a change, so its update
    never turns a restructure into a conflict.
    """
    if _is_terminal(local_entries.get(path)) and _has_descendant(_effective(remote_entries, ignore), path):
        return local_entries.get(path) != base_entries.get(path) and _subtree_changed(
            remote_entries, base_entries, path, ignore
        )
    if _is_terminal(remote_entries.get(path)) and _has_descendant(_effective(local_entries, ignore), path):
        return remote_entries.get(path) != base_entries.get(path) and _subtree_changed(
            local_entries, base_entries, path, ignore
        )
    return False


def _effective(entries: Mapping[KeyPath, Node], ignore: IgnorePaths) -> dict[KeyPath, Node]:
    """The entries with ignored paths removed: an ignored path is never counted as a change."""
    return {key: value for key, value in entries.items() if not _is_ignored(key, ignore)}


def _subtree_changed(
    entries: Mapping[KeyPath, Node],
    base_entries: Mapping[KeyPath, Node],
    path: KeyPath,
    ignore: IgnorePaths,
) -> bool:
    """True when `entries` differs from base anywhere strictly beneath `path`, ignoring ignore."""
    return _subtree(_effective(entries, ignore), path) != _subtree(_effective(base_entries, ignore), path)


def _subtree(entries: Mapping[KeyPath, Node], path: KeyPath) -> dict[KeyPath, Node]:
    return {key: value for key, value in entries.items() if _strictly_under(key, path)}


def _has_descendant(paths: Iterable[KeyPath], path: KeyPath) -> bool:
    return any(_strictly_under(candidate, path) for candidate in paths)


def _strictly_under(candidate: KeyPath, path: KeyPath) -> bool:
    return len(candidate) > len(path) and candidate[: len(path)] == path


def _ordered_entries(
    merged: Mapping[KeyPath, Node],
    local_entries: Mapping[KeyPath, Node],
    remote_entries: Mapping[KeyPath, Node],
) -> dict[KeyPath, Node]:
    """Order the merged paths: the local document order, with a remote-added key placed after
    its preceding sibling in the remote's order (``docs/design/03-manifest.md``).

    The render step does minimal text edits against the local file, so a newly added key must
    arrive in the sequence the remote document already had it — not appended ad hoc.
    """
    local_paths = list(local_entries)
    remote_paths = list(remote_entries)
    ordered: dict[KeyPath, Node] = {}
    _fill(ordered, (), merged, local_paths, remote_paths)
    for path, node in merged.items():
        ordered.setdefault(path, node)
    return ordered


def _fill(
    ordered: dict[KeyPath, Node],
    parent: KeyPath,
    merged: Mapping[KeyPath, Node],
    local_paths: list[KeyPath],
    remote_paths: list[KeyPath],
) -> None:
    surviving = {path[len(parent)] for path in merged if len(path) > len(parent) and path[: len(parent)] == parent}
    for key in _child_order(_child_keys(local_paths, parent), _child_keys(remote_paths, parent), surviving):
        child = (*parent, key)
        if child in merged:
            ordered[child] = merged[child]
        _fill(ordered, child, merged, local_paths, remote_paths)


def _child_keys(paths: list[KeyPath], parent: KeyPath) -> list[str]:
    """The immediate child keys of `parent`, in the order they first appear in `paths`."""
    keys: list[str] = []
    for path in paths:
        if len(path) > len(parent) and path[: len(parent)] == parent:
            key = path[len(parent)]
            if key not in keys:
                keys.append(key)
    return keys


def _child_order(local_kids: list[str], remote_kids: list[str], surviving: set[str]) -> list[str]:
    """Merge two sibling orders: keep the local order, insert a remote-only key after its last
    preceding remote sibling, or at the end when it has none."""
    order = [key for key in local_kids if key in surviving]
    for index, key in enumerate(remote_kids):
        if key in order or key not in surviving:
            continue
        preceding = next((prior for prior in reversed(remote_kids[:index]) if prior in order), None)
        if preceding is None:
            order.append(key)
        else:
            order.insert(order.index(preceding) + 1, key)
    # A key that survived but was in neither order (a base-only intermediate) goes last.
    order.extend(sorted(surviving - set(order)))
    return order


__all__ = ["IgnorePaths", "merge_tree"]
