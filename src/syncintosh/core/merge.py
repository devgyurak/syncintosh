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

from syncintosh.core.keyed_array import ArrayRule, KeyedMerge, OrderConflict, merge_keyed_array
from syncintosh.core.models import Conflict, TreeMerge
from syncintosh.core.resolution import Side
from syncintosh.core.tree import ABSENT, Absent, Array, ElementId, EmptyObject, KeyPath, Leaf, Node, Scalar, Tree

# The paths a merge must never decide — noise keys the collector knows are machine-specific.
IgnorePaths = frozenset[KeyPath]


def merge_tree(
    base: Tree,
    local: Tree,
    remote: Tree,
    *,
    ignore: IgnorePaths = frozenset(),
    choices: Mapping[KeyPath, Side | Tree] | None = None,
    keyed: Mapping[KeyPath, ArrayRule] | None = None,
    element_picks: Mapping[tuple[KeyPath, ElementId], Side | Array] | None = None,
) -> TreeMerge:
    """Merge three typed trees; conflicts are returned, not raised.

    ``base`` is the common ancestor; ``local`` wins a both-sides change. A path under ``ignore``
    keeps the local value and is never a conflict. ``choices`` settles a conflicted path to the
    side an operator picked, or to a proposed subtree: a `Tree` whose keys are relative to that
    path, so a proposed scalar is the tree with one root entry. A choice on a path that is not a
    conflict is ignored, and a conflict with no choice stays in the result.

    ``keyed`` names the array paths merged element by element (`core/keyed_array.py`). Such an array
    is consulted only once it is a both-sides conflict; a conflict in one of its groups is recorded
    with that group's identity, and the rest of the array merges. An array the rule cannot read keeps
    the whole-array conflict it would have had without the rule.

    ``element_picks`` settles one group of a keyed array, by ``(path, group identity)``, to a side or to
    a proposed array (`core/keyed_array.py`). It is consulted only for a group that still conflicts.
    """
    base_entries = base.entries
    local_entries = local.entries
    remote_entries = remote.entries
    rules = keyed or {}
    group_picks = element_picks or {}

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
            element_merge = (
                merge_keyed_array(
                    base_value,
                    local_value,
                    remote_value,
                    rules[path],
                    picks={element: pick for (at, element), pick in group_picks.items() if at == path},
                )
                if path in rules
                else None
            )
            if isinstance(element_merge, KeyedMerge):
                merged[path] = element_merge.merged
                conflicts.extend(
                    Conflict(
                        path=path,
                        base=group.base,
                        local=group.local,
                        remote=group.remote,
                        element=group.element,
                    )
                    for group in element_merge.conflicts
                )
                continue
            reason = element_merge.reason if isinstance(element_merge, OrderConflict) else ""
            conflicts.append(
                Conflict(path=path, base=base_value, local=local_value, remote=remote_value, reason=reason)
            )
        if not isinstance(chosen, Absent):
            merged[path] = chosen

    conflicts = _fold_structure(merged, base_entries, local_entries, remote_entries, ignore, conflicts)
    conflicts = _apply_choices(conflicts, choices or {}, merged, local_entries, remote_entries, ignore)
    conflicts.sort(key=_conflict_order)
    _drop_emptied(merged)
    ordered = _ordered_entries(merged, local_entries, remote_entries)
    # A merge that deleted every key leaves an **empty object** document, and a parse represents
    # that as `{(): EmptyObject()}` (`core.tree._flatten`), never as a tree with no root entry.
    # Returning the latter would break the invariant the content merge relies on — that rendering
    # the merged tree and re-parsing it returns the same tree — so a file whose merge emptied it
    # could never settle, and every later sync would read it as changed.
    return TreeMerge(merged=Tree.create(ordered or {(): EmptyObject()}), conflicts=tuple(conflicts))


def _conflict_order(conflict: Conflict) -> tuple[KeyPath, tuple[tuple[str, bool, str], ...]]:
    """Sort key: by path, then by element, so a whole-path conflict precedes its element conflicts.

    A missing field and an empty string sort apart (the flag), so the order is total and stable.
    """
    if conflict.element is None:
        return conflict.path, ()
    return conflict.path, tuple((field, value is None, value or "") for field, value in conflict.element)


def _apply_choices(
    conflicts: list[Conflict],
    choices: Mapping[KeyPath, Side | Tree],
    merged: dict[KeyPath, Node],
    local_entries: Mapping[KeyPath, Node],
    remote_entries: Mapping[KeyPath, Node],
    ignore: IgnorePaths,
) -> list[Conflict]:
    """Settle each chosen conflict to the side or proposal given; the rest stay conflicts.

    Only a path that is still a conflict is consulted, so a choice on a path that never conflicted
    changes nothing. Conflicts are subsumed under their shallowest path, so no chosen path sits
    under another chosen path and the order of application does not matter.
    """
    unchosen: list[Conflict] = []
    for conflict in conflicts:
        pick = choices.get(conflict.path)
        if pick is None:
            unchosen.append(conflict)
            continue
        if isinstance(pick, Tree):
            chosen = _rebased(conflict.path, pick)
        else:
            chosen = remote_entries if pick == "remote" else local_entries
        _choose_entries(conflict.path, chosen, merged, local_entries, ignore)
    return unchosen


def _rebased(path: KeyPath, subtree: Tree) -> Mapping[KeyPath, Node]:
    """A proposed subtree with its relative keys placed under `path`."""
    return {(*path, *relative): node for relative, node in subtree.entries.items()}


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

    A local ``{}`` at a key does not mean "the key is now empty": it is the local side's deletion of
    the keys it had under that key. Keys the other side added beneath it survive, because only a
    change both sides make to the same key is a conflict (``AGENTS.md`` §5.3). Keep this rule: a
    "empty object replaces everything beneath it" rule would silently drop the other side's edits.
    """
    conflicts_by_path = list(conflicts)
    structural: list[Conflict] = []
    roots: list[KeyPath] = []
    for path in sorted({*merged, *base_entries, *local_entries, *remote_entries}):
        if any(path[: len(root)] == root for root in roots):
            continue
        conflicted = not _is_ignored(path, ignore) and _shape_conflict(
            path, base_entries, local_entries, remote_entries, ignore
        )
        if conflicted:
            roots.append(path)
            structural.append(
                Conflict(
                    path=path,
                    base=base_entries.get(path, ABSENT),
                    local=local_entries.get(path, ABSENT),
                    remote=remote_entries.get(path, ABSENT),
                )
            )
            _choose_entries(path, local_entries, merged, local_entries, ignore)
        elif _is_terminal(merged.get(path)) and _keeps_local_subtree(path, ignore, local_entries):
            # A local key under this path is ignored, so local's subtree must survive; the
            # terminal that won here gives way instead of erasing the ignored key. Every other
            # terminal above live descendants is a conflict, already resolved above.
            del merged[path]

    # This runs after the fold: during it an `EmptyObject` can transiently sit above a descendant
    # a structural conflict is about to remove, and dropping it then would erase a root or a
    # parent that still stands.
    _drop_emptied(merged)

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


def _drop_emptied(merged: dict[KeyPath, Node]) -> None:
    """Drop an `EmptyObject` that kept a surviving child: the object is no longer empty."""
    for path in [p for p, node in merged.items() if isinstance(node, EmptyObject) and _has_descendant(merged, p)]:
        del merged[path]


def _choose_entries(
    path: KeyPath,
    chosen_entries: Mapping[KeyPath, Node],
    merged: dict[KeyPath, Node],
    local_entries: Mapping[KeyPath, Node],
    ignore: IgnorePaths,
) -> None:
    """Make `merged` take `chosen_entries`' shape at `path`, the same way for a side or a proposal.

    The chosen value's leaf replaces whatever sat beneath `path`; a chosen deletion leaves nothing
    there, and the chosen subtree is restored whole (a path the per-path pass deleted must come
    back). Ignored paths are never taken from the chosen value: local keeps them, as always.
    A terminal that would sit above local's ignored keys gives way, as `_fold_structure` does. The
    conflict is settled either way, so this only fixes the candidate value a resolver starts from
    (``AGENTS.md`` §5.3).
    """
    for deeper in [candidate for candidate in merged if _strictly_under(candidate, path)]:
        del merged[deeper]
    chosen_leaf = chosen_entries.get(path, ABSENT)
    if isinstance(chosen_leaf, Absent):
        merged.pop(path, None)
    else:
        merged[path] = chosen_leaf
    for deeper, node in chosen_entries.items():
        if _strictly_under(deeper, path) and not _is_ignored(deeper, ignore):
            merged[deeper] = node
    for deeper, node in local_entries.items():
        if _strictly_under(deeper, path) and _is_ignored(deeper, ignore):
            merged[deeper] = node
    if _is_terminal(merged.get(path)) and _keeps_local_subtree(path, ignore, local_entries):
        del merged[path]
    # The chosen side's object that holds `path` may be an empty object there: an `EmptyObject` on
    # that side is the only record that the object exists, so restore it unless the merge already
    # has something beneath it. Without this, choosing the last child's deletion erases the parent.
    for depth in range(len(path)):
        ancestor = path[:depth]
        empty_here = isinstance(chosen_entries.get(ancestor), EmptyObject)
        if empty_here and ancestor not in merged and not _has_descendant(merged, ancestor):
            merged[ancestor] = EmptyObject()


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
    """True when the two sides cannot both be represented at `path`.

    Two incompatible restructures conflict: one side collapses a path to a leaf while the other
    changed (or kept) a subtree beneath it, and one side deletes a path while the other collapses
    or extends it. A one-sided change where the other side matches base simply wins, and an
    ignored path is not a change, so its update never turns a restructure into a conflict.
    """
    if _is_terminal(local_entries.get(path)) and _has_descendant(_effective(remote_entries, ignore), path):
        return local_entries.get(path) != base_entries.get(path) and _subtree_changed(
            remote_entries, base_entries, path, ignore
        )
    if _is_terminal(remote_entries.get(path)) and _has_descendant(_effective(local_entries, ignore), path):
        return remote_entries.get(path) != base_entries.get(path) and _subtree_changed(
            local_entries, base_entries, path, ignore
        )
    local_restructured = _has_descendant(local_entries, path)
    remote_restructured = _has_descendant(remote_entries, path)
    # "Deleted" means no leaf *and* no subtree. An `EmptyObject` is a present leaf, not a deletion.
    local_deleted = isinstance(local_entries.get(path, ABSENT), Absent) and not local_restructured
    remote_deleted = isinstance(remote_entries.get(path, ABSENT), Absent) and not remote_restructured
    # A deletion against the other side's restructure of the same path is a conflict too: the
    # per-path pass sees "no leaf on either side" and would let one edit vanish silently.
    if _is_terminal(base_entries.get(path)):
        return (local_deleted and remote_restructured) or (remote_deleted and local_restructured)
    if _has_descendant(base_entries, path):
        return (
            (local_deleted and remote_restructured and _subtree_changed(remote_entries, base_entries, path, ignore))
            or (remote_deleted and local_restructured and _subtree_changed(local_entries, base_entries, path, ignore))
            or (local_deleted and _is_terminal(remote_entries.get(path)))
            or (remote_deleted and _is_terminal(local_entries.get(path)))
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
