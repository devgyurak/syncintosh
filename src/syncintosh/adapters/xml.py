# SPDX-License-Identifier: AGPL-3.0-or-later
"""The XML format adapter: parse to a key-path tree, render by minimal text edits.

JetBrains IDEs keep their settings as XML (``options/*.xml``, ``keymaps/*.xml``, ``colors/*.icls``). Their serializer
writes ``<option ... />`` with a space before the slash and `&#10;` inside attributes, and `lxml` re-serializes
that as ``/>`` and a raw newline, so a round-trip through a library rewrites lines nobody changed. This adapter
therefore does what the JSONC adapter does (``AGENTS.md`` §2.5): it records the source span of every start tag,
attribute value and text run with a small stdlib scanner, and **renders by editing the original text** at the spans that
changed. `lxml` only *verifies*: it parses the same text with entity resolution, DTD loading and network access
off, and the scanner and the parser must agree on every element's tag, attributes and simple text, or the file is
refused. A DOCTYPE (and with it any entity declaration) is refused outright.

Key paths (the mapping a three-way merge compares)
--------------------------------------------------

* The root element is not a segment; its attributes are depth-1 ``@attr`` leaves.
* A child element is a segment. Four families of tag carry an identity, so a settings key keeps its path when its
  siblings come and go: ``component``, ``option``, ``macro``, ``property`` and ``template`` by ``name``, ``entry`` by
  ``key``, ``action`` by ``id``, ``codeStyleSettings`` by ``language``. Such an element is the segment
  ``tag[value]`` (``component[UpdatesConfigurable]``); the identity attribute is not also a leaf. A tag from that
  table is always written with its identity, even when it has no siblings.
* Any other attribute is ``@attr`` and a leaf element's text is ``#text``. ``[``, ``@`` and ``#`` cannot start or
  appear in an XML name, so these segments never collide with a tag; the identity value is written as it is, unescaped.
* A tag outside the table is just ``tag`` while it is the only sibling of that name. When a name (or an identity)
  repeats, **every** element of that name under the parent becomes one ``Array`` leaf compared whole, in document
  order: ``<mask>`` in ``find.xml`` and ``<parameter-pattern>`` in ``databaseSettings.xml`` repeat with nothing to tell
  them apart, so a positional edit is not a key edit (the same reason an array is whole in JSON).
  ``<global_color_scheme name="..."/>`` is *not* in the table, since there ``name`` is a value: it is ``@name``.
* Every value is ``Scalar("str")``: XML has no types, and ``"true"`` stays a string. An element with no attribute
  but the identity, no text and no children is ``EmptyObject``.
* Comments, processing instructions and formatting whitespace are not in the tree; render keeps them. Whitespace-only
  text counts as formatting. An element that holds both text and child elements is refused (``ContentParseError``).
* The tree is a map, so the order of siblings is not compared: JetBrains sorts components when it saves, and an
  order change alone must not look like a conflict.

Namespaces are refused (a ``:`` in a tag or attribute name): no settings file written by the IDE platform uses one, and
refusing is cheaper than carrying a prefix map that nothing exercises.

Render rules
------------

* A changed attribute value replaces only that value; a new attribute is appended after the last one (before the
  ` />`); a removed attribute takes its leading space with it.
* A new element goes after the last unchanged sibling with the same tag, else after the last unchanged sibling, else it
  takes the place of the first removed one; an element without children is opened (` />` becomes a start and end tag).
  Indentation comes from the siblings or the parent; a removed element takes its whole line.
* A repeated group is diffed element by element, so one added ``<mask>`` is one inserted line.
* Escaping follows the IDE: `&amp; &lt; &gt; &quot; &#10; &#9; &#13;` in attributes, `&amp; &lt; &gt;` in text. A text
  whose original was a CDATA section stays one (`]]>` split).
* A trailing newline is kept if the file had one and not added if it had none.

The merge itself is not this module's job; it returns a :class:`~syncintosh.core.tree.Tree` and takes one back
(``core/merge.py``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Final

from syncintosh.adapters.edits import apply_edits, settle
from syncintosh.core.errors import ContentParseError, RenderRefused
from syncintosh.core.ports import TextEdit
from syncintosh.core.tree import Array, EmptyObject, KeyPath, Node, Object, Scalar, Tree

if TYPE_CHECKING:
    from syncintosh.core.keyed_array import ArrayRule
    from syncintosh.core.ports import Verbatim

# The attribute that names an element among its siblings, by tag. A fixed table: the IDE platform's own serializers
# (`xmlb`) key a `component`/`option` by `name`, so no other tag's `name` is assumed to be an identity.
IDENTITY_ATTRIBUTES: Final[Mapping[str, str]] = {
    "component": "name",
    "option": "name",
    "macro": "name",
    "property": "name",
    "template": "name",
    "entry": "key",
    "action": "id",
    "codeStyleSettings": "language",
}

# Fixed reasons; each names the cause and never a value (a setting may be a credential).
MIXED_CONTENT_REFUSED = "an element holds both text and child elements, which this format does not use"
NAMESPACE_REFUSED = "a namespaced name is not supported"
DOCTYPE_REFUSED = "a DOCTYPE or entity declaration is not allowed"
SCANNER_DISAGREES = "the scanner and the XML parser read this file differently"
COMMENT_IN_VALUE_REFUSED = "this value changed and has a comment inside it, so it cannot be written without losing it"
SHAPE_REFUSED = "the merged tree has a shape this format cannot write"

_XML_SPACE = " \t\r\n"
_NAME = r"[^\s/>=<\"'&]+"
_START_TAG = re.compile(rf"<({_NAME})")
_ATTRIBUTE = re.compile(rf"(\s+)({_NAME})\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_TAIL = re.compile(r"\s*(/?)>")
_END_TAG = re.compile(rf"</({_NAME})\s*>")
_REFERENCE = re.compile(r"&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);")
_ENCODING = re.compile(r"encoding\s*=\s*[\"']([^\"']+)[\"']")

_PREDEFINED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def _refuse(reason: str) -> ContentParseError:
    return ContentParseError(reason)


def _decode_reference(match: re.Match[str]) -> str:
    body = match.group(1)
    if body in _PREDEFINED:
        return _PREDEFINED[body]
    try:
        return chr(int(body[2:], 16) if body[1] == "x" else int(body[1:]))
    except (ValueError, OverflowError) as exc:
        raise _refuse("a character reference is out of range") from exc


def _decode(raw: str) -> str:
    if "&" not in raw:
        return raw
    decoded = _REFERENCE.sub(_decode_reference, raw)
    # Anything still holding an `&` named an entity the file never declared.
    if "&" in _REFERENCE.sub("", raw):
        raise _refuse("an undefined entity reference")
    return decoded


def _normal_newlines(raw: str) -> str:
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def _decode_text(raw: str) -> str:
    return _decode(_normal_newlines(raw))


def _decode_attribute(raw: str) -> str:
    # An unescaped tab or newline in an attribute is a space; only a character reference keeps one.
    return _decode(_normal_newlines(raw).replace("\n", " ").replace("\t", " "))


def _escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
        .replace("\t", "&#9;")
    )


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")


def _cdata(value: str) -> str:
    return "<![CDATA[" + value.replace("]]>", "]]]]><![CDATA[>") + "]]>"


@dataclass
class _Attribute:
    name: str
    raw: str
    value: str
    quote: str
    # The leading whitespace starts at `lead`, the name at `start`, and the closing quote ends at `end`.
    lead: int
    start: int
    value_start: int
    end: int


@dataclass
class _Piece:
    """One run of character data: plain text or a CDATA section."""

    value: str
    cdata: bool


@dataclass
class _Element:
    tag: str
    start: int
    name_end: int
    # The end of the last attribute, or of the name when there is none: where a new attribute goes.
    attributes_end: int = 0
    attributes: list[_Attribute] = field(default_factory=list)
    self_closing: bool = False
    start_tag_end: int = 0
    close_start: int = 0
    end: int = 0
    children: list[_Element] = field(default_factory=list)
    pieces: list[_Piece] = field(default_factory=list)
    # A comment or processing instruction directly inside the element.
    has_markup: bool = False

    @property
    def text(self) -> str:
        """The character data, with whitespace-only content read as formatting."""
        joined = "".join(piece.value for piece in self.pieces)
        return joined if joined.strip(_XML_SPACE) else ""

    @property
    def raw_text(self) -> str:
        return "".join(piece.value for piece in self.pieces)

    @property
    def has_cdata(self) -> bool:
        return any(piece.cdata for piece in self.pieces)

    def attribute(self, name: str) -> _Attribute | None:
        return next((item for item in self.attributes if item.name == name), None)

    def walk(self) -> list[_Element]:
        found = [self]
        for child in self.children:
            found.extend(child.walk())
        return found


def _lex(text: str) -> _Element:
    """Scan `text` into an element tree with every span, or refuse it."""
    declaration = re.match(r"﻿?<\?xml[^?]*\?>", text)
    if declaration is not None:
        encoding = _ENCODING.search(declaration.group(0))
        if encoding is not None and encoding.group(1).lower().replace("_", "-") not in ("utf-8", "utf8"):
            raise _refuse("only a UTF-8 document is supported")
    root: _Element | None = None
    stack: list[_Element] = []
    position = 0
    length = len(text)
    while position < length:
        if text.startswith("<!--", position):
            end = text.find("-->", position + 4)
            if end < 0:
                raise _refuse("an unterminated comment")
            _note_markup(stack)
            position = end + 3
        elif text.startswith("<![CDATA[", position):
            end = text.find("]]>", position + 9)
            if end < 0 or not stack:
                raise _refuse("a CDATA section outside an element or unterminated")
            stack[-1].pieces.append(_Piece(_normal_newlines(text[position + 9 : end]), cdata=True))
            position = end + 3
        elif text.startswith("<?", position):
            end = text.find("?>", position + 2)
            if end < 0:
                raise _refuse("an unterminated processing instruction")
            _note_markup(stack)
            position = end + 2
        elif text.startswith("<!", position):
            raise _refuse(DOCTYPE_REFUSED)
        elif text.startswith("</", position):
            match = _END_TAG.match(text, position)
            if match is None or not stack or stack[-1].tag != match.group(1):
                raise _refuse("a mismatched end tag")
            element = stack.pop()
            element.close_start = position
            element.end = match.end()
            position = match.end()
        elif text[position] == "<":
            element = _start_tag(text, position)
            if stack:
                stack[-1].children.append(element)
            elif root is None:
                root = element
            else:
                raise _refuse("more than one root element")
            if not element.self_closing:
                stack.append(element)
            position = element.start_tag_end
        else:
            end = text.find("<", position)
            end = length if end < 0 else end
            raw = text[position:end]
            if stack:
                stack[-1].pieces.append(_Piece(_decode_text(raw), cdata=False))
            elif raw.strip(_XML_SPACE + "﻿"):
                raise _refuse("text outside the root element")
            position = end
    if stack or root is None:
        raise _refuse("no root element" if root is None else "an element is never closed")
    return root


def _note_markup(stack: list[_Element]) -> None:
    if stack:
        stack[-1].has_markup = True


def _start_tag(text: str, position: int) -> _Element:
    match = _START_TAG.match(text, position)
    if match is None:
        raise _refuse("a malformed start tag")
    tag = match.group(1)
    if ":" in tag:
        raise _refuse(NAMESPACE_REFUSED)
    element = _Element(tag=tag, start=position, name_end=match.end())
    cursor = match.end()
    while (attribute := _ATTRIBUTE.match(text, cursor)) is not None:
        name = attribute.group(2)
        if ":" in name:
            raise _refuse(NAMESPACE_REFUSED)
        double = attribute.group(3) is not None
        raw = attribute.group(3) if double else attribute.group(4)
        element.attributes.append(
            _Attribute(
                name=name,
                raw=raw,
                value=_decode_attribute(raw),
                quote='"' if double else "'",
                lead=attribute.start(1),
                start=attribute.start(2),
                value_start=attribute.start(3) if double else attribute.start(4),
                end=attribute.end(),
            )
        )
        cursor = attribute.end()
    tail = _TAIL.match(text, cursor)
    if tail is None:
        raise _refuse("a malformed start tag")
    element.attributes_end = cursor
    element.self_closing = tail.group(1) == "/"
    element.start_tag_end = tail.end()
    if element.self_closing:
        element.end = tail.end()
        element.close_start = tail.end()
    return element


def _verify(text: str, root: _Element) -> None:
    """Make the real parser read `text` and agree with the scanner, or refuse the file.

    `lxml` is imported here and nowhere else, so CLI startup does not pay for it (``AGENTS.md`` §4).
    """
    import lxml.etree as etree  # ty: ignore[unresolved-import]  # lxml's compiled etree ships no stubs

    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        parsed = etree.fromstring(text.encode("utf-8"), parser)
    except etree.XMLSyntaxError as exc:
        line, column = exc.position
        raise ContentParseError(f"malformed XML at line {line}, column {column}") from exc
    except ValueError as exc:
        raise ContentParseError("malformed XML") from exc
    theirs = list(parsed.iter(tag=etree.Element))
    ours = root.walk()
    if len(theirs) != len(ours):
        raise _refuse(SCANNER_DISAGREES)
    for mine, other in zip(ours, theirs, strict=True):
        same = mine.tag == other.tag and {a.name: a.value for a in mine.attributes} == dict(other.attrib)
        # Text is compared for an element with nothing but text in it; a comment splits it in lxml's model.
        simple = not mine.children and not mine.has_markup
        if not same or (simple and mine.raw_text != (other.text or "")):
            raise _refuse(SCANNER_DISAGREES)


def _segment(element: _Element) -> tuple[str, str | None]:
    """The key-path segment of `element` and the identity attribute it was named by, if any."""
    attribute = IDENTITY_ATTRIBUTES.get(element.tag)
    if attribute is not None:
        named = element.attribute(attribute)
        if named is not None:
            return f"{element.tag}[{named.value}]", attribute
    return element.tag, None


def _groups(children: list[_Element]) -> dict[str, list[tuple[_Element, str | None]]]:
    """The children by segment, in the order each segment first appears."""
    grouped: dict[str, list[tuple[_Element, str | None]]] = {}
    for child in children:
        segment, identity = _segment(child)
        grouped.setdefault(segment, []).append((child, identity))
    return grouped


def _tag_of(segment: str) -> str:
    return segment.partition("[")[0]


def _identity_of(segment: str) -> str | None:
    _, bracket, rest = segment.partition("[")
    return rest[:-1] if bracket else None


def _check_content(element: _Element) -> None:
    if element.children and element.text:
        raise _refuse(MIXED_CONTENT_REFUSED)


def _object_of(element: _Element) -> Object:
    """An element inside an array: attributes (identity included), text and children, kept whole."""
    _check_content(element)
    entries: list[tuple[str, Node]] = [(f"@{item.name}", Scalar("str", item.value)) for item in element.attributes]
    if element.text:
        entries.append(("#text", Scalar("str", element.text)))
    for segment, members in _groups(element.children).items():
        objects = tuple(_object_of(member) for member, _ in members)
        entries.append((segment, objects[0] if len(objects) == 1 else Array(objects)))
    return Object(tuple(entries))


@dataclass
class _Document:
    """A scanned document: its tree, and where each attribute and text leaf lives in the source."""

    root: _Element
    tree: Tree
    attributes: dict[KeyPath, tuple[_Element, _Attribute]]
    texts: dict[KeyPath, _Element]


def _document(text: str) -> _Document:
    root = _lex(text)
    _verify(text, root)
    entries: dict[KeyPath, Node] = {}
    attributes: dict[KeyPath, tuple[_Element, _Attribute]] = {}
    texts: dict[KeyPath, _Element] = {}
    _fill(root, (), None, entries, attributes, texts)
    return _Document(root=root, tree=Tree.create(entries), attributes=attributes, texts=texts)


def _fill(
    element: _Element,
    path: KeyPath,
    identity: str | None,
    entries: dict[KeyPath, Node],
    attributes: dict[KeyPath, tuple[_Element, _Attribute]],
    texts: dict[KeyPath, _Element],
) -> None:
    _check_content(element)
    produced = False
    for attribute in element.attributes:
        if attribute.name != identity:
            key = (*path, f"@{attribute.name}")
            entries[key] = Scalar("str", attribute.value)
            attributes[key] = (element, attribute)
            produced = True
    if element.text:
        entries[(*path, "#text")] = Scalar("str", element.text)
        texts[(*path, "#text")] = element
        produced = True
    for segment, members in _groups(element.children).items():
        produced = True
        child_path = (*path, segment)
        if len(members) == 1:
            _fill(members[0][0], child_path, members[0][1], entries, attributes, texts)
        else:
            entries[child_path] = Array(tuple(_object_of(member) for member, _ in members))
    if not produced:
        entries[path] = EmptyObject()


@dataclass
class _Trie:
    """The wanted tree as nested segments, so a render walks the template and the tree side by side."""

    leaf: Node | None = None
    kids: dict[str, _Trie] = field(default_factory=dict)


def _trie_of(tree: Tree) -> _Trie:
    root = _Trie()
    for path, node in tree.entries.items():
        cursor = root
        for step in path:
            cursor = cursor.kids.setdefault(step, _Trie())
        cursor.leaf = node
    return root


@dataclass(frozen=True)
class _Described:
    """An element to write: what a new or replaced element looks like before it becomes text."""

    tag: str
    attributes: tuple[tuple[str, str], ...]
    text: str | None
    children: tuple[_Described, ...]


def _scalar_text(node: Node | None) -> str:
    if not isinstance(node, Scalar) or not isinstance(node.value, str):
        raise RenderRefused(SHAPE_REFUSED)
    return node.value


def _described_from_object(tag: str, node: Node) -> _Described:
    if not isinstance(node, Object):
        raise RenderRefused(SHAPE_REFUSED)
    attributes: list[tuple[str, str]] = []
    text: str | None = None
    children: list[_Described] = []
    for key, value in node.entries:
        if key.startswith("@"):
            attributes.append((key[1:], _scalar_text(value)))
        elif key == "#text":
            text = _scalar_text(value)
        elif isinstance(value, Array):
            children.extend(_described_from_object(_tag_of(key), item) for item in value.elements)
        else:
            children.append(_described_from_object(_tag_of(key), value))
    return _Described(tag, tuple(attributes), text, tuple(children))


def _described_from_trie(segment: str, node: _Trie) -> list[_Described]:
    tag = _tag_of(segment)
    if isinstance(node.leaf, Scalar | Object):
        raise RenderRefused(SHAPE_REFUSED)
    if isinstance(node.leaf, Array):
        return [_described_from_object(tag, item) for item in node.leaf.elements]
    identity = _identity_of(segment)
    attributes: list[tuple[str, str]] = []
    if identity is not None:
        attributes.append((IDENTITY_ATTRIBUTES[tag], identity))
    text: str | None = None
    children: list[_Described] = []
    for step, kid in node.kids.items():
        if step.startswith("@"):
            attributes.append((step[1:], _scalar_text(kid.leaf)))
        elif step == "#text":
            text = _scalar_text(kid.leaf)
        else:
            children.extend(_described_from_trie(step, kid))
    if text and children:
        raise RenderRefused(MIXED_CONTENT_REFUSED)
    return [_Described(tag, tuple(attributes), text, tuple(children))]


class XmlAdapter:
    """A `ContentAdapter` over XML that renders by editing the original text."""

    def parse(self, text: str) -> Tree:
        """Parse XML into a tree, raising `ContentParseError` on malformed or unsupported input."""
        try:
            return _document(text).tree
        except RecursionError as exc:
            raise ContentParseError("the document is nested too deeply") from exc

    def render(
        self,
        tree: Tree,
        template: str,
        arrays: Mapping[KeyPath, ArrayRule] | None = None,
        verbatim: Verbatim | None = None,
    ) -> str:
        """Render `tree` by editing `template`; unchanged content returns the bytes verbatim.

        `arrays` is accepted for the port and unused: an XML array is whole, so it has no identity to pair by.
        `verbatim` names attribute values and text to copy from another document as written there.
        """
        return apply_edits(template, self._edits(tree, template, verbatim))

    def render_edits(
        self,
        tree: Tree,
        template: str,
        arrays: Mapping[KeyPath, ArrayRule] | None = None,
        verbatim: Verbatim | None = None,
    ) -> tuple[TextEdit, ...]:
        """The settled edits :meth:`render` applies to `template`; none when it comes back verbatim."""
        return tuple(settle(self._edits(tree, template, verbatim)))

    def _edits(self, tree: Tree, template: str, verbatim: Verbatim | None) -> list[TextEdit]:
        try:
            document = _document(template)
            copies = _copies(tree, verbatim)
            return _Render(tree, template, document, copies).edits()
        except RecursionError as exc:
            raise ContentParseError("the document is nested too deeply") from exc


# What to copy for a verbatim path: the raw characters and, for an attribute, the quote they were written in.
@dataclass(frozen=True)
class _Copy:
    raw: str = field(repr=False)
    quote: str


def _copies(tree: Tree, verbatim: Verbatim | None) -> dict[KeyPath, _Copy]:
    """The raw text to copy for each verbatim leaf the source holds with `tree`'s own value."""
    if verbatim is None or not verbatim.paths:
        return {}
    source = _document(verbatim.text)
    copies: dict[KeyPath, _Copy] = {}
    for path in verbatim.paths:
        if tree.value(path) != source.tree.value(path):
            continue
        if path in source.attributes:
            attribute = source.attributes[path][1]
            copies[path] = _Copy(attribute.raw, attribute.quote)
        elif path in source.texts:
            element = source.texts[path]
            copies[path] = _Copy(verbatim.text[element.start_tag_end : element.close_start], "")
    return copies


class _Render:
    """The edits that turn the template into the wanted tree, element by element."""

    def __init__(self, tree: Tree, text: str, document: _Document, copies: dict[KeyPath, _Copy]) -> None:
        self._text = text
        self._document = document
        self._copies = copies
        self._wanted = _trie_of(tree)
        self._edits: list[TextEdit] = []
        self._multiline = "\n" in text
        self._nl = "\r\n" if "\r\n" in text else "\n"
        self._unit = self._detect_unit() if self._multiline else ""
        self._empty_close = self._detect_empty_close()

    def edits(self) -> list[TextEdit]:
        self._element(self._document.root, (), None, self._wanted)
        return self._edits

    # --- layout ---------------------------------------------------------------------------------------------

    def _detect_unit(self) -> str:
        root = self._document.root
        base = self._indent_of(root) or ""
        for child in root.children:
            indent = self._indent_of(child)
            if indent is not None and indent.startswith(base) and len(indent) > len(base):
                return indent[len(base) :]
        return "  "

    def _detect_empty_close(self) -> str:
        for element in self._document.root.walk():
            if element.self_closing:
                return " />" if self._text[element.attributes_end : element.end].startswith((" ", "\t")) else "/>"
        return " />"

    def _line_start(self, position: int) -> int:
        return self._text.rfind("\n", 0, position) + 1

    def _indent_of(self, element: _Element) -> str | None:
        """The whitespace before `element` on its line, or `None` when something else shares the line."""
        before = self._text[self._line_start(element.start) : element.start]
        return None if before.strip(" \t") else before

    def _child_indent(self, parent: _Element) -> str | None:
        """The indentation for a child of `parent`, or `None` when the file is not laid out line by line."""
        for child in parent.children:
            indent = self._indent_of(child)
            if indent is not None:
                return indent
        own = self._indent_of(parent)
        return None if own is None or not self._multiline else own + self._unit

    # --- writing text ---------------------------------------------------------------------------------------

    def _serialize(self, element: _Described, indent: str | None) -> str:
        head = f"<{element.tag}" + "".join(
            f' {name}="{_escape_attribute(value)}"' for name, value in element.attributes
        )
        if element.text:
            return f"{head}>{_escape_text(element.text)}</{element.tag}>"
        if not element.children:
            return head + self._empty_close
        if indent is None:
            return (
                head + ">" + "".join(self._serialize(child, None) for child in element.children) + f"</{element.tag}>"
            )
        inner = indent + self._unit
        body = "".join(f"{self._nl}{inner}{self._serialize(child, inner)}" for child in element.children)
        return f"{head}>{body}{self._nl}{indent}</{element.tag}>"

    def _block(self, parent: _Element, children: list[_Described]) -> str:
        """The content that gives `parent` these children, laid out as its siblings are."""
        inner = self._child_indent(parent)
        if inner is None:
            return "".join(self._serialize(child, None) for child in children)
        outer = self._indent_of(parent) or ""
        body = "".join(f"{self._nl}{inner}{self._serialize(child, inner)}" for child in children)
        return f"{body}{self._nl}{outer}"

    def _set_content(self, element: _Element, content: str) -> None:
        """Replace everything between `element`'s start and end tag, opening ` />` first when it has none."""
        if element.self_closing:
            self._edits.append(TextEdit(element.attributes_end, element.end, f">{content}</{element.tag}>"))
        else:
            self._edits.append(TextEdit(element.start_tag_end, element.close_start, content))

    # --- one element ----------------------------------------------------------------------------------------

    def _element(self, element: _Element, path: KeyPath, identity: str | None, wanted: _Trie) -> None:
        self._attributes(element, path, identity, wanted)
        text_leaf = wanted.kids.get("#text")
        text = None if text_leaf is None else _scalar_text(text_leaf.leaf)
        children = {step: kid for step, kid in wanted.kids.items() if step[:1] not in ("@", "#")}
        if text and children:
            raise RenderRefused(MIXED_CONTENT_REFUSED)
        if element.children:
            if text:
                self._set_content(element, _escape_text(text))
            else:
                self._children(element, path, children)
        elif children:
            described = [item for step, kid in children.items() for item in _described_from_trie(step, kid)]
            if element.text and element.has_markup:
                raise RenderRefused(COMMENT_IN_VALUE_REFUSED)
            self._set_content(element, self._block(element, described))
        else:
            self._text_leaf(element, path, text)

    def _attributes(self, element: _Element, path: KeyPath, identity: str | None, wanted: _Trie) -> None:
        wanted_names = {step[1:]: kid for step, kid in wanted.kids.items() if step.startswith("@")}
        present = {item.name for item in element.attributes}
        for attribute in element.attributes:
            if attribute.name == identity:
                continue
            kid = wanted_names.get(attribute.name)
            if kid is None:
                self._edits.append(TextEdit(attribute.lead, attribute.end, ""))
                continue
            value = _scalar_text(kid.leaf)
            copy = self._copies.get((*path, f"@{attribute.name}"))
            if copy is not None:
                if (attribute.raw, attribute.quote) != (copy.raw, copy.quote):
                    self._edits.append(
                        TextEdit(attribute.value_start - 1, attribute.end, f"{copy.quote}{copy.raw}{copy.quote}")
                    )
            elif attribute.value != value:
                self._edits.append(TextEdit(attribute.value_start - 1, attribute.end, f'"{_escape_attribute(value)}"'))
        added = []
        for name, kid in wanted_names.items():
            if name in present or name == identity:
                continue
            copy = self._copies.get((*path, f"@{name}"))
            rendered = (
                f"{copy.quote}{copy.raw}{copy.quote}"
                if copy is not None
                else f'"{_escape_attribute(_scalar_text(kid.leaf))}"'
            )
            added.append(f" {name}={rendered}")
        if added:
            self._edits.append(TextEdit(element.attributes_end, element.attributes_end, "".join(added)))

    def _text_leaf(self, element: _Element, path: KeyPath, text: str | None) -> None:
        raw = self._text[element.start_tag_end : element.close_start]
        copy = self._copies.get((*path, "#text"))
        if text is None or not text:
            if element.text:
                if element.has_markup:
                    raise RenderRefused(COMMENT_IN_VALUE_REFUSED)
                self._edits.append(TextEdit(element.start_tag_end, element.close_start, ""))
            return
        if copy is not None:
            wanted = copy.raw
        elif element.text == text:
            return
        else:
            wanted = _cdata(text) if element.has_cdata else _escape_text(text)
        if element.has_markup and element.text != text:
            raise RenderRefused(COMMENT_IN_VALUE_REFUSED)
        if element.self_closing:
            self._edits.append(TextEdit(element.attributes_end, element.end, f">{wanted}</{element.tag}>"))
        elif raw != wanted:
            self._edits.append(TextEdit(element.start_tag_end, element.close_start, wanted))

    # --- children -------------------------------------------------------------------------------------------

    def _children(self, parent: _Element, path: KeyPath, wanted: dict[str, _Trie]) -> None:
        groups = _groups(parent.children)
        removed: list[_Element] = []
        survivors: list[_Element] = []
        for segment, members in groups.items():
            kid = wanted.get(segment)
            if kid is not None and isinstance(kid.leaf, Scalar | Object):
                raise RenderRefused(SHAPE_REFUSED)
            if kid is None:
                removed.extend(member for member, _ in members)
            elif isinstance(kid.leaf, Array):
                self._group([member for member, _ in members], segment, kid.leaf)
            elif len(members) == 1:
                self._element(members[0][0], (*path, segment), members[0][1], kid)
                survivors.append(members[0][0])
            else:
                first, *rest = (member for member, _ in members)
                described = _described_from_trie(segment, kid)
                self._replace(first, described[0])
                removed.extend(rest)
        fresh = [(segment, kid) for segment, kid in wanted.items() if segment not in groups]
        self._insert_new(parent, fresh, survivors, removed)

    def _insert_new(
        self, parent: _Element, fresh: list[tuple[str, _Trie]], survivors: list[_Element], removed: list[_Element]
    ) -> None:
        placed: dict[int, tuple[_Element, list[_Described]]] = {}
        takes_the_place_of_removed: list[_Described] = []
        for segment, kid in fresh:
            described = _described_from_trie(segment, kid)
            anchor = self._anchor(parent, _tag_of(segment), survivors, removed)
            if anchor is None:
                takes_the_place_of_removed.extend(described)
            else:
                placed.setdefault(id(anchor), (anchor, []))[1].extend(described)
        if takes_the_place_of_removed:
            first, removed = removed[0], removed[1:]
            self._replace(first, takes_the_place_of_removed[0], *takes_the_place_of_removed[1:])
        for anchor, items in placed.values():
            self._insert_after(anchor, items)
        for element in removed:
            self._delete(element)

    def _anchor(
        self, parent: _Element, tag: str, survivors: list[_Element], removed: list[_Element]
    ) -> _Element | None:
        """The sibling a new element goes after, or `None` when it takes the place of a removed one."""
        for element in reversed(survivors):
            if element.tag == tag:
                return element
        if survivors:
            return survivors[-1]
        if removed:
            return None
        return parent.children[-1]

    def _group(self, members: list[_Element], segment: str, wanted: Array) -> None:
        tag = _tag_of(segment)
        old = [_object_of(member) for member in members]
        new = list(wanted.elements)
        matcher = SequenceMatcher(None, old, new, autojunk=False)
        for kind, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if kind == "equal":
                continue
            shared = min(old_end - old_start, new_end - new_start)
            for offset in range(shared):
                self._replace(members[old_start + offset], _described_from_object(tag, new[new_start + offset]))
            for member in members[old_start + shared : old_end]:
                self._delete(member)
            extra = [_described_from_object(tag, node) for node in new[new_start + shared : new_end]]
            if not extra:
                continue
            if old_start + shared < len(members):
                self._insert_before(members[old_start + shared], extra)
            else:
                self._insert_after(members[-1], extra)

    # --- placing elements -----------------------------------------------------------------------------------

    def _replace(self, element: _Element, described: _Described, *more: _Described) -> None:
        indent = self._indent_of(element)
        pieces = [self._serialize(described, indent)]
        pieces.extend(self._serialize(item, indent) for item in more)
        glue = "" if indent is None else self._nl + indent
        self._edits.append(TextEdit(element.start, element.end, glue.join(pieces)))

    def _insert_after(self, anchor: _Element, items: list[_Described]) -> None:
        indent = self._indent_of(anchor)
        if indent is None:
            text = "".join(self._serialize(item, None) for item in items)
        else:
            text = "".join(f"{self._nl}{indent}{self._serialize(item, indent)}" for item in items)
        self._edits.append(TextEdit(anchor.end, anchor.end, text))

    def _insert_before(self, anchor: _Element, items: list[_Described]) -> None:
        indent = self._indent_of(anchor)
        if indent is None:
            text = "".join(self._serialize(item, None) for item in items)
            self._edits.append(TextEdit(anchor.start, anchor.start, text))
            return
        text = "".join(f"{indent}{self._serialize(item, indent)}{self._nl}" for item in items)
        start = self._line_start(anchor.start)
        self._edits.append(TextEdit(start, start, text))

    def _delete(self, element: _Element) -> None:
        """Remove `element`, taking its whole line when nothing else is on it."""
        line_end = self._text.find("\n", element.end)
        line_end = len(self._text) if line_end < 0 else line_end
        alone = self._indent_of(element) is not None and not self._text[element.end : line_end].strip(" \t\r")
        if alone:
            self._edits.append(TextEdit(self._line_start(element.start), min(line_end + 1, len(self._text)), ""))
        else:
            self._edits.append(TextEdit(element.start, element.end, ""))


__all__ = [
    "COMMENT_IN_VALUE_REFUSED",
    "DOCTYPE_REFUSED",
    "IDENTITY_ATTRIBUTES",
    "MIXED_CONTENT_REFUSED",
    "NAMESPACE_REFUSED",
    "SCANNER_DISAGREES",
    "SHAPE_REFUSED",
    "XmlAdapter",
]
