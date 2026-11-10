# SPDX-License-Identifier: AGPL-3.0-or-later
"""The property-list adapter: an XML plist as a typed tree, rendered back in the one canonical form.

Why it differs from the JSONC and XML adapters: a plist has no comments to keep and its dict order carries no
meaning, so there is nothing a text edit must preserve. The adapter therefore parses with `plistlib` and renders the
canonical XML of `core/plist_xml.py` — the form the defaults-domain store also writes, so the two never disagree on
bytes. `render_edits` still returns line-level edits (difflib over the template) so `core/projection.py` can see
where a projection changed the text and undo only that.

The tree keeps plist types apart: `<integer>1</integer>` is an `int` and `<real>1.0</real>` a `float`, which are
different values; `<data>` is a `data` scalar (base64 text) and `<date>` a `date` scalar (ISO-8601 UTC text), each
compared whole. A binary plist, a root that is not a dict, and a DOCTYPE with an internal subset are refused
(`ContentParseError`), so this adapter never sees input it cannot write back.
"""

from __future__ import annotations

import base64
import difflib
from datetime import datetime
from typing import TYPE_CHECKING

from syncintosh.core.errors import ContentParseError
from syncintosh.core.plist_xml import dump_plist, parse_plist
from syncintosh.core.ports import TextEdit
from syncintosh.core.tree import Array as TreeArray
from syncintosh.core.tree import EmptyObject, Node, Object, Scalar, Tree

if TYPE_CHECKING:
    from collections.abc import Mapping

    from syncintosh.core.keyed_array import ArrayRule
    from syncintosh.core.ports import Verbatim
    from syncintosh.core.tree import KeyPath

_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _node_of(value: object) -> Node:
    """A plist value as a tree node; a dict inside an array stays whole (`Object`)."""
    if isinstance(value, dict):
        return Object(tuple((str(key), _node_of(child)) for key, child in value.items()))
    if isinstance(value, list):
        return TreeArray(tuple(_node_of(item) for item in value))
    # `bool` first: it is an `int` in Python and a different plist type.
    if isinstance(value, bool):
        return Scalar("bool", value)
    if isinstance(value, int):
        return Scalar("int", value)
    if isinstance(value, float):
        return Scalar("float", value)
    if isinstance(value, str):
        return Scalar("str", value)
    if isinstance(value, bytes):
        return Scalar("data", base64.b64encode(value).decode("ascii"))
    if isinstance(value, datetime):
        # plistlib hands back a naive datetime that is UTC by the format's definition.
        return Scalar("date", value.strftime(_DATE_FORMAT))
    raise ContentParseError(f"unsupported property-list value: {type(value).__name__}")


def _entries(value: dict[str, object], path: KeyPath, entries: dict[KeyPath, Node]) -> None:
    if not value:
        entries[path] = EmptyObject()
        return
    for key, child in value.items():
        if isinstance(child, dict):
            _entries(child, (*path, key), entries)
        else:
            entries[(*path, key)] = _node_of(child)


def _value_of(node: Node) -> object:
    """The plist value a node stands for, so a kind survives the trip back to XML."""
    if isinstance(node, EmptyObject):
        return {}
    if isinstance(node, Object):
        return {key: _value_of(child) for key, child in node.entries}
    if isinstance(node, TreeArray):
        return [_value_of(item) for item in node.elements]
    if node.kind == "data" and isinstance(node.value, str):
        return base64.b64decode(node.value)
    if node.kind == "date" and isinstance(node.value, str):
        # Naive, as `plistlib` itself reads and writes it: the format defines a `<date>` as UTC.
        return datetime.strptime(node.value, _DATE_FORMAT)
    return node.value


def _document(tree: Tree) -> dict[str, object]:
    document: dict[str, object] = {}
    for path, node in tree.entries.items():
        if not path:
            continue
        parent = document
        for segment in path[:-1]:
            child = parent.setdefault(segment, {})
            if not isinstance(child, dict):
                raise ContentParseError("a key is both a value and a dict")
            parent = child
        parent[path[-1]] = _value_of(node)
    return document


class PlistAdapter:
    """A `ContentAdapter` over XML property lists that renders the canonical form."""

    def parse(self, text: str) -> Tree:
        """Parse an XML plist, raising `ContentParseError` on binary, a non-dict root, or an internal subset."""
        try:
            value = parse_plist(text.encode("utf-8"))
        except ValueError as exc:
            raise ContentParseError(str(exc)) from exc
        entries: dict[KeyPath, Node] = {}
        _entries(value, (), entries)
        return Tree.create(entries)

    def render(
        self,
        tree: Tree,
        template: str,
        arrays: Mapping[KeyPath, ArrayRule] | None = None,
        verbatim: Verbatim | None = None,
    ) -> str:
        """The canonical XML of `tree`.

        `template`, `arrays` and `verbatim` are accepted for the port and unused: the canonical form is one text per
        tree, so there is no layout to keep, no array identity to pair by, and no spelling to copy.
        """
        try:
            return dump_plist(_document(tree)).decode("utf-8")
        except ValueError as exc:
            raise ContentParseError(str(exc)) from exc

    def render_edits(
        self,
        tree: Tree,
        template: str,
        arrays: Mapping[KeyPath, ArrayRule] | None = None,
        verbatim: Verbatim | None = None,
    ) -> tuple[TextEdit, ...]:
        """Line-level edits that turn `template` into :meth:`render`, none when it already is that text."""
        rendered = self.render(tree, template, arrays, verbatim)
        before = template.splitlines(keepends=True)
        after = rendered.splitlines(keepends=True)
        starts = [0]
        for line in before:
            starts.append(starts[-1] + len(line))
        matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
        return tuple(
            TextEdit(starts[i1], starts[i2], "".join(after[j1:j2]))
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        )


__all__ = ["PlistAdapter"]
