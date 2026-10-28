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
  the original and kept. A multi-line array is spliced one element at a time (see
  `_splice_array`), with a rule or without one: each unchanged element keeps its own text and
  notes. A removal that would take a note from an array with no rule is refused with a fixed
  reason (`UNRULED_REMOVAL_REFUSED`), since nothing says which element the note belongs to. A
  value the splice cannot place — an inline array, an array with a comma first on its line, an
  object that is not laid out member-per-line — is reserialized whole, and refused when a
  comment inside it would be lost.

The merge itself is not this module's job: it returns a :class:`~syncintosh.core.tree.Tree` and
takes one back, and the engine decides what changed (``core/merge.py``).
"""

from __future__ import annotations

import bisect
import json
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property

from syncintosh.core.errors import ContentParseError, RenderRefused
from syncintosh.core.keyed_array import ArrayRule, element_group, element_identity
from syncintosh.core.tree import Array, ElementId, EmptyObject, KeyPath, Node, Object, Scalar, ScalarKind, Tree

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
    value: str | int | float | bool | None = field(repr=False)
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
    text: str = field(repr=False)


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

    def render(self, tree: Tree, template: str, arrays: Mapping[KeyPath, ArrayRule] | None = None) -> str:
        """Render `tree` by editing `template`; unchanged content returns the bytes verbatim.

        `arrays` names the arrays that pair an element by its identity when it is rewritten (`core/keyed_array.py`);
        an array without a rule pairs nothing, so a changed element there is removed and re-added.
        """
        try:
            return self._render(tree, template, arrays or {})
        except (ValueError, RecursionError) as exc:
            raise ContentParseError(str(exc)) from exc

    def _render(self, tree: Tree, template: str, arrays: Mapping[KeyPath, ArrayRule]) -> str:
        parser = _Parser(template)
        root = parser.parse()
        if _tree_of(root) == tree:
            return template
        return _Diff(tree, root, template, _Comments(template, tuple(parser.comments)), arrays).apply()


class _Diff:
    """Computes the minimal edits that turn the template's AST into the merged tree."""

    def __init__(
        self,
        tree: Tree,
        root: _AstNode,
        text: str,
        comments: _Comments,
        arrays: Mapping[KeyPath, ArrayRule],
    ) -> None:
        self._arrays = arrays
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
            if (
                isinstance(leaf, Array)
                and isinstance(node, _ArrayNode)
                and self._splice_array(node, leaf, edits, self._arrays.get(path))
            ):
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

    def _rewrite(self, node: _AstNode, text: str, edits: list[_Edit], refusal: str | None = None) -> None:
        """Replace a whole value, refusing when that would silently drop a comment.

        Rewriting a span is only safe while it holds no comment: the merged tree has no comments,
        so a re-parse check cannot notice them gone. Refusing turns a comment loss into a
        conflict, which the caller surfaces rather than publishing a stripped file. The refusal
        names a changed binding only when the caller is rewriting one (`refusal`); any other value
        gets the general text.
        """
        if self._comments.overlaps(node.span.start, node.span.end):
            raise RenderRefused(refusal or VALUE_WITH_NOTE_REFUSED)
        edits.append(_Edit(node.span.start, node.span.end, text))

    def _splice_array(self, node: _ArrayNode, leaf: Array, edits: list[_Edit], rule: ArrayRule | None) -> bool:
        """Edit a one-element-per-line array to the merged elements, moving each binding with its own notes.

        A binding's notes are the block of comment lines directly above it (no blank line between), the comments on
        its own line, and the comments inside it. Each template element is matched to the merged element with the same
        key: its identity under the array's rule, or its exact value when there is no rule; then, within a group, the
        elements left on each side pair in order. The longest run of matches that keeps the order stays in place; every
        other matched binding moves with its notes. A changed binding is rewritten in its own span, which refuses when a
        comment is inside that span. A removed binding takes its own notes with it: the block directly above it, the
        comments on its lines, and the comments inside it (`_owned_start`). A blank line ends the block, so a section
        title stays. Without a rule a removal that would take any note is refused, because nothing can tell which
        binding the note belonged to. The comma follows the final order, and a trailing comma is kept when the template
        had one. Returns False for a layout this does not handle, so the caller rewrites the whole value.
        """
        if not node.elements or not _clean_multiline_array(self._source, node, self._comments):
            return False
        text = self._source
        template = [_to_node(element) for element in node.elements]
        wanted = list(leaf.elements)
        pairs = _match(template, wanted, rule)
        stay = _stable(pairs)
        template_of = {m: t for t, m in pairs}
        matched = {t for t, _ in pairs}
        slots: list[_Slot] = []
        for m, element in enumerate(wanted):
            if m not in template_of:
                slots.append(_Added(element))
                continue
            t = template_of[m]
            slots.append(_Placed(t, None if template[t] == element else element, t in stay))
        removed = [t for t in range(len(template)) if t not in matched]
        moved = [t for t, _ in pairs if t not in stay]

        trailing = _comma_after(self._comments, node.elements[-1]) is not None
        indent = text[_line_start(text, node.elements[0].span.start) : node.elements[0].span.start]
        multiline = any(
            isinstance(element, _ObjectNode) and "\n" in text[element.span.start : element.span.end]
            for element in node.elements
        )
        unit = _array_unit(text, node, indent) or self._indent
        last = len(slots) - 1
        for position, slot in enumerate(slots):
            if not (isinstance(slot, _Placed) and slot.stay):
                continue
            if slot.replacement is not None:
                # A changed element keeps its span; a comment inside it refuses the rewrite (a conflict).
                self._rewrite(
                    node.elements[slot.index],
                    self._element_text(slot.replacement, indent, multiline, unit),
                    edits,
                    refusal=CHANGED_WITH_NOTE_REFUSED,
                )
            element = node.elements[slot.index]
            comma = _comma_at(self._comments, element.span.end)
            wants = position < last or trailing
            if wants and comma is None:
                edits.append(_Edit(element.span.end, element.span.end, ","))
            if not wants and comma is not None:
                edits.append(_Edit(comma, comma + 1, ""))

        # Insertions go before the removals below, so at a shared offset the new lines come first.
        pending: list[str] = []
        anchor: int | None = None
        insert_points: set[int] = set()
        for position, slot in enumerate(slots):
            wants = position < last or trailing
            if isinstance(slot, _Placed) and slot.stay:
                if pending:
                    start = _open_block_position(text, self._comments, node.elements[slot.index].span.start)
                    edits.append(_Edit(start, start, "".join(pending)))
                    insert_points.add(start)
                    pending = []
                anchor = slot.index
            elif isinstance(slot, _Added):
                element_text = self._element_text(slot.value, indent, multiline, unit)
                pending.append(indent + element_text + ("," if wants else "") + self._newline)
            else:
                pending.append(self._moved_line(node, slot, indent, multiline, unit, wants))
        if pending:
            if anchor is None:
                start = _open_block_position(text, self._comments, node.elements[0].span.start)
            else:
                end = node.elements[anchor].span.end
                start = _past_block_comment(self._comments, end, _next_line_start(text, end))
            edits.append(_Edit(start, start, "".join(pending)))
            insert_points.add(start)

        deletions: list[tuple[int, int]] = []
        for index in removed:
            # A removed element takes its own notes with it: the comment block directly above it, the comments on its
            # lines and the comments inside it (`_owned_start`). A blank line ends the block, so a section title stays.
            element = node.elements[index]
            start = _owned_start(text, self._comments, element.span.start)
            end = _line_end_outside_comment(self._comments, start, _next_line_start(text, element.span.end))
            if rule is None and self._comments.overlaps(start, end):
                # Without a rule nothing says which binding a note belongs to, so a removal that would take one
                # is refused rather than losing it.
                raise RenderRefused(UNRULED_REMOVAL_REFUSED)
            deletions.append((start, end))
        for index in moved:
            # A moved element carries the block of comment lines directly above it, so its notes move with it.
            element = node.elements[index]
            start = _owned_start(text, self._comments, element.span.start)
            end = _line_end_outside_comment(self._comments, start, _next_line_start(text, element.span.end))
            deletions.append((start, end))
        for start, end in _widened_deletions(text, deletions, self._comments, insert_points):
            edits.append(_Edit(start, end, ""))
        return True

    def _moved_line(self, node: _ArrayNode, slot: _Placed, indent: str, multiline: bool, unit: str, wants: bool) -> str:
        """A matched element that changes place, re-emitted with its own text, comments and tail."""
        text = self._source
        element = node.elements[slot.index]
        if slot.replacement is None:
            core = text[element.span.start : element.span.end]
        else:
            if self._comments.overlaps(element.span.start, element.span.end):
                raise RenderRefused(CHANGED_WITH_NOTE_REFUSED)
            core = self._element_text(slot.replacement, indent, multiline, unit)
        start = _line_start(text, element.span.start)
        line_end = _line_end_outside_comment(self._comments, start, _next_line_start(text, element.span.end))
        comma = _comma_at(self._comments, element.span.end)
        if comma is not None and comma < line_end:
            tail = text[element.span.end : comma] + text[comma + 1 : line_end]
        else:
            tail = text[element.span.end : line_end]
        notes = text[_owned_start(text, self._comments, element.span.start) : start]
        return notes + indent + core + ("," if wants else "") + tail.rstrip("\r\n") + self._newline

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
    line_end = _line_end(text, value_end)
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

    text: str = field(repr=False)
    spans: tuple[_Span, ...]

    def overlaps(self, start: int, end: int) -> bool:
        """True when a comment starts, ends, or lies inside ``[start, end)``."""
        # Spans do not overlap, so the first span that ends after `start` is the only candidate.
        index = bisect.bisect_right(self._ends, start)
        return index < len(self.spans) and self.spans[index].start < end

    def closes_after(self, start: int, limit: int) -> int | None:
        """The end of a comment opening in ``[start, limit)`` and closing at or after `limit`."""
        first = bisect.bisect_left(self._starts, start)
        last = bisect.bisect_left(self._starts, limit)
        # Of the spans opening before `limit`, only the last one can reach past it: the others end before it starts.
        if last > first and self.spans[last - 1].end > limit:
            return self.spans[last - 1].end
        return None

    def next_meaningful(self, position: int) -> int:
        """The offset of the first character after `position` that is not in a comment or a space."""
        cursor = position
        while True:
            while cursor < len(self.text) and self.text[cursor] in _WHITESPACE:
                cursor += 1
            span = self._span_at(cursor)
            if span is None:
                return cursor
            cursor = span.end

    def without_comments(self, start: int, end: int) -> str:
        """The text of ``[start, end)`` with every comment in it removed."""
        pieces: list[str] = []
        cursor = start
        index = bisect.bisect_right(self._ends, start)
        while index < len(self.spans) and self.spans[index].start < end:
            span = self.spans[index]
            pieces.append(self.text[cursor : max(cursor, span.start)])
            cursor = min(end, span.end)
            index += 1
        pieces.append(self.text[cursor:end])
        return "".join(pieces)

    def own_comment_start(self, line_start: int, line_end: int) -> int | None:
        """Where the comment that makes a line comment-only starts, or `None` when the line holds code or is blank.

        Every character that is not a space must lie inside a comment. The comment may be a `//` line or a `/* … */`
        block that opens on this line or on an earlier one, so a block spread over lines counts as one note. A block
        that opens after code on an earlier line does not count: the code on that line would be taken with it.
        """
        content = [i for i in range(line_start, line_end) if self.text[i] not in _SPACES and self.text[i] != "\r"]
        if not content:
            return None
        covering = [self._span_at(i) for i in content]
        first = covering[0]
        if first is None or any(span is None for span in covering):
            return None
        before_block = self.text[_line_start(self.text, first.start) : first.start]
        if first.start >= line_start or before_block.strip(" \t") == "":
            return first.start
        return None

    @cached_property
    def _starts(self) -> tuple[int, ...]:
        """The start of each comment span; the parser records them in text order, so this is sorted."""
        return tuple(span.start for span in self.spans)

    @cached_property
    def _ends(self) -> tuple[int, ...]:
        """The end of each comment span, in the same order as `_starts`."""
        return tuple(span.end for span in self.spans)

    def _span_at(self, position: int) -> _Span | None:
        index = bisect.bisect_right(self._starts, position) - 1
        if index < 0:
            return None
        span = self.spans[index]
        return span if position < span.end else None


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


def _widened_deletions(
    text: str, spans: list[tuple[int, int]], comments: _Comments, insert_points: set[int]
) -> list[tuple[int, int]]:
    """The deletions, each widened over the blank lines it would leave beside a bracket or a blank line.

    Two deletions that touch are one segment. Two deletions separated by exactly one blank line form a group. A group
    is either joined or not, decided by `_gap_goes_with_group`: a blank line or the opening bracket above it, or the
    closing bracket below it. A joined group is widened once, as one range from its first deletion to its last, so the
    blank line between its parts goes with it. A group that is not joined is widened segment by segment, so the blank
    line that separates two parts of the array that stay is kept. The neighbours that decide this need not be
    bindings: a section title or a comment can stand there. Ranges that overlap after widening are merged last
    (`_union`), so no two deletions overlap.
    """
    groups: list[list[tuple[int, int]]] = []
    for start, end in sorted(spans):
        if groups and start <= groups[-1][-1][1]:
            last_start, last_end = groups[-1][-1]
            groups[-1][-1] = (last_start, max(last_end, end))
        elif groups and _single_blank_line(text, groups[-1][-1][1], start):
            groups[-1].append((start, end))
        else:
            groups.append([(start, end)])
    return _union([piece for group in groups for piece in _group_pieces(text, comments, insert_points, group)])


def _union(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The ranges in order, with overlapping ones merged, so no two deletions overlap.

    Ranges that only touch stay apart: an insertion at the offset where they meet would fall inside a merged range, and
    an edit that starts inside an earlier one is not applied (see `_apply_edits`).
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _group_pieces(
    text: str, comments: _Comments, insert_points: set[int], segments: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """The ranges one group of deletion segments becomes (see `_widened_deletions`)."""
    first, last = segments[0][0], segments[-1][1]
    if len(segments) == 1 or _gap_goes_with_group(text, comments, first, last):
        return _widen(text, comments, insert_points, first, last)
    pieces: list[tuple[int, int]] = []
    for start, end in segments:
        pieces.extend(_widen(text, comments, insert_points, start, end))
    return pieces


def _gap_goes_with_group(text: str, comments: _Comments, first: int, last: int) -> bool:
    """True when the blank line between two parts of a group goes with the group.

    That is when a blank line or the opening bracket stands above the group, or the closing bracket stands below it.
    """
    blank_above = first > 0 and _is_blank(text, comments, _line_start(text, first - 1), first - 1)
    return blank_above or _opens_array(text, comments, first) or _bracket_below(text, comments, last)


def _single_blank_line(text: str, gap_start: int, gap_end: int) -> bool:
    """True when the text between two deletions is exactly one blank line."""
    gap = text[gap_start:gap_end]
    return gap.strip() == "" and gap.count("\n") == 1


def _is_blank(text: str, comments: _Comments, line_start: int, line_end: int) -> bool:
    """True for a line with nothing on it. A line inside a block comment holds that comment, so it is not blank."""
    return text[line_start:line_end].strip() == "" and not comments.overlaps(line_start, line_end)


def _line_end(text: str, position: int) -> int:
    newline = text.find("\n", position)
    return len(text) if newline == -1 else newline


def _opens_array(text: str, comments: _Comments, start: int) -> bool:
    """True when the nearest code above `start` is an opening bracket, so `start` is the array's first element."""
    return start > 0 and _code_above(text, comments, start - 1).endswith("[")


def _code_above(text: str, comments: _Comments, line_end: int) -> str:
    """The code of the nearest line at or above `line_end` that holds any, with its comments taken out.

    Lines that hold only comments are skipped, so a bracket followed by a block comment that runs over several
    lines still counts as the line that opens the array. A blank line ends the search with nothing, as does the top.
    """
    while True:
        line_start = _line_start(text, line_end)
        code = comments.without_comments(line_start, line_end).strip()
        if code:
            return code
        if _is_blank(text, comments, line_start, line_end) or line_start == 0:
            return ""
        line_end = line_start - 1


def _bracket_below(text: str, comments: _Comments, position: int) -> bool:
    """True when the first line at or below `position` that is not blank starts with the closing `]`.

    Blank lines are skipped, because a deletion that leaves a blank line in front of the closing bracket leaves the
    same blank line as one a deletion right before the bracket would. A line of comments before the bracket is not
    skipped: it stays, so the bracket is not reached.
    """
    while position < len(text):
        line_end = _line_end(text, position)
        if not _is_blank(text, comments, position, line_end):
            return comments.without_comments(position, line_end).strip().startswith("]")
        position = _next_line_start(text, line_end)
    return False


def _widen(text: str, comments: _Comments, insert_points: set[int], start: int, end: int) -> list[tuple[int, int]]:
    """A deletion `[start, end)` and the blank lines it takes with it, as separate ranges.

    A blank line that sits between two unaffected elements is left alone. A blank line is removed when the deletion
    would put it right after the opening `[`, right before the closing `]`, or beside another blank line; but not when
    an insertion goes in at the deletion's first offset, since that blank line then separates a section title from the
    new binding. Brackets are read with their comments taken out, so `[ // cols` opens an array and `// [` does not.
    A blank line before the deletion is its own range, not part of it, so an insertion at the same offset is not
    skipped. The lines are read from the text as it stands, which is the text the deletion is made in.
    """
    after_end = _next_line_start(text, end)
    after = text[end:after_end] if end < len(text) else None
    blank_before = start > 0 and _is_blank(text, comments, _line_start(text, start - 1), start - 1)
    blank_after = after is not None and after != "" and _is_blank(text, comments, end, _line_end(text, end))
    opens = _opens_array(text, comments, start)
    closes = _bracket_below(text, comments, end)
    # An insertion at this offset puts the new binding where the deleted one was. The blank line after the deletion then
    # separates that binding from whatever follows (a section title, say) and must stay, unless the closing bracket
    # follows: then it would sit against the bracket.
    inserted = start in insert_points
    pieces: list[tuple[int, int]] = []
    if blank_before and closes and not inserted:
        pieces.append((_line_start(text, start - 1), start))
    if blank_after and (blank_before or opens or closes) and (closes or not inserted):
        end = after_end
    pieces.append((start, end))
    return pieces


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


# Refusals are fixed texts: a message that echoed a value or a key name would leak it into a conflict.
# A changed value that holds a comment cannot be rewritten without losing it; the text names only the cause.
VALUE_WITH_NOTE_REFUSED = (
    "this value changed and has a comment inside it, so it cannot be written without losing that comment"
)

# A changed binding is rewritten whole, so a comment inside it would be lost; the text names the cause, never a value.
CHANGED_WITH_NOTE_REFUSED = (
    "this binding changed and has a comment inside it, so it cannot be written without losing that comment"
)

# Two edits to one array overlap, so one of them would be dropped; the text names only the cause.
EDIT_OVERLAP_REFUSED = "two edits to this array overlap, so it cannot be written without losing one of them"

UNRULED_REMOVAL_REFUSED = (
    "this array has no rule to tell its bindings apart, so a comment on a removed element cannot be kept"
)


@dataclass(frozen=True)
class _Placed:
    """A template element the merged array keeps (`stay`) or moves, with its new value when the merge changed it."""

    index: int
    replacement: Node | None
    stay: bool


@dataclass(frozen=True)
class _Added:
    """A value the merged array has and the template does not, placed where it falls in order."""

    value: Node


_Slot = _Placed | _Added


def _key(identity: ElementId | None, node: Node) -> tuple[str, object]:
    """Which binding a node is: its identity under the rule, or its exact value when the rule names none."""
    return ("id", identity) if identity is not None else ("value", node)


def _match(template: list[Node], wanted: list[Node], rule: ArrayRule | None) -> list[tuple[int, int]]:
    """The `(template index, merged index)` pairs of elements that are the same binding, in template order.

    Same key pairs first, in order of occurrence. Then, within a group, the bindings left on each side pair in order,
    so a changed binding (its command changed, say) is rewritten in its own place rather than removed and re-added.
    Each identity is computed once.
    """
    template_ids = [None if rule is None else element_identity(node, rule) for node in template]
    wanted_ids = [None if rule is None else element_identity(node, rule) for node in wanted]
    free: dict[tuple[str, object], deque[int]] = {}
    for m, (node, identity) in enumerate(zip(wanted, wanted_ids, strict=True)):
        free.setdefault(_key(identity, node), deque()).append(m)
    pairs: dict[int, int] = {}
    for t, (node, identity) in enumerate(zip(template, template_ids, strict=True)):
        candidates = free.get(_key(identity, node))
        if candidates:
            pairs[t] = candidates.popleft()
    if rule is not None:
        taken = set(pairs.values())
        left_by_group: dict[tuple[tuple[str, str | None], ...], deque[int]] = {}
        for m, identity in enumerate(wanted_ids):
            if identity is not None and m not in taken:
                left_by_group.setdefault(element_group(identity, rule), deque()).append(m)
        for t, identity in enumerate(template_ids):
            if t in pairs or identity is None:
                continue
            candidates = left_by_group.get(element_group(identity, rule))
            if candidates:
                pairs[t] = candidates.popleft()
    return sorted(pairs.items())


def _stable(pairs: list[tuple[int, int]]) -> set[int]:
    """The template indexes of the longest run of pairs whose merged indexes also ascend: those keep their place.

    Every other pair moves. Found by longest increasing subsequence, so it is the fewest moves that keep the order.
    """
    tails: list[int] = []  # the smallest merged index that ends an increasing run of each length
    tail_pairs: list[int] = []  # the pair index that ends that run
    previous = [-1] * len(pairs)
    for index, (_, m) in enumerate(pairs):
        position = bisect.bisect_left(tails, m)
        previous[index] = tail_pairs[position - 1] if position > 0 else -1
        if position == len(tails):
            tails.append(m)
            tail_pairs.append(index)
        else:
            tails[position] = m
            tail_pairs[position] = index
    if not tail_pairs:
        return set()
    kept: set[int] = set()
    index = tail_pairs[-1]
    while index != -1:
        kept.add(pairs[index][0])
        index = previous[index]
    return kept


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
        and _comma_stays_on_its_line(text, comments, element)
        for element in node.elements
    )


def _comma_stays_on_its_line(text: str, comments: _Comments, element: _AstNode) -> bool:
    """True when an element's comma, if it has one, is on the element's own line rather than a line of its own.

    A comma alone on a line would survive the removal of its element's line and leave the array invalid, so such a
    layout is not spliced; the whole value is rewritten instead.
    """
    comma = _comma_at(comments, element.span.end)
    return comma is None or "\n" not in text[element.span.end : comma]


def _owned_start(text: str, comments: _Comments, element_start: int) -> int:
    """Where an element's own notes begin: the block of comment lines directly above it, with no blank line between.

    This is the one rule that decides which binding a note belongs to. A blank line, or a line of code, ends the block,
    and a note past a blank line is a section title that belongs to no element. A `//` line and a `/* … */` block
    both count, so a note written as a block moves with its element, goes with it when it is removed, or is refused.
    """
    position = _line_start(text, element_start)
    while position > 0:
        start = comments.own_comment_start(_line_start(text, position - 1), position - 1)
        if start is None:
            break
        position = _line_start(text, start)
    return position


def _open_block_position(text: str, comments: _Comments, start: int) -> int:
    """Where a block inserted before the element starting at `start` goes: above that element's own notes.

    Inserting between a note and its element would hand the note to the new element, so the insertion point is the
    start of the element's notes, given by the same ownership rule (`_owned_start`).
    """
    return _owned_start(text, comments, start)


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
    """Apply the edits in offset order.

    An edit that starts inside an earlier one is skipped only when it takes nothing with it: it removes text the earlier
    edit already removed. One that carries text, or reaches past the earlier edit, would be lost without a trace, so it
    refuses the render instead.
    """
    pieces: list[str] = []
    cursor = 0
    for edit in sorted(edits, key=lambda item: item.start):
        if edit.start < cursor:
            if edit.text or edit.end > cursor:
                raise RenderRefused(EDIT_OVERLAP_REFUSED)
            continue
        pieces.append(text[cursor : edit.start])
        pieces.append(edit.text)
        cursor = edit.end
    pieces.append(text[cursor:])
    return "".join(pieces)


__all__ = [
    "CHANGED_WITH_NOTE_REFUSED",
    "EDIT_OVERLAP_REFUSED",
    "UNRULED_REMOVAL_REFUSED",
    "VALUE_WITH_NOTE_REFUSED",
    "JsoncAdapter",
]
