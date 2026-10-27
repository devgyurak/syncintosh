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
from difflib import SequenceMatcher

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
        # Every comment this parse skips, in order. The renderer edits the *text*, so it needs to
        # know where its syntax is: re-finding `//` or `/*` with a search is what read a documented
        # glob inside a comment as syntax, and deleted the member after it.
        self.comments: list[_Span] = []

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
            start = self._pos
            if char in _WHITESPACE:
                self._pos += 1
            elif text.startswith("//", self._pos):
                newline = text.find("\n", self._pos)
                self._pos = length if newline == -1 else newline
                # The comment's text stops before the line ending: the `\r` of a CRLF pair is part of
                # the line ending and not of the comment, and a span that swallowed it made every
                # CRLF line with a trailing `//` comment read as a line ending outside a comment.
                self.comments.append(
                    _Span(start, self._pos - 1 if self._pos > start and text[self._pos - 1] == "\r" else self._pos)
                )
            elif text.startswith("/*", self._pos):
                end = text.find("*/", self._pos + 2)
                if end == -1:
                    raise ContentParseError("unterminated block comment")
                self._pos = end + 2
                self.comments.append(_Span(start, self._pos))
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
        parser = _Parser(template)
        root = parser.parse()
        if _tree_of(root) == tree:
            return template
        return _Diff(tree, root, template, _Comments(template, tuple(parser.comments))).apply()


class _Diff:
    """Computes the minimal edits that turn the template's AST into the merged tree."""

    def __init__(self, tree: Tree, root: _AstNode, text: str, comments: _Comments) -> None:
        self._source = text
        self._comments = comments
        self._root = root
        self._leaves = dict(tree.entries)
        self._kids: dict[KeyPath, list[str]] = {}
        for path in tree:
            for index in range(len(path)):
                keys = self._kids.setdefault(path[:index], [])
                if path[index] not in keys:
                    keys.append(path[index])
        self._indent, self._newline = _detect_style(text, root, comments)

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
                if _clean_multiline_object(self._source, node, self._comments):
                    self._object(node, path, edits)
                    return
                if self._splice_inline(node, path, edits):
                    return
            if isinstance(leaf, Array) and isinstance(node, _ArrayNode) and self._splice_array(node, leaf, edits):
                return
            if leaf is not None:
                self._rewrite(node, _leaf_text(leaf), edits)
            return
        if not isinstance(node, _ObjectNode):
            # The tree wants an object here but the template has something else: rewrite it.
            self._rewrite(node, self._subtree(path, len(path)), edits)
            return
        if _clean_multiline_object(self._source, node, self._comments):
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
        if self._comments.overlaps(node.span.start + 1, node.span.end - 1):
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

    def _open_position(self, node: _ObjectNode) -> int:
        """Where a block that opens the object goes: above the first member's own comment.

        A comment on the line directly above the first member documents *that* member, so inserting
        between the two would move the comment onto the inserted key. The walk is `_open_block_position`,
        shared with the array splice, so the two cannot drift apart.
        """
        if not node.members:
            return _line_start(self._source, node.span.end - 1)
        return _open_block_position(self._source, self._comments, node.members[0].span.start)

    def _rewrite(self, node: _AstNode, text: str, edits: list[_Edit]) -> None:
        """Replace a whole value, refusing when that would silently drop a comment.

        Rewriting a span is only safe while it holds no comment: the merged tree has no comments,
        so a re-parse check cannot notice them gone. Refusing turns a comment loss into a
        conflict, which the caller surfaces rather than publishing a stripped file.
        """
        if self._comments.overlaps(node.span.start, node.span.end):
            raise ContentParseError("cannot rewrite a value that contains a comment")
        edits.append(_Edit(node.span.start, node.span.end, text))

    def _splice_array(self, node: _ArrayNode, leaf: Array, edits: list[_Edit]) -> bool:
        """Edit a one-element-per-line array by element, so untouched elements keep their comments.

        Elements the merge kept are left as they are (or, when the merge changed their value, only their
        own span is rewritten); a removed element goes with its line, its comma and a same-line comment; an
        added element goes above the next kept element's own comments. A comma is set from the final order:
        every element but the last gets one, and the last gets one only when the template had a trailing
        comma. Returns False for a layout this does not handle, so the caller rewrites the whole value.
        """
        if not node.elements or not _clean_multiline_array(self._source, node, self._comments):
            return False
        text = self._source
        template = [_to_node(element) for element in node.elements]
        wanted = list(leaf.elements)
        slots: list[_Slot] = []
        deleted: list[int] = []
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, template, wanted, autojunk=False).get_opcodes():
            if tag == "equal":
                slots.extend(_Kept(index, None) for index in range(i1, i2))
                continue
            for old_offset, new_offset in _pair_block(template[i1:i2], wanted[j1:j2]):
                if old_offset is None and new_offset is not None:
                    slots.append(_Added(wanted[j1 + new_offset]))
                elif old_offset is not None and new_offset is None:
                    deleted.append(i1 + old_offset)
                elif old_offset is not None and new_offset is not None:
                    slots.append(_Kept(i1 + old_offset, wanted[j1 + new_offset]))

        trailing = _comma_after(self._comments, node.elements[-1]) is not None
        indent = text[_line_start(text, node.elements[0].span.start) : node.elements[0].span.start]
        multiline = any(
            isinstance(element, _ObjectNode) and "\n" in text[element.span.start : element.span.end]
            for element in node.elements
        )
        unit = _array_unit(text, node, indent) or self._indent
        last = len(slots) - 1
        for position, slot in enumerate(slots):
            if isinstance(slot, _Kept) and slot.replacement is not None:
                # A changed element keeps its span; a comment inside it refuses the rewrite (a conflict).
                self._rewrite(
                    node.elements[slot.index], self._element_text(slot.replacement, indent, multiline, unit), edits
                )
            wants = position < last or trailing
            if isinstance(slot, _Kept):
                element = node.elements[slot.index]
                comma = _comma_at(self._comments, element.span.end)
                if wants and comma is None:
                    edits.append(_Edit(element.span.end, element.span.end, ","))
                if not wants and comma is not None:
                    edits.append(_Edit(comma, comma + 1, ""))

        # Insertions go before the deletions below, so at a shared offset the new lines come first.
        pending: list[str] = []
        anchor: int | None = None
        for position, slot in enumerate(slots):
            if isinstance(slot, _Added):
                wants = position < last or trailing
                pending.append(
                    indent
                    + self._element_text(slot.value, indent, multiline, unit)
                    + ("," if wants else "")
                    + self._newline
                )
                continue
            if pending:
                start = _open_block_position(text, self._comments, node.elements[slot.index].span.start)
                edits.append(_Edit(start, start, "".join(pending)))
                pending = []
            anchor = slot.index
        if pending:
            if anchor is None:
                start = _open_block_position(text, self._comments, node.elements[0].span.start)
            else:
                end = node.elements[anchor].span.end
                start = _past_block_comment(self._comments, end, _next_line_start(text, end))
            edits.append(_Edit(start, start, "".join(pending)))

        for index in deleted:
            # Only the element's own line goes (with its comma and a same-line comment). The comment lines above it
            # stay where they are: which element they documented cannot be known from the text alone, and silently
            # deleting a note is worse than leaving it attached to the element below.
            element = node.elements[index]
            start = _line_start(text, element.span.start)
            end = _line_end_outside_comment(self._comments, start, _next_line_start(text, element.span.end))
            edits.append(_Edit(start, end, ""))
        return True

    def _element_text(self, node: Node, indent: str, multiline: bool, unit: str) -> str:
        """An array element's text: an object spread over lines, indented in the array's own unit."""
        if multiline and isinstance(node, Object) and node.entries:
            inner = indent + unit
            members = [
                inner + json.dumps(key, ensure_ascii=False) + ": " + self._element_text(value, inner, True, unit)
                for key, value in node.entries
            ]
            return "{" + self._newline + ("," + self._newline).join(members) + self._newline + indent + "}"
        return _leaf_text(node)

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
        wanted = set(desired)
        by_key = {member.key: member for member in node.members}
        index_of = {member.key: index for index, member in enumerate(node.members)}
        trailing = _has_trailing_comma(self._comments, node)

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
                self._insert(edits, anchor, node, pending, wanted=wanted, trailing=trailing)
                if anchor_index is not None:
                    insert_anchors.add(anchor_index)
                pending = []
            anchor = member
            anchor_index = index_of[key]
            self._render(member.value, (*path, key), edits)
        if pending:
            self._insert(edits, anchor, node, pending, wanted=wanted, trailing=trailing)
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
        wanted: set[str],
        trailing: bool,
    ) -> None:
        """Insert a run of newly added members, after `anchor` or before the first existing member.

        Whether the block needs a *trailing* comma is decided by what follows it **in the file among
        the members that survive**, because a removed member is no longer there to be separated from:
        reading the original neighbour put a comma back into `{"psFormat": "b", "imagesFormat": "table"}`
        when the second member was deleted, and Docker's strict JSON parser refuses that file.
        """
        if anchor is None:
            follows = any(member.key in wanted for member in node.members)
        else:
            following = node.members[node.members.index(anchor) + 1 :]
            follows = any(member.key in wanted for member in following)
        last = len(pending) - 1
        block = "".join(
            member + ("," if index < last or follows or trailing else "") + self._newline
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
            if _comma_after(self._comments, anchor) is None:
                # The anchor is now followed by a member: it needs the comma it never had.
                edits.append(_Edit(anchor.value.span.end, anchor.value.span.end, ","))
            position = _past_block_comment(
                self._comments, anchor.value.span.end, _next_line_start(self._source, anchor.value.span.end)
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
            end = _line_end_outside_comment(self._comments, start, _next_line_start(text, member.value.span.end))
            edits.append(_Edit(start, end, ""))
        if drop_comma and last_survivor is not None:
            comma = _comma_after(self._comments, node.members[last_survivor])
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


def _detect_style(text: str, root: _AstNode, comments: _Comments) -> tuple[str, str]:
    """The indent unit and newline style, read from the file's own lines.

    The newline is read from the text *outside* the comments. A raw `\\r\\n` cannot be inside a JSON
    string (a JSON string cannot hold a raw newline at all), so a comment is where a pasted Windows
    snippet lands — and reading it there made the whole file CRLF, so every inserted line was
    terminated the other way from the lines around it.
    """
    newline = "\r\n" if "\r\n" in comments.without_comments(0, len(text)) else "\n"
    indent = "  "
    if isinstance(root, _ObjectNode) and root.members:
        leading = text[_line_start(text, root.members[0].span.start) : root.members[0].span.start]
        if leading and leading.strip() == "":
            indent = leading
    return indent, newline


def _clean_multiline_object(text: str, node: _ObjectNode, comments: _Comments) -> bool:
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
        if not _member_tail_clean(comments, member.value.span.end):
            return False
    close = node.span.end - 1
    return text[_line_start(text, close) : close].strip() == ""


def _member_tail_clean(comments: _Comments, value_end: int) -> bool:
    """True when what follows a value to end of line is only a comma, comments, and whitespace."""
    text = comments.text
    line_end = text.find("\n", value_end)
    line_end = len(text) if line_end == -1 else line_end
    # The comments are masked out with the parse's own spans, so a `, // hides **/*.map` tail reads
    # as a comma rather than as text that only looks like syntax.
    return comments.without_comments(value_end, line_end).strip() in ("", ",")


@dataclass(frozen=True)
class _Comments:
    """The comment spans one parse found, so nothing later has to look for syntax again.

    Every question the renderer asks about comments — does this span hold one, does one straddle this
    offset, what is the next real character, is this line only a comment — is answered from the spans
    the *parser* recorded, never by searching the text. A search cannot tell a comment from a string,
    and a settings file documents path globs in its comments.
    """

    text: str
    spans: tuple[_Span, ...]

    def overlaps(self, start: int, end: int) -> bool:
        """True when a comment starts, ends, or lies inside ``[start, end)``."""
        return any(span.start < end and span.end > start for span in self.spans)

    def closes_after(self, start: int, limit: int) -> int | None:
        """The end of a comment opening in ``[start, limit)`` and closing at or after `limit`."""
        for span in self.spans:
            if start <= span.start < limit and span.end > limit:
                return span.end
        return None

    def next_meaningful(self, position: int) -> int:
        """The offset of the first character after `position` that is not in a comment or a space."""
        cursor = position
        while True:
            while cursor < len(self.text) and self.text[cursor] in _WHITESPACE:
                cursor += 1
            span = next((item for item in self.spans if item.start <= cursor < item.end), None)
            if span is None:
                return cursor
            cursor = span.end

    def without_comments(self, start: int, end: int) -> str:
        """The text of ``[start, end)`` with every comment in it blanked out."""
        pieces: list[str] = []
        cursor = start
        for span in self.spans:
            if span.end <= cursor or span.start >= end:
                continue
            pieces.append(self.text[cursor : max(cursor, span.start)])
            cursor = min(end, span.end)
        pieces.append(self.text[cursor:end])
        return "".join(pieces)

    def is_own_line_comment(self, line_start: int, line_end: int) -> bool:
        """True when the line holds nothing but whitespace and a `//` comment that ends it.

        The comment has to *start* the line. A line can be the inside of a block comment and still
        look like one — `/* fuzz` above, `still */ // note` here — and stepping the insertion over it
        would put the new member inside the comment, where it parses as nothing.
        """
        first = line_start
        while first < line_end and self.text[first] in _SPACES:
            first += 1
        return any(
            span.start == first and span.end >= line_end - 1 and self.text.startswith("//", span.start)
            for span in self.spans
        )


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _next_line_start(text: str, pos: int) -> int:
    newline = text.find("\n", pos)
    return len(text) if newline == -1 else newline + 1


def _past_block_comment(comments: _Comments, after: int, position: int) -> int:
    """A position that is not inside a comment opened after `after`.

    Splicing a member inside a comment makes it parse as nothing — or, when the comment's closing
    text is what follows, breaks the file — and the render then stops matching the tree, so a merge
    reports a conflict it had already resolved. The block goes after the comment instead, which
    leaves the comment where it was, attached to the member it documents.
    """
    closing = comments.closes_after(after, position)
    return position if closing is None else _next_line_start(comments.text, closing)


def _line_end_outside_comment(comments: _Comments, start: int, end: int) -> int:
    """The end of a line deletion, extended past a comment the line opens.

    A member is deleted together with its line, its comma, and a same-line comment, so cutting at the
    line's end would leave the remainder of a comment that closes below it in the file — text the
    parser then reads as a member, or as a broken object.
    """
    limit = end
    while True:
        closing = comments.closes_after(start, limit)
        if closing is None:
            return limit
        limit = _next_line_start(comments.text, closing)


def _comma_after(comments: _Comments, member: _MemberNode | _AstNode) -> int | None:
    """The offset of the comma that follows a value, or `None` when there is none.

    A comment between the value and its comma is skipped, so `1 /* n */,` reads as followed by a
    comma; a line comment ends the search, because a comma cannot follow it on the line.
    """
    value = member.value if isinstance(member, _MemberNode) else member
    return _comma_at(comments, value.span.end)


def _comma_at(comments: _Comments, end: int) -> int | None:
    """The offset of the comma that follows a value ending at `end`, or `None` (see `_comma_after`)."""
    position = comments.next_meaningful(end)
    text = comments.text
    return position if position < len(text) and text[position] == "," else None


@dataclass(frozen=True)
class _Kept:
    """A template element the merged array keeps, with its new value when the merge changed it."""

    index: int
    replacement: Node | None


@dataclass(frozen=True)
class _Added:
    """A value the merged array has and the template does not, placed where it falls in order."""

    value: Node


_Slot = _Kept | _Added


def _clean_multiline_array(text: str, node: _ArrayNode, comments: _Comments) -> bool:
    """True when the array spans lines, its closing bracket is alone, and each element starts its own line.

    Only then can elements be spliced by line. An array on one line, or one with two elements on a line, or
    a comment before an element on its line, is rewritten whole instead, as before.
    """
    if "\n" not in text[node.span.start : node.span.end]:
        return False
    close = node.span.end - 1
    if text[_line_start(text, close) : close].strip() != "":
        return False
    return all(
        text[_line_start(text, element.span.start) : element.span.start].strip() == ""
        and _member_tail_clean(comments, element.span.end)
        for element in node.elements
    )


def _open_block_position(text: str, comments: _Comments, start: int) -> int:
    """Where a block placed before the element starting at `start` goes: above that element's own comments.

    The same walk `_open_position` makes for an object member: a comment on the line above documents the
    element below it, so the block must not separate them.
    """
    position = _line_start(text, start)
    while position > 0:
        previous = _line_start(text, position - 1)
        if not comments.is_own_line_comment(previous, position - 1):
            break
        position = previous
    return position


# How alike two array elements must be before the splice rewrites one in place rather than deleting the old
# and inserting the new: strictly more than half of the larger element's key/value pairs. A two-field binding
# that shares only its command, or only its key, is not the same binding.
_PAIR_THRESHOLD = 0.5


def _similarity(old: Node, new: Node) -> float:
    """How much of `new` is `old` again, as the share of key/value pairs both objects hold (0 for other values)."""
    if isinstance(old, Object) and isinstance(new, Object):
        if not old.entries and not new.entries:
            return 1.0
        shared = len(set(old.entries) & set(new.entries))
        return shared / max(len(old.entries), len(new.entries))
    return 1.0 if old == new else 0.0


def _pair_block(old: list[Node], new: list[Node]) -> list[tuple[int | None, int | None]]:
    """Pair a replaced run in order: `(old, new)` pairs, `(old, None)` deletions and `(None, new)` additions.

    An old element is paired only with a later new element it resembles, so a binding removed and a different one
    put in its place are a deletion and an addition, not one rewrite that would carry the old element's comments onto
    the new one.
    """
    result: list[tuple[int | None, int | None]] = []
    cursor = 0
    for old_offset, element in enumerate(old):
        # The most alike later candidate, not the first one that clears the bar: a first match can take a binding
        # that a better one should have, and its comment would land on the wrong binding.
        scored = [(_similarity(element, new[offset]), offset) for offset in range(cursor, len(new))]
        best_score, match = max(scored, key=lambda item: (item[0], -item[1]), default=(0.0, None))
        if match is None or best_score <= _PAIR_THRESHOLD:
            result.append((old_offset, None))
            continue
        result.extend((None, offset) for offset in range(cursor, match))
        result.append((old_offset, match))
        cursor = match + 1
    result.extend((None, offset) for offset in range(cursor, len(new)))
    return result


def _array_unit(text: str, node: _ArrayNode, indent: str) -> str | None:
    """The indent step the array's own multi-line objects use, read from their first member line.

    A root array has no members of its own to read the step from, so the step comes from the elements: the
    difference between an element's line and its first member's line. `None` when no element shows one.
    """
    for element in node.elements:
        if not isinstance(element, _ObjectNode) or not element.members:
            continue
        first = element.members[0].span.start
        member_indent = text[_line_start(text, first) : first]
        # A one-line element puts `{ ` before its first member; that is not an indent step, so it is skipped.
        if member_indent.strip() == "" and member_indent.startswith(indent) and len(member_indent) > len(indent):
            return member_indent[len(indent) :]
    return None


def _has_trailing_comma(comments: _Comments, node: _ObjectNode) -> bool:
    return bool(node.members) and _comma_after(comments, node.members[-1]) is not None


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
