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

from collections.abc import Callable, Iterable, Mapping
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

    # Plaintext settings, so a repr shows neither the bindings nor the identity: a `when` clause can name an internal
    # host. A caller that needs the group's name prints `element_label`.
    element: ElementId = field(repr=False)
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

    base_ids = [identity for identity, _ in base_items]
    local_ids = [identity for identity, _ in local_items]
    remote_ids = [identity for identity, _ in remote_items]
    chosen: list[_Item] = []
    conflicts: list[GroupConflict] = []
    # A conflicting group's bindings are set aside from the shared order: whether the conflict is settled decides their
    # order, not the order the two sides shared. A settled group's bindings are placed from the side they were settled
    # to; a proposed group's replacement takes the place the group held.
    set_aside: set[ElementId] = set()
    owners: dict[ElementId, str] = {}
    replacements: list[
        tuple[frozenset[ElementId], frozenset[ElementId], frozenset[ElementId], tuple[ElementId, ...]]
    ] = []
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
            pick = (picks or {}).get(group)
            settled = _settle_group(group, pick, local_group, remote_group, rule)
            if settled is None:
                picked = local_group
                set_aside.update(identity for identity, _ in (*local_group, *remote_group))
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
                identities = [identity for identity, _ in settled]
                set_aside.update(identity for identity, _ in (*local_group, *remote_group))
                set_aside.update(identities)
                if pick == "local" or pick == "remote":
                    owners.update((identity, pick) for identity in identities)
                else:
                    owners.update((identity, "proposal") for identity in identities)
                    local_members = frozenset(identity for identity, _ in local_group)
                    remote_members = frozenset(identity for identity, _ in remote_group)
                    base_members = frozenset(identity for identity, _ in base_group)
                    replacements.append((local_members, remote_members, base_members, tuple(identities)))
        chosen.extend(picked)

    ordered = _ordered(base_ids, local_ids, remote_ids, chosen, owners, replacements, frozenset(set_aside))
    if ordered is None:
        return None
    return KeyedMerge(merged=Array(tuple(node for _, node in ordered)), conflicts=tuple(conflicts))


def element_identity(element: Node, rule: ArrayRule) -> ElementId | None:
    """An element's identity under `rule`, or ``None`` when it cannot be identified.

    The adapter pairs a rewritten element with its replacement by this identity, so the pairing is the one the
    merge decides, not a guess from how the two look.
    """
    return _identity(element, rule)


def element_group(identity: ElementId, rule: ArrayRule) -> ElementId:
    """The group an identity falls in under `rule`: the same helper the merge groups by, so the two cannot disagree."""
    return _group_of(identity, rule)


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
    references = [node for _, node in (*local_group, *remote_group)]
    return tuple((identity, _keys_like(node, references)) for identity, node in items)


def _keys_like(node: Node, references: list[Node]) -> Node:
    """A proposed binding with the key order of a binding it stands in for, when one has the same keys.

    A model that returns keys sorted (or in any order) would otherwise reorder the user's own binding, and
    object equality is order-sensitive, so the change would show on every later diff.
    """
    if not isinstance(node, Object):
        return node
    keys = {key for key, _ in node.entries}
    for reference in references:
        if isinstance(reference, Object) and {key for key, _ in reference.entries} == keys:
            values = dict(node.entries)
            return Object(tuple((key, values[key]) for key, _ in reference.entries))
    return node


def accepts_group(leaf: Leaf, rule: ArrayRule, group: ElementId) -> bool:
    """True when `leaf` is an array this rule identifies and every element in it belongs to `group`.

    The same test a proposal must pass to settle a group, offered so a caller can refuse a proposal before
    the merge sees it. An empty array is accepted: it proposes that the group has no bindings.
    """
    if not isinstance(leaf, Array):
        return False
    items = _items(leaf, rule)
    return items is not None and all(_group_of(identity, rule) == group for identity, _ in items)


def group_leaves(leaf: Leaf, rule: ArrayRule) -> dict[ElementId, Leaf] | None:
    """Every group of an array leaf, each as its leaf, in one pass; ``None`` when the rule cannot identify it.

    The one-pass form of `group_leaf` for a caller that compares many groups: it indexes the array once rather
    than scanning it for each group.
    """
    if not isinstance(leaf, Array):
        return None
    items = _items(leaf, rule)
    if items is None:
        return None
    return {group: _leaf(members) for group, members in _groups(items, rule).items()}


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
    base_ids: list[ElementId],
    local_ids: list[ElementId],
    remote_ids: list[ElementId],
    chosen: list[_Item],
    owners: dict[ElementId, str],
    replacements: list[tuple[frozenset[ElementId], frozenset[ElementId], frozenset[ElementId], tuple[ElementId, ...]]],
    set_aside: frozenset[ElementId],
) -> list[_Item] | None:
    """The merged elements in order, or ``None`` when the two sides reordered the shared ones differently.

    The elements that survive, exist on all three sides, and are not set aside carry the order ("anchors"). A set-aside
    binding is placed from the side its conflict was settled to (`owners`), in that side's order, and the rest follow
    the local, then the remote, sequence. So settling a conflict never turns the array into a whole-array conflict.
    """
    node_of = dict(chosen)
    local_set, remote_set = set(local_ids), set(remote_ids)
    anchors = {
        identity
        for identity in base_ids
        if identity in node_of and identity in local_set and identity in remote_set and identity not in set_aside
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

    local_sequence, remote_sequence = list(local_ids), list(remote_ids)
    # The base as the replacements already made have changed it, so a later group is placed after an earlier
    # group's proposal rather than after the base binding that group used to hold.
    effective_base = list(base_ids)
    for local_old, remote_old, base_old, new_identities in replacements:
        local_sequence = _spine(local_sequence, local_old, new_identities, effective_base, base_old)
        remote_sequence = _spine(remote_sequence, remote_old, new_identities, effective_base, base_old)
        effective_base = _with_replacement(effective_base, base_old, new_identities)

    ordered = list(anchored)
    placed = set(ordered)

    def place(sequence: list[ElementId], wanted: Callable[[ElementId], bool]) -> None:
        for position, identity in enumerate(sequence):
            if identity not in node_of or identity in placed or not wanted(identity):
                continue
            ordered.insert(_slot(sequence, position, ordered, placed), identity)
            placed.add(identity)

    # Each side's own settled bindings first, in that side's order; a proposal is placed from the local sequence.
    place(local_sequence, lambda identity: owners.get(identity) in ("local", "proposal"))
    place(remote_sequence, lambda identity: owners.get(identity) == "remote")
    place(local_sequence, lambda identity: True)
    place(remote_sequence, lambda identity: True)
    ordered = _in_chosen_order(ordered, owners, {"local": local_sequence, "remote": remote_sequence})
    return [(identity, node_of[identity]) for identity in ordered]


def _in_chosen_order(
    ordered: list[ElementId], owners: dict[ElementId, str], sequences: dict[str, list[ElementId]]
) -> list[ElementId]:
    """Each settled side's bindings put into that side's order, inside the places they already hold.

    Placement already follows the chosen side, so this changes nothing when placement is right; it is the guarantee
    that a settled group reads in the order its side had, whatever the placement did with the other bindings.
    """
    result = list(ordered)
    for side, sequence in sequences.items():
        members = {identity for identity, owner in owners.items() if owner == side}
        places = [index for index, identity in enumerate(result) if identity in members]
        wanted = [identity for identity in sequence if identity in members]
        if len(places) == len(wanted):
            for place, identity in zip(places, wanted, strict=True):
                result[place] = identity
    return result


def _with_replacement(
    effective: list[ElementId], old: frozenset[ElementId], new: tuple[ElementId, ...]
) -> list[ElementId]:
    """The base order with a group's bindings replaced by its proposal, at the place the group first held."""
    position = next((index for index, identity in enumerate(effective) if identity in old), None)
    if position is None:
        return effective
    return [*effective[:position], *new, *[identity for identity in effective[position:] if identity not in old]]


def _spine(
    sequence: list[ElementId],
    old: frozenset[ElementId],
    new: tuple[ElementId, ...],
    base_ids: list[ElementId],
    base_old: frozenset[ElementId],
) -> list[ElementId]:
    """`sequence` with a group's identities replaced by a proposal's, where the group held on this side.

    When the group is absent on this side (the side removed it), the proposal goes where the group sat in the base,
    after the nearest base binding this side still has. A group that neither the base nor this side ever held (both
    sides added a binding under it, and the person proposed one) has no place to go, so it goes at the end.
    """
    position = next((index for index, identity in enumerate(sequence) if identity in old), None)
    if position is not None:
        return [*sequence[:position], *new, *[identity for identity in sequence[position:] if identity not in old]]
    base_position = next((index for index, identity in enumerate(base_ids) if identity in base_old), None)
    if base_position is None:
        return [*sequence, *new]
    present = set(sequence)
    for before in reversed(base_ids[:base_position]):
        if before in present:
            index = sequence.index(before) + 1
            return [*sequence[:index], *new, *sequence[index:]]
    for after in base_ids[base_position + 1 :]:
        if after in present:
            index = sequence.index(after)
            return [*sequence[:index], *new, *sequence[index:]]
    return [*sequence, *new]


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
