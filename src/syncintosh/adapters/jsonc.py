# SPDX-License-Identifier: AGPL-3.0-or-later
"""The JSONC format adapter: parse to a tree, render by minimal text edits.

JSON with comments and trailing commas is the format of VS Code's `settings.json`, and no JSON
library round-trips it: `json` drops the comments and `json5`/`jsonc` normalizes the text. So
this adapter does the two halves itself (``AGENTS.md`` §2.5):

* **parse** is a small hand-written scanner that accepts `//` and `/* */` comments and a trailing
  comma, records every value's source span, and rejects a **duplicate key** — the one hazard a
  comment-tolerant parser must not swallow, because two definitions of one key make "the value
  of key K" ill-defined. An empty or comment-only file is the empty object; a malformed literal
  that a Python int/nesting limit would reject as `ValueError`/`RecursionError` is reported as a
  parse failure, never a crash.
* **render** edits the *original* text at the spans that actually changed. A changed value
  rewrites only that value's span; an added key is inserted after the sibling the remote had
  before it; a removed key takes its line, comma, and same-line comment with it, leaving the
  comment above it. Indentation, trailing-comma style, newline style, and a BOM are detected in
  the original and kept. An array is one value, so a change to it (or to an object inside it) is
  rendered by reserializing the whole array; an object that is not laid out member-per-line is
  reserialized whole too, because a one-line object has no per-member line to splice.

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
_SPACES = " \t"

# JSON number grammar (RFC 8259). A bare `01` or `.5` matches a prefix and then fails the caller's
# separator check, which is the correct "not a number" verdict.
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")

_LITERALS: dict[str, tuple[ScalarKind, bool | None]] = {
    "true": ("bool", True),
    "false": ("bool", False),
    "null": ("null", None),
}


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
        if self._pos >= len(self._text):
            # An empty or comment-only file is the empty object, not a parse failure: a settings
            # file with nothing in it is `{}`, and treating it as malformed would make every
            # both-sides change a whole-file conflict.
            return _ObjectNode((), _Span(0, len(self._text)))
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
        value = json.loads(token)
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
        try:
            return _tree_of(_Parser(text).parse())
        except (ValueError, RecursionError) as exc:
            # A literal Python int/nesting limit (huge integer, very deep document) is a
            # malformed input for this format, not a crash the caller has to know about.
            raise ContentParseError(str(exc)) from exc

    def render(self, tree: Tree, template: str) -> str:
        """Render `tree` by editing `template`; unchanged content returns the bytes verbatim."""
        try:
            return self._render(tree, template)
        except (ValueError, RecursionError) as exc:
            raise ContentParseError(str(exc)) from exc

    def _render(self, tree: Tree, template: str) -> str:
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
            if isinstance(leaf, EmptyObject) and isinstance(node, _ObjectNode) and node.members:
                # The tree says this object is empty but the template still has members. Take them
                # out in place — line by line, or by splice — rather than rewriting the span, so a
                # comment above a member and the object's own layout survive (`_rewrite` would
                # refuse the comment anyway).
                if _clean_multiline_object(self._source, node):
                    self._object(node, path, edits)
                    return
                if self._splice_inline(node, path, edits):
                    return
            if leaf is not None:
                self._rewrite(node, _leaf_text(leaf), edits)
            return
        if not isinstance(node, _ObjectNode):
            # The tree wants an object here but the template has something else: rewrite it.
            self._rewrite(node, self._subtree(path, len(path)), edits)
            return
        if _clean_multiline_object(self._source, node):
            self._object(node, path, edits)
            return
        if self._splice_inline(node, path, edits):
            return
        # A one-line object with a comment inside it cannot be spliced member by member, and
        # re-flowing it would move that comment; rewrite the whole value (which refuses when the
        # span holds a comment, so a comment is never silently dropped).
        self._rewrite(node, self._subtree(path, len(path)), edits)

    def _inline_inner(self, node: _ObjectNode) -> str | None:
        """The text between a one-line object's braces when it can be spliced, else `None`.

        A splice is only safe when nothing but members, commas, and whitespace sits between the
        braces. A newline means the member-per-line path applies instead; a comment inside would
        be moved or swallowed by a splice, so the caller falls back to a whole-value rewrite (and
        the comment guard refuses that rather than dropping the comment).
        """
        if "\n" in self._source[node.span.start : node.span.end]:
            return None
        if node.span.end - node.span.start < 2:
            # An empty or comment-only template parses as a synthetic object with no braces at
            # all; nothing can be spliced, and the caller's whole-value rewrite builds a document.
            return None
        if self._source[node.span.start] != "{" or self._source[node.span.end - 1] != "}":
            return None
        if _has_comment(self._source, node.span.start + 1, node.span.end - 1):
            return None
        return self._source[node.span.start + 1 : node.span.end - 1]

    def _splice_inline(self, node: _ObjectNode, path: KeyPath, edits: list[_Edit]) -> bool:
        """Add and remove members of a **one-line** object in place; False when it splices nothing.

        Re-flowing a one-line object to member-per-line changes the file far beyond what the merge
        decided: a projection that re-attaches a stripped key would rewrite the object's layout,
        and a round-trip check that compares bytes would then never agree again. Splicing keeps
        the layout, and the two directions are inverse:

        * an added member is appended just before the closing brace, carrying the separator its
          own removal takes away again;
        * a removed member takes the separator *before* it — or, when it is the first member, the
          one after it — so no separator is left dangling and none is lost.
        """
        if self._inline_inner(node) is None:
            return False
        desired = self._children(path)
        wanted = set(desired)
        by_key = {member.key: member for member in node.members}
        kept = [member for member in node.members if member.key in wanted]
        for member in node.members:
            if member.key in wanted:
                self._render(member.value, (*path, member.key), edits)
        removals: list[_Edit] = []
        self._splice_removals(node, wanted, removals)
        edits.extend(removals)
        added = [key for key in desired if key not in by_key]
        if added:
            lead = self._inline_gap(node, kept)
            # With no survivor to take a style from (`{}`, or every member removed) the lead is
            # empty and the members still need a separator between them.
            join = lead or ", "
            members = [self._inline_member((*path, key)) for key in added]
            # The block goes after everything that survives — and after any removal, never inside
            # one. The padding before `}` sits between the last member and the brace, so a run that
            # emptied the object ends past that member's value; an insert placed there would be
            # inside the deleted range, and the edit merge drops an edit that starts before the
            # cursor. `{"a": 1, "b": 2}` handed `{c}` would then render as `{}` with no `c` in it.
            natural = node.members[-1].value.span.end if node.members else node.span.end - 1
            position = max([natural, *(edit.end for edit in removals)])
            edits.append(_Edit(position, position, lead + join.join(members)))
        return True

    def _splice_removals(self, node: _ObjectNode, wanted: set[str], edits: list[_Edit]) -> None:
        """Delete every member the tree no longer has, one contiguous run at a time."""
        runs: list[list[int]] = []
        for index, member in enumerate(node.members):
            if member.key in wanted:
                continue
            if runs and runs[-1][-1] == index - 1:
                runs[-1].append(index)
                continue
            runs.append([index])
        for run in runs:
            first, last = node.members[run[0]], node.members[run[-1]]
            if run[0] == 0 and run[-1] == len(node.members) - 1:
                # The run is the whole object: take the padding and any trailing comma with it, so
                # `{"a": 1,}` becomes `{}` rather than `{,}` — which does not parse, and would make
                # the file `unprojectable` on every sync from then on.
                edits.append(_Edit(first.span.start, node.span.end - 1, ""))
                continue
            if run[0] == 0:
                # The object opened with the run: keep the padding after `{`, drop the run, and
                # drop the separator that used to follow it.
                edits.append(_Edit(first.span.start, last.value.span.end, ""))
                edits.append(_Edit(last.value.span.end, node.members[run[-1] + 1].span.start, ""))
                continue
            # The run sits between members: drop the separator before it plus the run itself, so
            # the members on either side end up adjacent.
            edits.append(_Edit(node.members[run[0] - 1].value.span.end, last.value.span.end, ""))

    def _inline_gap(self, node: _ObjectNode, kept: list[_MemberNode]) -> str:
        """The separator to put before a member appended to a one-line object.

        Taken from two members that are **adjacent in the original**, so what sits between them is
        a separator and nothing else (a comment inside a one-line object is refused outright, so the
        only other thing it could hold is whitespace). Slicing between the first two *survivors*
        instead copies whatever was removed between them back into the file — the member comes back
        to life and the render stops matching the tree. The removal takes this separator away with
        it, so the choice never has to be undone exactly.
        """
        if not kept:
            # Every member is being removed, so the object is left empty and the added member must
            # not carry a separator: `{}, "a": 1}` would not parse.
            return ""
        if len(node.members) < 2:
            # A single member leaves no gap to copy the file's spacing from, but the added member
            # still needs a separator between it and what is already there.
            return ", "
        return self._source[node.members[0].value.span.end : node.members[1].span.start]

    def _inline_member(self, path: KeyPath) -> str:
        return json.dumps(path[-1], ensure_ascii=False) + ": " + self._inline_subtree(path)

    def _inline_subtree(self, path: KeyPath) -> str:
        children = self._children(path)
        if not children:
            leaf = self._leaves.get(path)
            return "{}" if leaf is None else _leaf_text(leaf)
        entries = ", ".join(self._inline_member((*path, key)) for key in children)
        return "{" + entries + "}"

    def _past_block_comment(self, after: int, position: int) -> int:
        """A position that is not inside a block comment opened after `after`.

        A block comment can open on a member's line and close on a later one, so the next line after
        that member is still inside it: splicing a member there puts it inside the comment, where it
        parses as nothing — or, if the text after the comment is what closes it, as a broken file.
        Either way the render stops matching the tree, and a merge reports a conflict it had already
        resolved. The block goes after the comment instead, which leaves the comment where it was,
        attached to the member it documents.
        """
        text = self._source
        cursor = after
        while True:
            opening = text.find("/*", cursor)
            if opening == -1 or opening >= position:
                return position
            closing = text.find("*/", opening + 2)
            if closing == -1:
                # An unterminated comment means the text never parsed, so this cannot be reached.
                return position
            if closing + 2 > position:
                return _next_line_start(text, closing + 2)
            cursor = closing + 2

    def _open_position(self, node: _ObjectNode) -> int:
        """Where a block that opens the object goes: above the first member's own comment.

        A comment on the line directly above the first member documents *that* member, so inserting
        between the two would move the comment onto the inserted key. Walking up over `//` lines
        keeps it attached to what it describes. A blank line, or any other line, stops the walk —
        and so does a block comment, which the walk cannot count lines within: splicing inside one
        would produce a file that does not parse, and refusing the insert is worse than leaving the
        layout alone.
        """
        if not node.members:
            return _line_start(self._source, node.span.end - 1)
        position = _line_start(self._source, node.members[0].span.start)
        while position > 0:
            previous = _line_start(self._source, position - 1)
            line = self._source[previous : position - 1]
            # A line inside a block comment can start with `//`, so a line that carries a block
            # marker stops the walk: stepping over it would put the block inside the comment, where
            # it parses as nothing and the render stops matching the tree.
            if not line.strip().startswith("//") or "/*" in line or "*/" in line:
                break
            position = previous
        return position

    def _rewrite(self, node: _AstNode, text: str, edits: list[_Edit]) -> None:
        """Replace a whole value, refusing when that would silently drop a comment.

        Rewriting a span is only safe while it holds no comment: the merged tree has no comments,
        so a re-parse check cannot notice them gone. Refusing turns a comment loss into a
        conflict, which the caller surfaces rather than publishing a stripped file.
        """
        if _has_comment(self._source, node.span.start, node.span.end):
            raise ContentParseError("cannot rewrite a value that contains a comment")
        edits.append(_Edit(node.span.start, node.span.end, text))

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
        index_of = {member.key: index for index, member in enumerate(node.members)}
        trailing = _has_trailing_comma(self._source, node)

        pending: list[str] = []
        anchor: _MemberNode | None = None
        anchor_index: int | None = None
        insert_anchors: set[int] = set()
        for key in desired:
            member = by_key.get(key)
            if member is None:
                pending.append(self._member_line((*path, key), len(path)))
                continue
            if pending:
                self._insert(edits, anchor, node, pending, at_end=False, trailing=trailing)
                if anchor_index is not None:
                    insert_anchors.add(anchor_index)
                pending = []
            anchor = member
            anchor_index = index_of[key]
            self._render(member.value, (*path, key), edits)
        if pending:
            self._insert(edits, anchor, node, pending, at_end=True, trailing=trailing)
            if anchor_index is not None:
                insert_anchors.add(anchor_index)

        self._remove_deleted(node, set(desired), insert_anchors, trailing, edits)

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
            # Nothing precedes the block *in the tree*, so it opens the object: before the first
            # member the template has. Reaching for the closing brace instead would put the block
            # after the last member — which is neither the order the tree asked for nor valid
            # JSONC, because the member before it would then lack the comma the block's own
            # trailing comma was meant to complete. An object with no members has no first line,
            # so a genuinely empty `{ ... }` still takes the block just above its brace.
            position = self._open_position(node)
        else:
            if _comma_after(self._source, anchor) is None:
                # The anchor is now followed by a member: it needs the comma it never had.
                edits.append(_Edit(anchor.value.span.end, anchor.value.span.end, ","))
            position = self._past_block_comment(
                anchor.value.span.end, _next_line_start(self._source, anchor.value.span.end)
            )
        edits.append(_Edit(position, position, block))

    def _remove_deleted(
        self,
        node: _ObjectNode,
        wanted: set[str],
        insert_anchors: set[int],
        trailing: bool,
        edits: list[_Edit],
    ) -> None:
        """Delete each removed member's line, and the dangling comma a tail deletion leaves."""
        text = self._source
        deleted = [index for index, member in enumerate(node.members) if member.key not in wanted]
        if not deleted:
            return
        survivors = [index for index in range(len(node.members)) if index not in deleted]
        last_survivor = survivors[-1] if survivors else None
        tail_deleted = (len(node.members) - 1) in deleted
        # When the last member is removed and the style has no trailing comma, the comma after
        # the new last member dangles — unless an inserted member will follow it and needs that
        # very comma as its separator.
        drop_comma = tail_deleted and not trailing and last_survivor is not None and last_survivor not in insert_anchors
        for index in deleted:
            member = node.members[index]
            start = _line_start(text, member.span.start)
            end = _line_end_outside_comment(text, start, _next_line_start(text, member.value.span.end))
            edits.append(_Edit(start, end, ""))
        if drop_comma and last_survivor is not None:
            comma = _comma_after(text, node.members[last_survivor])
            if comma is not None:
                edits.append(_Edit(comma, comma + 1, ""))

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
    """Compare an object **in order**: an array of objects compares as a whole, so a reordering
    of the keys inside one is a real change, matching `core.tree.Object`'s own equality."""
    if len(entries) != len(members):
        return False
    return all(
        key == member.key and _same_value(value, member.value)
        for (key, value), member in zip(entries, members, strict=True)
    )


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


def _clean_multiline_object(text: str, node: _ObjectNode) -> bool:
    """True when every member is alone on its line and the closing brace is on its own line.

    Only then can a member be spliced by line; anything else (one line, two members on a line,
    `}` sharing a member's line) is reserialized whole instead of risking a broken edit. A
    member-less object may still be spliced when it is a real, brace-delimited `{ ... }`; a
    synthesized empty document (whitespace- or comment-only) has no braces and is rewritten.
    """
    if "\n" not in text[node.span.start : node.span.end]:
        return False
    if not node.members:
        return text[node.span.start] == "{"
    for member in node.members:
        if text[_line_start(text, member.span.start) : member.span.start].strip() != "":
            return False
        if not _member_tail_clean(text, member.value.span.end):
            return False
    close = node.span.end - 1
    return text[_line_start(text, close) : close].strip() == ""


def _member_tail_clean(text: str, value_end: int) -> bool:
    """True when what follows a value to end of line is only a comma, comments, and whitespace."""
    length = len(text)
    pos = value_end
    while True:
        spaces = _skip_spaces(text, pos)
        if text.startswith("/*", spaces):
            end = text.find("*/", spaces + 2)
            if end == -1:
                return False
            pos = end + 2
            continue
        if spaces < length and text[spaces] == ",":
            pos = spaces + 1
            continue
        pos = spaces
        break
    if text.startswith("//", pos):
        newline = text.find("\n", pos)
        pos = length if newline == -1 else newline
    pos = _skip_spaces(text, pos)
    while pos < length and text[pos] == "\r":
        pos += 1
    return pos >= length or text[pos] == "\n"


def _skip_spaces(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in _SPACES:
        pos += 1
    return pos


def _has_comment(text: str, start: int, end: int) -> bool:
    """True when the ``[start, end)`` span holds a `//` or `/* */` comment, string-aware."""
    pos = start
    while pos < end:
        char = text[pos]
        if char == '"':
            pos += 1
            while pos < end:
                if text[pos] == "\\":
                    pos += 2
                    continue
                if text[pos] == '"':
                    pos += 1
                    break
                pos += 1
        elif text.startswith("//", pos) or text.startswith("/*", pos):
            return True
        else:
            pos += 1
    return False


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _next_line_start(text: str, pos: int) -> int:
    newline = text.find("\n", pos)
    return len(text) if newline == -1 else newline + 1


def _line_end_outside_comment(text: str, start: int, end: int) -> int:
    """The end of a line deletion, extended past a block comment the line opens.

    A member is deleted together with its line, its comma, and a same-line comment. A block comment
    can open on that line and close on a later one, and cutting between the two leaves the comment's
    remainder in the file — text the parser then reads as a member, or as a broken object. The
    deletion runs to the end of the comment's last line instead, which keeps the file parseable.
    """
    cursor = start
    limit = end
    while True:
        opening = text.find("/*", cursor)
        if opening == -1 or opening >= limit:
            return limit
        closing = text.find("*/", opening + 2)
        if closing == -1:
            return limit
        if closing + 2 > limit:
            limit = _next_line_start(text, closing + 2)
            continue
        cursor = closing + 2


def _comma_after(text: str, member: _MemberNode) -> int | None:
    """The offset of the comma that follows `member`'s value, or `None` when there is none.

    A block comment between the value and its comma is skipped, so `1 /* n */,` reads as
    followed by a comma; a line comment ends the search (a comma cannot follow it on the line).
    """
    pos = member.value.span.end
    length = len(text)
    while pos < length:
        if text[pos] in _SPACES:
            pos += 1
        elif text.startswith("/*", pos):
            end = text.find("*/", pos + 2)
            if end == -1:
                return None
            pos = end + 2
        elif text.startswith("//", pos):
            return None
        elif text[pos] == ",":
            return pos
        else:
            return None
    return None


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
