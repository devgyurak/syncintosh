# SPDX-License-Identifier: AGPL-3.0-or-later
"""The JSONC format adapter: parse to a tree, render by minimal text edits.

JSON with comments and trailing commas is the format of VS Code's `settings.json`, and no JSON
library round-trips it: `json` drops the comments and `json5`/`jsonc` normalizes the text. So
this adapter does the two halves itself (``AGENTS.md`` §2.5):

* **parse** is a small hand-written scanner that accepts `//` and `/* */` comments and a trailing
  comma, records every value's source span, and rejects a **duplicate key** — the one hazard a
  comment-tolerant parser must not swallow, because two definitions of one key make "the value
  of key K" ill-defined.
* **render** edits the *original* text at the spans that actually changed. A changed value
  rewrites only that value's span; an added key is inserted after the sibling the remote had
  before it; a removed key takes its line, comma, and same-line comment with it, leaving the
  comment above it. Indentation, trailing-comma style, newline style, and a BOM are detected in
  the original and kept.

The merge itself is not this module's job: it returns a :class:`~syncintosh.core.tree.Tree` and
takes one back, and the engine decides what changed (``core/merge.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from syncintosh.core.errors import ContentParseError
from syncintosh.core.tree import Array, EmptyObject, KeyPath, Node, Object, Scalar, ScalarKind, Tree

# Whitespace the scanner skips; a BOM is treated as leading trivia so spans stay absolute.
_WHITESPACE = " \t\r\n\ufeff"

# JSON number grammar (RFC 8259). A bare `01` or `.5` matches a prefix and then fails the caller's
# separator check, which is the correct "not a number" verdict.
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")

_LITERALS: dict[str, tuple[ScalarKind, bool | None]] = {
    "true": ("bool", True),
    "false": ("bool", False),
    "null": ("null", None),
}

# What may follow a member value on its own line: an optional comma, an optional trailing comment.
_MEMBER_TAIL = re.compile(r"[ \t]*,?[ \t]*(?://[^\n]*)?[ \t]*")


@dataclass(frozen=True)
class _Span:
    """A half-open ``[start, end)`` range in the source text."""

    start: int
    end: int


@dataclass(frozen=True)
class _ScalarNode:
    kind: ScalarKind
    value: str | int | float | bool | None
    span: _Span


@dataclass(frozen=True)
class _ArrayNode:
    elements: tuple[_AstNode, ...]
    span: _Span


@dataclass(frozen=True)
class _MemberNode:
    key: str
    value: _AstNode
    span: _Span


@dataclass(frozen=True)
class _ObjectNode:
    members: tuple[_MemberNode, ...]
    span: _Span


_AstNode = _ScalarNode | _ArrayNode | _ObjectNode


class _Parser:
    """A JSONC scanner/parser that records source spans."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0

    def parse(self) -> _AstNode:
        self._skip()
        node = self._value()
        self._skip()
        if self._pos != len(self._text):
            raise ContentParseError(f"unexpected trailing content at offset {self._pos}")
        return node

    def _skip(self) -> None:
        text = self._text
        length = len(text)
        while self._pos < length:
            char = text[self._pos]
            if char in _WHITESPACE:
                self._pos += 1
            elif text.startswith("//", self._pos):
                newline = text.find("\n", self._pos)
                self._pos = length if newline == -1 else newline
            elif text.startswith("/*", self._pos):
                end = text.find("*/", self._pos + 2)
                if end == -1:
                    raise ContentParseError("unterminated block comment")
                self._pos = end + 2
            else:
                return

    def _value(self) -> _AstNode:
        if self._pos >= len(self._text):
            raise ContentParseError("unexpected end of input")
        char = self._text[self._pos]
        if char == "{":
            return self._object()
        if char == "[":
            return self._array()
        if char == '"':
            return self._string()
        return self._literal()

    def _string(self) -> _ScalarNode:
        start = self._pos
        end = self._scan_string_end()
        try:
            value = json.loads(self._text[start:end])
        except json.JSONDecodeError as exc:
            raise ContentParseError(f"invalid string literal: {exc}") from exc
        self._pos = end
        return _ScalarNode("str", value, _Span(start, end))

    def _key(self) -> tuple[str, _Span]:
        node = self._string()
        value = node.value
        if not isinstance(value, str):
            raise ContentParseError("an object key must be a string")
        return value, node.span

    def _scan_string_end(self) -> int:
        text = self._text
        length = len(text)
        pos = self._pos + 1
        while pos < length:
            char = text[pos]
            if char == "\\":
                pos += 2
                continue
            if char == '"':
                return pos + 1
            pos += 1
        raise ContentParseError("unterminated string")

    def _literal(self) -> _ScalarNode:
        start = self._pos
        for token, (kind, value) in _LITERALS.items():
            if self._text.startswith(token, start):
                self._pos = start + len(token)
                return _ScalarNode(kind, value, _Span(start, self._pos))
        match = _NUMBER.match(self._text, start)
        if match is None:
            raise ContentParseError(f"invalid value at offset {start}")
        token = match.group(0)
        self._pos = match.end()
        kind: ScalarKind = "float" if any(char in token for char in ".eE") else "int"
        try:
            value = json.loads(token)
        except json.JSONDecodeError as exc:
            raise ContentParseError(f"invalid number literal: {exc}") from exc
        return _ScalarNode(kind, value, _Span(start, self._pos))

    def _object(self) -> _ObjectNode:
        start = self._pos
        self._pos += 1
        members: list[_MemberNode] = []
        seen: set[str] = set()
        while True:
            self._skip()
            if self._pos >= len(self._text):
                raise ContentParseError("unterminated object")
            if self._text[self._pos] == "}":
                self._pos += 1
                break
            key, key_span = self._key()
            if key in seen:
                raise ContentParseError(f"duplicate key {key!r}")
            seen.add(key)
            self._skip()
            if self._pos >= len(self._text) or self._text[self._pos] != ":":
                raise ContentParseError("expected ':' after an object key")
            self._pos += 1
            self._skip()
            value = self._value()
            members.append(_MemberNode(key, value, _Span(key_span.start, value.span.end)))
            self._skip()
            if self._pos < len(self._text) and self._text[self._pos] == ",":
                self._pos += 1
                continue
            if self._pos < len(self._text) and self._text[self._pos] == "}":
                self._pos += 1
                break
            raise ContentParseError("expected ',' or '}' in an object")
        return _ObjectNode(tuple(members), _Span(start, self._pos))

    def _array(self) -> _ArrayNode:
        start = self._pos
        self._pos += 1
        elements: list[_AstNode] = []
        while True:
            self._skip()
            if self._pos >= len(self._text):
                raise ContentParseError("unterminated array")
            if self._text[self._pos] == "]":
                self._pos += 1
                break
            elements.append(self._value())
            self._skip()
            if self._pos < len(self._text) and self._text[self._pos] == ",":
                self._pos += 1
                continue
            if self._pos < len(self._text) and self._text[self._pos] == "]":
                self._pos += 1
                break
            raise ContentParseError("expected ',' or ']' in an array")
        return _ArrayNode(tuple(elements), _Span(start, self._pos))


def _tree_of(node: _AstNode) -> Tree:
    entries: dict[KeyPath, Node] = {}
    _flatten(node, (), entries)
    return Tree.create(entries)


def _flatten(node: _AstNode, path: KeyPath, entries: dict[KeyPath, Node]) -> None:
    if isinstance(node, _ScalarNode):
        entries[path] = Scalar(node.kind, node.value)
        return
    if isinstance(node, _ArrayNode):
        entries[path] = Array(tuple(_to_node(element) for element in node.elements))
        return
    if not node.members:
        entries[path] = EmptyObject()
        return
    for member in node.members:
        _flatten(member.value, (*path, member.key), entries)


def _to_node(node: _AstNode) -> Node:
    if isinstance(node, _ScalarNode):
        return Scalar(node.kind, node.value)
    if isinstance(node, _ArrayNode):
        return Array(tuple(_to_node(element) for element in node.elements))
    return Object(tuple((member.key, _to_node(member.value)) for member in node.members))


@dataclass(frozen=True)
class _Edit:
    """Replace ``[start, end)`` with ``text``; a zero-width range is an insertion."""

    start: int
    end: int
    text: str


class JsoncAdapter:
    """A `ContentAdapter` over JSONC that renders by editing the original text."""

    def parse(self, text: str) -> Tree:
        """Parse JSONC into a tree, raising `ContentParseError` on malformed input."""
        return _tree_of(_Parser(text).parse())

    def render(self, tree: Tree, template: str) -> str:
        """Render `tree` by editing `template`; unchanged content returns the bytes verbatim."""
        root = _Parser(template).parse()
        if _tree_of(root) == tree:
            return template
        return _Diff(tree, root, template).apply()


class _Diff:
    """Computes the minimal edits that turn the template's AST into the merged tree."""

    def __init__(self, tree: Tree, root: _AstNode, text: str) -> None:
        self._source = text
        self._root = root
        self._leaves = dict(tree.entries)
        self._kids: dict[KeyPath, list[str]] = {}
        for path in tree:
            for index in range(len(path)):
                keys = self._kids.setdefault(path[:index], [])
                if path[index] not in keys:
                    keys.append(path[index])
        self._indent, self._newline = _detect_style(text, root)

    def apply(self) -> str:
        edits: list[_Edit] = []
        self._render(self._root, (), edits)
        return _apply_edits(self._source, edits)

    def _children(self, path: KeyPath) -> list[str]:
        return self._kids.get(path, [])

    def _render(self, node: _AstNode, path: KeyPath, edits: list[_Edit]) -> None:
        if self._matches(node, path):
            return
        children = self._children(path)
        if not children:
            leaf = self._leaves.get(path)
            if leaf is not None:
                edits.append(_Edit(node.span.start, node.span.end, _leaf_text(leaf)))
            return
        if not isinstance(node, _ObjectNode) or "\n" not in self._source[node.span.start : node.span.end]:
            # An object the tree wants here, or an inline object: replacing it is the only safe
            # edit, because a one-line object has no per-member line to splice.
            edits.append(_Edit(node.span.start, node.span.end, self._subtree(path, len(path))))
            return
        self._object(node, path, edits)

    def _matches(self, node: _AstNode, path: KeyPath) -> bool:
        children = self._children(path)
        if children:
            if not isinstance(node, _ObjectNode) or len(node.members) != len(children):
                return False
            by_key = {member.key: member for member in node.members}
            return all(key in by_key and self._matches(by_key[key].value, (*path, key)) for key in children)
        leaf = self._leaves.get(path)
        return leaf is not None and _same(leaf, node)

    def _object(self, node: _ObjectNode, path: KeyPath, edits: list[_Edit]) -> None:
        desired = self._children(path)
        by_key = {member.key: member for member in node.members}
        trailing = _has_trailing_comma(self._source, node)
        pending: list[str] = []
        anchor: _MemberNode | None = None
        for key in desired:
            member = by_key.get(key)
            if member is None:
                pending.append(self._member_line((*path, key), len(path)))
                continue
            if pending:
                self._insert(edits, anchor, node, pending, at_end=False, trailing=trailing)
                pending = []
            anchor = member
            self._render(member.value, (*path, key), edits)
        if pending:
            self._insert(edits, anchor, node, pending, at_end=True, trailing=trailing)
        for index, member in enumerate(node.members):
            if member.key not in desired:
                self._remove(edits, node, member, index)

    def _insert(
        self,
        edits: list[_Edit],
        anchor: _MemberNode | None,
        node: _ObjectNode,
        pending: list[str],
        *,
        at_end: bool,
        trailing: bool,
    ) -> None:
        last = len(pending) - 1
        block = "".join(
            member + ("," if index < last or not at_end or trailing else "") + self._newline
            for index, member in enumerate(pending)
        )
        if anchor is None:
            position = _line_start(self._source, node.span.end - 1)
        else:
            if _comma_after(self._source, anchor) is None:
                # The anchor is now followed by a member: it needs the comma it never had.
                edits.append(_Edit(anchor.value.span.end, anchor.value.span.end, ","))
            position = _next_line_start(self._source, anchor.value.span.end)
        edits.append(_Edit(position, position, block))

    def _remove(self, edits: list[_Edit], node: _ObjectNode, member: _MemberNode, index: int) -> None:
        text = self._source
        comma = _comma_after(text, member)
        line_start = _line_start(text, member.span.start)
        line_end = _line_end(text, member.value.span.end)
        alone = text[line_start : member.span.start].strip() == "" and _MEMBER_TAIL.fullmatch(
            text[member.value.span.end : line_end]
        )
        if alone:
            edits.append(_Edit(line_start, _next_line_start(text, member.value.span.end), ""))
        else:
            end = comma + 1 if comma is not None else member.value.span.end
            edits.append(_Edit(member.span.start, end, ""))
        if comma is None and index > 0:
            prev_comma = _comma_after(text, node.members[index - 1])
            if prev_comma is not None:
                # Removing the last member would leave the previous one with a dangling comma.
                edits.append(_Edit(prev_comma, prev_comma + 1, ""))

    def _member_line(self, path: KeyPath, level: int) -> str:
        key = path[-1]
        return self._indent * (level + 1) + json.dumps(key, ensure_ascii=False) + ": " + self._subtree(path, level + 1)

    def _subtree(self, path: KeyPath, level: int) -> str:
        children = self._children(path)
        if not children:
            leaf = self._leaves.get(path)
            return "{}" if leaf is None else _leaf_text(leaf)
        newline = self._newline
        lines = [
            self._indent * (level + 1)
            + json.dumps(key, ensure_ascii=False)
            + ": "
            + self._subtree((*path, key), level + 1)
            for key in children
        ]
        return "{" + newline + ("," + newline).join(lines) + newline + self._indent * level + "}"


def _same(leaf: Node, node: _AstNode) -> bool:
    if isinstance(leaf, Scalar) and isinstance(node, _ScalarNode):
        return leaf.kind == node.kind and leaf.value == node.value
    if isinstance(leaf, Array) and isinstance(node, _ArrayNode):
        return len(leaf.elements) == len(node.elements) and all(
            _same_value(element, ast) for element, ast in zip(leaf.elements, node.elements, strict=True)
        )
    if isinstance(leaf, Object) and isinstance(node, _ObjectNode):
        return _same_object(leaf.entries, node.members)
    return isinstance(leaf, EmptyObject) and isinstance(node, _ObjectNode) and not node.members


def _same_value(leaf: Node, node: _AstNode) -> bool:
    if isinstance(leaf, Object) and isinstance(node, _ObjectNode):
        return _same_object(leaf.entries, node.members)
    return _same(leaf, node)


def _same_object(entries: tuple[tuple[str, Node], ...], members: tuple[_MemberNode, ...]) -> bool:
    if len(entries) != len(members):
        return False
    by_key = {member.key: member for member in members}
    return all(key in by_key and _same_value(value, by_key[key].value) for key, value in entries)


def _leaf_text(node: Node) -> str:
    if isinstance(node, Scalar):
        return json.dumps(node.value, ensure_ascii=False)
    if isinstance(node, Array):
        return "[" + ", ".join(_leaf_text(element) for element in node.elements) + "]"
    if isinstance(node, Object):
        entries = (json.dumps(key, ensure_ascii=False) + ": " + _leaf_text(value) for key, value in node.entries)
        return "{" + ", ".join(entries) + "}"
    return "{}"


def _detect_style(text: str, root: _AstNode) -> tuple[str, str]:
    """The indent unit and newline style, read from the first indented member if there is one."""
    newline = "\r\n" if "\r\n" in text else "\n"
    indent = "  "
    if isinstance(root, _ObjectNode) and root.members:
        leading = text[_line_start(text, root.members[0].span.start) : root.members[0].span.start]
        if leading and leading.strip() == "":
            indent = leading
    return indent, newline


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _line_end(text: str, pos: int) -> int:
    newline = text.find("\n", pos)
    return len(text) if newline == -1 else newline


def _next_line_start(text: str, pos: int) -> int:
    newline = text.find("\n", pos)
    return len(text) if newline == -1 else newline + 1


def _comma_after(text: str, member: _MemberNode) -> int | None:
    """The offset of the comma that follows `member`'s value, or `None` when it is the last."""
    pos = member.value.span.end
    length = len(text)
    while pos < length:
        if text[pos] in " \t":
            pos += 1
        elif text.startswith("//", pos):
            newline = text.find("\n", pos)
            pos = length if newline == -1 else newline
        else:
            break
    return pos if pos < length and text[pos] == "," else None


def _has_trailing_comma(text: str, node: _ObjectNode) -> bool:
    return bool(node.members) and _comma_after(text, node.members[-1]) is not None


def _apply_edits(text: str, edits: list[_Edit]) -> str:
    """Apply the edits in offset order, skipping any that overlap an earlier one."""
    pieces: list[str] = []
    cursor = 0
    for edit in sorted(edits, key=lambda item: item.start):
        if edit.start < cursor:
            continue
        pieces.append(text[cursor : edit.start])
        pieces.append(edit.text)
        cursor = edit.end
    pieces.append(text[cursor:])
    return "".join(pieces)


__all__ = ["JsoncAdapter"]
