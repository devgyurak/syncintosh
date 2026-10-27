# SPDX-License-Identifier: AGPL-3.0-or-later
"""Element-level three-way merge for an array of objects, such as VS Code's `keybindings.json`.

A plain array is one leaf compared whole (`core/tree.py`), so two machines that each add a
different keybinding conflict on the entire file. This module merges such an array element by
element. Each element has an **identity** (the fields an :class:`ArrayRule` names, such as `key`,
`command` and `when`) and belongs to a **group** (the subset of those fields that decides which
elements one three-way decision covers, such as `key` and `when`).

The decision is made per group, with the same three-way rule a key path uses:

* a group changed on one side takes that side;
* a group both sides changed to the same contents is taken once;
* a group both sides changed to different contents is a conflict on that group, and only on it.

Elements are placed by identity. The local order is kept; an element one side added goes after its
nearest preceding sibling on that side. When both sides reorder the elements they share differently,
the order cannot be decided, so the caller falls back to the whole-array compare.

A few arrays cannot be read this way, and :func:`merge_keyed_array` returns ``None`` for them, which
the caller treats as a whole-array conflict: an element that is not an object, a field that is missing
without being optional or is not a string, two elements with one identity, or the reorder above.

Stdlib only, and no I/O (``AGENTS.md`` §2.1, §2.2).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from syncintosh.core.resolution import Side
from syncintosh.core.tree import ABSENT, Array, ElementId, Leaf, Node, Object, Scalar

# An element of an array together with its identity.
_Item = tuple[ElementId, Node]


@dataclass(frozen=True)
class ArrayRule:
    """Which fields identify an array's elements, which of them must be present, and how they group.

    ``identity`` lists the fields that make an element's identity. ``required`` is the subset that
    must be present as a string; an optional field that is absent (such as ``when``) is a value of its
    own, distinct from an empty string. ``group`` is the subset that decides which elements one
    three-way decision covers. Built only through :meth:`create`, which checks that the subsets hold.
    """

    identity: tuple[str, ...]
    required: tuple[str, ...]
    group: tuple[str, ...]

    @classmethod
    def create(cls, *, identity: tuple[str, ...], required: tuple[str, ...], group: tuple[str, ...]) -> ArrayRule:
        """Build a rule, refusing one whose ``required`` or ``group`` names a field not in ``identity``."""
        if not set(required) <= set(identity) or not set(group) <= set(identity):
            raise ValueError("required and group must name identity fields")
        return cls(identity=identity, required=required, group=group)


@dataclass(frozen=True)
class GroupConflict:
    """One group both sides changed to different contents.

    ``element`` is the group's identity (the group fields only). Each side is the group's elements as
    an array, or ``ABSENT`` when that side has none in the group.
    """

    element: ElementId
    # The bindings are plaintext settings, so a repr never shows them (AGENTS.md §4); the identity stays visible.
    base: Leaf = field(repr=False)
    local: Leaf = field(repr=False)
    remote: Leaf = field(repr=False)


@dataclass(frozen=True)
class KeyedMerge:
    """The merged array, with the groups that still conflict.

    ``merged`` keeps the local contents of each conflicting group, the same candidate a key-level
    conflict keeps, so the result is never silently one side's choice.
    """

    merged: Array = field(repr=False)
    conflicts: tuple[GroupConflict, ...]


def merge_keyed_array(
    base: Leaf,
    local: Leaf,
    remote: Leaf,
    rule: ArrayRule,
    picks: Mapping[ElementId, Side | Array] | None = None,
) -> KeyedMerge | None:
    """Merge three array leaves element by element, or ``None`` when the whole-array compare must decide.

    ``None`` covers a side that is not an array, a base that is not an array (an array newly added on
    both sides has no base to group against), an array the rule cannot identify, and an order the two
    sides changed differently. The caller records a whole-array conflict in each of those cases.

    ``picks`` settles conflicting groups by identity: a side takes that side's group, and a proposed array
    takes the proposed elements, provided every one of them belongs to that group. A pick that does not
    fit stays a conflict.
    """
    if not (isinstance(base, Array) and isinstance(local, Array) and isinstance(remote, Array)):
        return None
    base_items = _items(base, rule)
    local_items = _items(local, rule)
    remote_items = _items(remote, rule)
    if base_items is None or local_items is None or remote_items is None:
        return None

    base_groups = _groups(base_items, rule)
    local_groups = _groups(local_items, rule)
    remote_groups = _groups(remote_items, rule)

    chosen: list[_Item] = []
    conflicts: list[GroupConflict] = []
    proposed: list[list[ElementId]] = []
    for group in _union(base_groups, local_groups, remote_groups):
        base_group = base_groups.get(group, ())
        local_group = local_groups.get(group, ())
        remote_group = remote_groups.get(group, ())
        if local_group == remote_group:
            picked = local_group
        elif local_group == base_group:
            picked = remote_group
        elif remote_group == base_group:
            picked = local_group
        else:
            settled = _settle_group(group, (picks or {}).get(group), local_group, remote_group, rule)
            if settled is None:
                picked = local_group
                conflicts.append(
                    GroupConflict(
                        element=group,
                        base=_leaf(base_group),
                        local=_leaf(local_group),
                        remote=_leaf(remote_group),
                    )
                )
            else:
                picked = settled
                proposed.append([identity for identity, _ in settled])
        chosen.extend(picked)

    ordered = _ordered(base_items, local_items, remote_items, chosen, proposed)
    if ordered is None:
        return None
    return KeyedMerge(merged=Array(tuple(node for _, node in ordered)), conflicts=tuple(conflicts))


def _items(array: Array, rule: ArrayRule) -> list[_Item] | None:
    """Each element with its identity, or ``None`` when one cannot be identified or two share one."""
    items: list[_Item] = []
    for element in array.elements:
        identity = _identity(element, rule)
        if identity is None:
            return None
        items.append((identity, element))
    if len({identity for identity, _ in items}) != len(items):
        return None
    return items


def _identity(element: Node, rule: ArrayRule) -> ElementId | None:
    """An element's identity, or ``None`` when it is not an object or a required field is not a string."""
    if not isinstance(element, Object):
        return None
    fields = dict(element.entries)
    pairs: list[tuple[str, str | None]] = []
    for name in rule.identity:
        node = fields.get(name)
        if node is None:
            if name in rule.required:
                return None
            pairs.append((name, None))
        elif isinstance(node, Scalar) and node.kind == "str" and isinstance(node.value, str):
            pairs.append((name, node.value))
        else:
            return None
    return tuple(pairs)


def _groups(items: list[_Item], rule: ArrayRule) -> dict[ElementId, tuple[_Item, ...]]:
    """The items of each group, in the order they appear in the array."""
    grouped: dict[ElementId, list[_Item]] = {}
    for item in items:
        grouped.setdefault(_group_of(item[0], rule), []).append(item)
    return {group: tuple(members) for group, members in grouped.items()}


def _settle_group(
    group: ElementId,
    pick: Side | Array | None,
    local_group: tuple[_Item, ...],
    remote_group: tuple[_Item, ...],
    rule: ArrayRule,
) -> tuple[_Item, ...] | None:
    """The contents a pick gives a conflicting group, or ``None`` when it does not settle it.

    A proposed array must parse as an array this rule identifies, and every element in it must belong to
    this group; anything else is refused, so a proposal can never move a binding into another group.
    """
    if pick == "local":
        return local_group
    if pick == "remote":
        return remote_group
    if not isinstance(pick, Array):
        return None
    items = _items(pick, rule)
    if items is None or any(_group_of(identity, rule) != group for identity, _ in items):
        return None
    return tuple(items)


def accepts_group(leaf: Leaf, rule: ArrayRule, group: ElementId) -> bool:
    """True when `leaf` is an array this rule identifies and every element in it belongs to `group`.

    The same test a proposal must pass to settle a group, offered so a caller can refuse a proposal before
    the merge sees it. An empty array is accepted: it proposes that the group has no bindings.
    """
    if not isinstance(leaf, Array):
        return False
    items = _items(leaf, rule)
    return items is not None and all(_group_of(identity, rule) == group for identity, _ in items)


def group_ids(leaf: Leaf, rule: ArrayRule) -> tuple[ElementId, ...] | None:
    """The groups an array leaf holds, in first-seen order; ``None`` when the rule cannot identify it."""
    if not isinstance(leaf, Array):
        return None
    items = _items(leaf, rule)
    if items is None:
        return None
    return tuple(dict.fromkeys(_group_of(identity, rule) for identity, _ in items))


def group_leaf(leaf: Leaf, rule: ArrayRule, group: ElementId) -> Leaf:
    """The elements of an array leaf that fall in `group`, as a leaf; ``ABSENT`` when there are none.

    Used to show a group conflict's sides to a person. An array the rule cannot identify has no groups, so
    it yields ``ABSENT`` as well.
    """
    if not isinstance(leaf, Array):
        return ABSENT
    items = _items(leaf, rule)
    if items is None:
        return ABSENT
    return _leaf(tuple(item for item in items if _group_of(item[0], rule) == group))


def _group_of(identity: ElementId, rule: ArrayRule) -> ElementId:
    """The group fields of an identity, in identity order."""
    return tuple(pair for pair in identity if pair[0] in rule.group)


def _union(*maps: Mapping[ElementId, object]) -> Iterable[ElementId]:
    """Every group key across the maps, each once, in first-seen order."""
    return dict.fromkeys(group for mapping in maps for group in mapping)


def _leaf(items: tuple[_Item, ...]) -> Leaf:
    """A group's elements as an array leaf, or ``ABSENT`` when the group is empty on that side."""
    if not items:
        return ABSENT
    return Array(tuple(node for _, node in items))


def _ordered(
    base_items: list[_Item],
    local_items: list[_Item],
    remote_items: list[_Item],
    chosen: list[_Item],
    proposed: list[list[ElementId]],
) -> list[_Item] | None:
    """The merged elements in order, or ``None`` when the two sides reordered the shared ones differently.

    The elements that survive and exist on all three sides ("anchors") carry the order. Each other
    element is placed after its nearest preceding sibling on the side it came from, so local additions
    keep their local order and remote additions land where they were on the remote.
    """
    node_of = dict(chosen)
    base_ids = [identity for identity, _ in base_items]
    local_ids = [identity for identity, _ in local_items]
    remote_ids = [identity for identity, _ in remote_items]
    local_set, remote_set = set(local_ids), set(remote_ids)
    anchors = {
        identity for identity in base_ids if identity in node_of and identity in local_set and identity in remote_set
    }

    base_order = [identity for identity in base_ids if identity in anchors]
    local_order = [identity for identity in local_ids if identity in anchors]
    remote_order = [identity for identity in remote_ids if identity in anchors]
    if local_order == base_order:
        anchored = remote_order
    elif local_order == remote_order or remote_order == base_order:
        anchored = local_order
    else:
        return None

    ordered = list(anchored)
    placed = set(ordered)
    for sequence in (local_ids, remote_ids, *proposed):
        for position, identity in enumerate(sequence):
            if identity not in node_of or identity in placed:
                continue
            ordered.insert(_slot(sequence, position, ordered, placed), identity)
            placed.add(identity)
    return [(identity, node_of[identity]) for identity in ordered]


def _slot(sequence: list[ElementId], position: int, ordered: list[ElementId], placed: set[ElementId]) -> int:
    """Where an element goes: after its nearest placed predecessor, else before its nearest placed successor."""
    for before in range(position - 1, -1, -1):
        if sequence[before] in placed:
            return ordered.index(sequence[before]) + 1
    for after in range(position + 1, len(sequence)):
        if sequence[after] in placed:
            return ordered.index(sequence[after])
    return len(ordered)


__all__ = ["ArrayRule", "GroupConflict", "KeyedMerge", "merge_keyed_array"]
