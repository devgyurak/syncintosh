# SPDX-License-Identifier: AGPL-3.0-or-later
"""The typed tree the merge engine reasons about.

A settings file is a nested document, but a three-way merge only ever decides *keys*. This
module flattens a parsed document into a :class:`Tree` — an ordered map from a **key path** (a
tuple of object keys) to a leaf — so the engine can compare one key at a time without a
line-level merge that would break JSON/XML syntax (``AGENTS.md`` §2.5, §2.9).

Three shapes keep a flat map honest:

* :class:`Scalar` carries its JSON **kind** (``null``/``bool``/``int``/``float``/``str``)
  beside the value, so ``true`` and ``1`` and ``1`` and ``1.0`` are three different values, not
  one; a merge must not treat a rewritten type as "unchanged".
* :class:`Array` is a leaf compared **whole**. An array has no key paths inside it: a positional
  edit is not a key edit, and a partial array merge would need an index identity the format does
  not have. :class:`Object` carries an object that lives inside an array for the same reason —
  compared whole, never flattened.
* :class:`EmptyObject` marks ``{}``, which flattens to *no* paths and would otherwise vanish;
  the flatten → render round-trip needs it to survive.

``ABSENT`` is the "this key is not here" sentinel, which the merge keeps distinct from a JSON
``null`` (a key explicitly set to null is present). Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

# A key path is a tuple of object keys; an array contributes no segment because it is compared
# whole. The empty tuple is the document root.
KeyPath = tuple[str, ...]

# The JSON primitive kinds, serialized verbatim so they are a `Literal` and never an `Enum`.
ScalarKind = Literal["null", "bool", "int", "float", "str"]


@dataclass(frozen=True)
class Scalar:
    """A JSON primitive together with its kind.

    The kind is part of the identity, so ``True`` (``bool``) and ``1`` (``int``) are different
    leaves — Python's own ``==`` would fold them together — and ``1`` and ``1.0`` differ too.
    """

    kind: ScalarKind
    value: str | int | float | bool | None


@dataclass(frozen=True)
class Array:
    """A whole array, compared as one value; its elements never become key paths.

    An element may itself be an array or an :class:`Object`, both compared by value with the
    same kind distinction a top-level leaf gets.
    """

    elements: tuple[Node, ...]


@dataclass(frozen=True)
class Object:
    """An object *inside* an array, kept whole because array positions are not key paths."""

    entries: tuple[tuple[str, Node], ...]


@dataclass(frozen=True)
class EmptyObject:
    """``{}`` as a leaf, so flattening an empty object does not erase it."""


# A value that can sit at a key path, or inside an array.
Node = Scalar | Array | Object | EmptyObject


@dataclass(frozen=True)
class Absent:
    """The type of the ``ABSENT`` sentinel: a key that is not present on a side.

    A frozen dataclass with no fields, so every instance compares equal; the module-level
    ``ABSENT`` is the single instance the merge uses.
    """

    def __repr__(self) -> str:
        return "ABSENT"


# "This key is not here." Distinct from a JSON `null`, which is a present key with a null value.
ABSENT: Final = Absent()

# What the merge may see at a path: a real node, or the absent sentinel.
Leaf = Node | Absent


@dataclass(frozen=True)
class Tree:
    """An ordered ``key path -> leaf`` map for one document.

    Built only through :meth:`create`, which freezes the mapping (``AGENTS.md`` §2.6, §2.7), so
    a tree cannot be assembled field by field or mutated after construction. Iteration order is
    the document order the parse produced, which the render step needs to place an added key.
    """

    _entries: Mapping[KeyPath, Node]

    @classmethod
    def create(cls, entries: Mapping[KeyPath, Node]) -> Tree:
        """Freeze an ordered path map. The single constructor (``AGENTS.md`` §2.7)."""
        return cls(_entries=MappingProxyType(dict(entries)))

    @property
    def entries(self) -> Mapping[KeyPath, Node]:
        """The ordered path map. Read-only."""
        return self._entries

    def __hash__(self) -> int:
        # A frozen boundary object must really be hashable, but a `MappingProxyType` is not; hash
        # the items instead so a `Tree` can be a set member or a mapping key.
        return hash(frozenset(self._entries.items()))

    def __iter__(self) -> Iterator[KeyPath]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def value(self, path: KeyPath) -> Leaf:
        """The node at `path`, or ``ABSENT`` when the key is not in the tree."""
        return self._entries.get(path, ABSENT)

    def to_value(self) -> object:
        """Rebuild the nested JSON value this tree came from.

        The inverse of :func:`tree_from_value`, used by a caller that needs the plain document
        back (the ``merge`` command before a format adapter exists).
        """
        if () in self._entries:
            return _node_to_value(self._entries[()])
        return _object_to_value(self._entries, ())


def tree_from_value(value: object) -> Tree:
    """Flatten a parsed JSON value (``dict``/``list``/scalar) into a :class:`Tree`.

    Objects become key paths, arrays and scalars become leaves. Building a tree from *values*
    rather than from paths keeps the two merge inputs comparable no matter who parsed them.
    """
    entries: dict[KeyPath, Node] = {}
    _flatten(value, (), entries)
    return Tree.create(entries)


def scalar(value: object) -> Scalar:
    """Wrap a JSON primitive, tagging it with its kind (``bool`` before ``int``)."""
    if value is None:
        return Scalar("null", None)
    if isinstance(value, bool):
        return Scalar("bool", value)
    if isinstance(value, int):
        return Scalar("int", value)
    if isinstance(value, float):
        return Scalar("float", value)
    if isinstance(value, str):
        return Scalar("str", value)
    raise TypeError(f"not a JSON scalar: {type(value).__name__}")


def _flatten(value: object, path: KeyPath, entries: dict[KeyPath, Node]) -> None:
    if isinstance(value, dict):
        if not value:
            entries[path] = EmptyObject()
            return
        for key, child in value.items():
            _flatten(child, (*path, str(key)), entries)
        return
    if isinstance(value, list):
        entries[path] = Array(tuple(_to_node(item) for item in value))
        return
    entries[path] = scalar(value)


def _to_node(value: object) -> Node:
    if isinstance(value, dict):
        return Object(tuple((str(key), _to_node(child)) for key, child in value.items()))
    if isinstance(value, list):
        return Array(tuple(_to_node(item) for item in value))
    return scalar(value)


def _node_to_value(node: Node) -> object:
    if isinstance(node, Scalar):
        return node.value
    if isinstance(node, Array):
        return [_node_to_value(element) for element in node.elements]
    if isinstance(node, Object):
        return {key: _node_to_value(child) for key, child in node.entries}
    return {}


def _object_to_value(entries: Mapping[KeyPath, Node], parent: KeyPath) -> dict[str, object]:
    result: dict[str, object] = {}
    for path, node in entries.items():
        if len(path) <= len(parent) or path[: len(parent)] != parent:
            continue
        key = path[len(parent)]
        if key in result:
            continue
        if len(path) == len(parent) + 1:
            result[key] = _node_to_value(node)
        else:
            result[key] = _object_to_value(entries, (*parent, key))
    return result


__all__ = [
    "ABSENT",
    "Absent",
    "Array",
    "EmptyObject",
    "KeyPath",
    "Leaf",
    "Node",
    "Object",
    "Scalar",
    "ScalarKind",
    "Tree",
    "scalar",
    "tree_from_value",
]
