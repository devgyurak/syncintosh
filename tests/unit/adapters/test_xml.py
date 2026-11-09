# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the XML adapter.

The load-bearing property is the same as JSONC's (``AGENTS.md`` §2.5): parse then render with no edit returns the
original bytes (the IDE's ` />`, `&#10;` and CDATA included), and an edit touches only the span that changed. The
fixture is shaped from the structure of DataGrip's `options/*.xml` with synthetic values.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.adapters.xml import (
    COMMENT_IN_VALUE_REFUSED,
    IDENTITY_ATTRIBUTES,
    MIXED_CONTENT_REFUSED,
    XmlAdapter,
)
from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import ContentParseError, RenderRefused
from syncintosh.core.merge import merge_tree
from syncintosh.core.ports import TextEdit, Verbatim
from syncintosh.core.tree import ABSENT, Array, EmptyObject, Object, Scalar, Tree

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "xml"
ADAPTER = XmlAdapter()


def _fixture() -> str:
    return (FIXTURES / "options.xml").read_text(encoding="utf-8")


def _render(wanted: str, template: str) -> str:
    return ADAPTER.render(ADAPTER.parse(wanted), template)


# --------------------------------------------------------------------------- round-trip


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(lambda text: text, id="as-written"),
        pytest.param(lambda text: text + "\n", id="trailing-newline"),
        pytest.param(lambda text: text.replace("\n", "\r\n"), id="crlf"),
        pytest.param(lambda text: "﻿" + text, id="bom"),
        pytest.param(lambda text: '<?xml version="1.0" encoding="UTF-8"?>\n' + text, id="declaration"),
        pytest.param(lambda text: text.replace("\n  ", "").replace("\n", ""), id="one-line"),
    ],
)
def test_the_fixture_round_trips_byte_for_byte(transform) -> None:
    # given
    text = transform(_fixture())

    # when
    rendered = ADAPTER.render(ADAPTER.parse(text), text)

    # then
    assert rendered == text


def test_an_unchanged_render_makes_no_edit() -> None:
    # given
    text = _fixture()

    # when
    edits = ADAPTER.render_edits(ADAPTER.parse(text), text)

    # then
    assert edits == ()


# --------------------------------------------------------------------------- the key-path mapping


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("component[DefaultFont]", "option[FONT_SIZE]", "@value"), "14", id="option-value"),
        pytest.param(("component[Registry]", "entry[ide.example.flag]", "@source"), "SYSTEM", id="entry-by-key"),
        pytest.param(("component[DatabaseSettings]", "option[NOTE]", "#text"), "a < b && c", id="cdata-text"),
        pytest.param(
            ("component[DatabaseSettings]", "option[QUERY]", "@value"),
            'select 1;\n\tfrom t where a < "b"',
            id="decoded-entities",
        ),
        pytest.param(("global_color_scheme", "@name"), "Example Dark", id="name-that-is-a-value"),
    ],
)
def test_a_leaf_lives_at_its_segment_path(path, value) -> None:
    # given
    tree = ADAPTER.parse(_fixture())

    # when
    leaf = tree.value(path)

    # then — every value is a string: XML has no types
    assert leaf == Scalar("str", value)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(("component[FindSettings]", "mask"), id="repeated-text-element"),
        pytest.param(("component[DatabaseSettings]", "parameter-pattern"), id="repeated-attributes"),
        pytest.param(("component[DatabaseSettings]", "csv-format"), id="repeated-named-element-outside-the-table"),
        pytest.param(("component[CsvSettings]", "set", "option"), id="identity-tag-without-its-identity"),
    ],
)
def test_a_repeated_group_is_one_array_leaf(path) -> None:
    # given
    tree = ADAPTER.parse(_fixture())

    # when
    leaf = tree.value(path)

    # then
    assert isinstance(leaf, Array)
    assert all(isinstance(element, Object) for element in leaf.elements)


def test_an_array_element_keeps_its_identity_attribute() -> None:
    # given
    tree = ADAPTER.parse(_fixture())

    # when
    leaf = tree.value(("component[DatabaseSettings]", "csv-format"))

    # then
    assert isinstance(leaf, Array)
    assert leaf.elements[0] == Object((("@name", Scalar("str", "CSV")), ("@id", Scalar("str", "a1"))))


def test_the_identity_attribute_is_a_segment_and_not_also_a_leaf() -> None:
    # given
    tree = ADAPTER.parse(_fixture())

    # when
    leaf = tree.value(("component[DefaultFont]", "@name"))

    # then
    assert leaf is ABSENT


def test_an_element_with_only_its_identity_is_an_empty_object() -> None:
    # given
    tree = ADAPTER.parse(_fixture())

    # when
    leaf = tree.value(("component[Empty]",))

    # then
    assert leaf == EmptyObject()


def test_the_root_is_not_a_segment_and_its_attributes_are_depth_one() -> None:
    # given
    text = '<application version="2"><component name="A" /></application>'

    # when
    tree = ADAPTER.parse(text)

    # then
    assert tree.value(("@version",)) == Scalar("str", "2")
    assert tree.value(("component[A]",)) == EmptyObject()


def test_a_lone_element_outside_the_table_is_its_tag() -> None:
    # given
    text = '<application><set note="x" /></application>'

    # when
    tree = ADAPTER.parse(text)

    # then
    assert tree.value(("set", "@note")) == Scalar("str", "x")


def test_a_table_tag_is_identified_even_when_it_has_no_siblings() -> None:
    # given
    one = ADAPTER.parse('<application><component name="A"><option name="x" value="1" /></component></application>')
    two = ADAPTER.parse(
        '<application><component name="A"><option name="x" value="1" /><option name="y" value="2" /></component>'
        "</application>"
    )

    # when
    shared = ("component[A]", "option[x]", "@value")

    # then — the path does not move when a sibling arrives
    assert one.value(shared) == two.value(shared) == Scalar("str", "1")


def test_the_identity_table_is_fixed() -> None:
    # when
    table = dict(IDENTITY_ATTRIBUTES)

    # then
    assert table == {
        "component": "name",
        "option": "name",
        "macro": "name",
        "property": "name",
        "template": "name",
        "entry": "key",
        "action": "id",
        "codeStyleSettings": "language",
    }


def test_the_order_of_siblings_is_not_compared() -> None:
    # given — the IDE sorts components when it saves
    first = '<application><component name="A" /><component name="B" /></application>'
    second = '<application><component name="B" /><component name="A" /></application>'

    # when
    equal = ADAPTER.parse(first) == ADAPTER.parse(second)

    # then
    assert equal is True


def test_whitespace_only_text_is_formatting() -> None:
    # given
    text = '<application><component name="A">\n</component></application>'

    # when
    tree = ADAPTER.parse(text)

    # then
    assert tree.value(("component[A]",)) == EmptyObject()


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n", id="whitespace"),
        pytest.param("<!-- only a comment -->", id="no-root"),
        pytest.param('<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>', id="doctype-with-entity"),
        pytest.param("<!DOCTYPE a><a />", id="doctype"),
        pytest.param("<a>&x;</a>", id="undefined-entity"),
        pytest.param("<a>&#1114112;</a>", id="character-out-of-range"),
        pytest.param("<a>text<b /></a>", id="mixed-content"),
        pytest.param("<a><b /></c>", id="mismatched-end-tag"),
        pytest.param("<a><b></a>", id="unclosed-element"),
        pytest.param("<a /><b />", id="two-roots"),
        pytest.param("<a /> trailing", id="text-after-root"),
        pytest.param('<a x="1" x="2" />', id="duplicate-attribute"),
        pytest.param("<a:b />", id="namespaced-tag"),
        pytest.param('<a x:y="1" />', id="namespaced-attribute"),
        pytest.param('<?xml version="1.0" encoding="ISO-8859-1"?><a />', id="other-encoding"),
        pytest.param("<a><!-- open", id="unterminated-comment"),
        pytest.param("<a><![CDATA[ open", id="unterminated-cdata"),
        pytest.param("<a><? open", id="unterminated-instruction"),
        pytest.param("< a />", id="malformed-start-tag"),
        pytest.param('<a x="<" />', id="angle-bracket-in-attribute"),
        pytest.param("<a>" * 300 + "</a>" * 300, id="too-deep"),
    ],
)
def test_text_that_is_not_supported_xml_is_refused(text) -> None:
    # when & then
    with pytest.raises(ContentParseError):
        ADAPTER.parse(text)


def test_a_refusal_names_a_cause_and_never_a_value() -> None:
    # given
    text = '<a secret="hunter2">hunter2<b /></a>'

    # when & then
    with pytest.raises(ContentParseError) as raised:
        ADAPTER.parse(text)
    assert "hunter2" not in str(raised.value)
    assert str(raised.value) == MIXED_CONTENT_REFUSED


# --------------------------------------------------------------------------- minimal edits

BASE = """<application>
  <!-- keep me -->
  <component name="Fonts">
    <option name="SIZE" value="14" />
    <option name="FACE" value="Mono" />
  </component>
  <component name="Find">
    <mask>*.java</mask>
    <mask>*.kt</mask>
  </component>
  <component name="Empty" />
  <component name="Note">
    <option name="TEXT"><![CDATA[a < b]]></option>
  </component>
</application>
"""


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param('value="14"', 'value="15"', id="changed-value"),
        pytest.param('value="14" />', 'value="14" extra="1" />', id="added-attribute"),
        pytest.param(' value="Mono"', "", id="removed-attribute"),
        pytest.param('    <option name="FACE" value="Mono" />\n', "", id="removed-element-takes-its-line"),
        pytest.param(
            '    <option name="FACE" value="Mono" />\n',
            '    <option name="FACE" value="Mono" />\n    <option name="WEIGHT" value="b" />\n',
            id="added-element-after-the-last-sibling-of-its-tag",
        ),
        pytest.param("<mask>*.kt</mask>", "<mask>*.kts</mask>", id="one-array-element-changed"),
        pytest.param(
            "    <mask>*.kt</mask>\n", "    <mask>*.kt</mask>\n    <mask>*.py</mask>\n", id="array-element-appended"
        ),
        pytest.param("    <mask>*.java</mask>\n", "", id="array-element-removed"),
        pytest.param(
            "    <mask>*.java</mask>\n", "    <mask>*.c</mask>\n    <mask>*.java</mask>\n", id="array-element-prepended"
        ),
        pytest.param(
            '<component name="Empty" />',
            '<component name="Empty">\n    <option name="A" value="1" />\n  </component>',
            id="an-empty-element-is-opened",
        ),
        pytest.param('<component name="Empty" />', '<component name="Empty" extra="1" />', id="attribute-on-empty"),
        pytest.param(
            "  </component>\n</application>",
            '  </component>\n  <component name="New">\n    <option name="N" value="1" />\n  </component>\n'
            "</application>",
            id="added-component-at-the-end",
        ),
        pytest.param("<![CDATA[a < b]]>", "<![CDATA[a < c]]>", id="cdata-stays-cdata"),
        pytest.param("<![CDATA[a < b]]>", "<![CDATA[a ]]]]><![CDATA[> b]]>", id="cdata-with-a-terminator-is-split"),
        pytest.param('value="14"', 'value="a &lt; &quot;b&quot;&#10;&#9;&amp; c &gt;"', id="escapes-follow-the-ide"),
        pytest.param(
            '<component name="Empty" />',
            '<component name="Empty"><option name="T">x &amp; y</option></component>',
            id="x",
        ),
    ],
)
def test_an_edit_changes_only_the_span_that_changed(old, new) -> None:
    # given — what the file should look like after the change, written as the IDE would write it
    wanted = BASE.replace(old, new, 1)
    wanted_tree = ADAPTER.parse(wanted)

    # when
    rendered = ADAPTER.render(wanted_tree, BASE)

    # then — the comment, the layout and the trailing newline are all as they were
    assert ADAPTER.parse(rendered) == wanted_tree
    assert rendered == wanted or '<option name="T">' in new
    assert "<!-- keep me -->" in rendered
    assert rendered.endswith("\n")


def test_every_edit_is_reported_as_a_span_of_the_template() -> None:
    # given
    wanted = BASE.replace('value="14"', 'value="15"', 1)

    # when
    edits = ADAPTER.render_edits(ADAPTER.parse(wanted), BASE)

    # then
    assert edits == (TextEdit(BASE.index('"14"'), BASE.index('"14"') + 4, '"15"'),)


def test_text_that_becomes_empty_leaves_an_empty_element() -> None:
    # given
    template = '<application><component name="C"><option name="T">hello</option></component></application>'
    wanted = '<application><component name="C"><option name="T" /></component></application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert ADAPTER.parse(rendered) == ADAPTER.parse(wanted)


def test_text_is_added_to_an_empty_element() -> None:
    # given
    template = '<application><component name="C"><option name="T" /></component></application>'
    wanted = '<application><component name="C"><option name="T">hello</option></component></application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert rendered == wanted


def test_an_element_that_swaps_text_for_children_is_rebuilt() -> None:
    # given
    template = '<application><component name="C">hello</component></application>'
    wanted = '<application><component name="C"><option name="T" value="1" /></component></application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert ADAPTER.parse(rendered) == ADAPTER.parse(wanted)


def test_an_element_that_swaps_children_for_text_is_rebuilt() -> None:
    # given
    template = '<application><component name="C"><option name="T" value="1" /></component></application>'
    wanted = '<application><component name="C">hello</component></application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert ADAPTER.parse(rendered) == ADAPTER.parse(wanted)


def test_a_group_that_becomes_a_single_element_is_replaced() -> None:
    # given — one `<mask>` on this machine, two on the other
    template = '<application><component name="F"><mask>a</mask></component></application>'
    wanted = '<application><component name="F"><mask>a</mask><mask>b</mask></component></application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert ADAPTER.parse(rendered) == ADAPTER.parse(wanted)


def test_a_group_that_shrinks_to_one_element_is_replaced() -> None:
    # given
    template = '<application><component name="F"><mask>a</mask><mask>b</mask></component></application>'
    wanted = '<application><component name="F"><mask>c</mask></component></application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert ADAPTER.parse(rendered) == ADAPTER.parse(wanted)


def test_a_new_element_takes_the_place_of_the_only_removed_one() -> None:
    # given
    template = '<application>\n  <component name="A" />\n</application>'
    wanted = '<application>\n  <component name="B" />\n</application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert rendered == wanted


def test_a_changed_value_with_a_comment_inside_is_refused() -> None:
    # given
    template = '<application><component name="C"><option name="T">a<!-- note -->b</option></component></application>'
    wanted = '<application><component name="C"><option name="T">z</option></component></application>'

    # when & then
    with pytest.raises(RenderRefused, match=COMMENT_IN_VALUE_REFUSED):
        _render(wanted, template)


def test_a_tree_the_format_cannot_write_is_refused() -> None:
    # given — a scalar where an element belongs, as a half-resolved merge could leave
    tree = Tree.create({("component[A]",): Scalar("str", "x")})

    # when & then
    with pytest.raises(RenderRefused):
        ADAPTER.render(tree, "<application><component name='A' /></application>")


def test_the_empty_element_style_of_the_file_is_kept() -> None:
    # given — a file written without the space before the slash
    template = '<application><component name="A"/></application>'
    wanted = '<application><component name="A"/><component name="B"/></application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert rendered == wanted


def test_a_single_quoted_value_is_rewritten_with_double_quotes() -> None:
    # given
    template = "<application><option name='A' value='1' /></application>"
    wanted = '<application><option name="A" value="2" /></application>'

    # when
    rendered = _render(wanted, template)

    # then
    assert ADAPTER.parse(rendered) == ADAPTER.parse(wanted)


# --------------------------------------------------------------------------- verbatim


def test_a_verbatim_attribute_is_copied_as_it_was_written() -> None:
    # given — the same value, spelled with a literal `>` in the source and `&gt;` in the template
    source = '<application><option name="A" value="a>b" /></application>'
    template = '<application><option name="A" value="a&gt;b" /></application>'
    path = ("option[A]", "@value")

    # when
    rendered = ADAPTER.render(
        ADAPTER.parse(template), template, verbatim=Verbatim(text=source, paths=frozenset({path}))
    )

    # then
    assert rendered == source


def test_a_verbatim_attribute_with_another_value_is_not_copied() -> None:
    # given
    source = '<application><option name="A" value="other" /></application>'
    template = '<application><option name="A" value="a&gt;b" /></application>'
    path = ("option[A]", "@value")

    # when
    rendered = ADAPTER.render(
        ADAPTER.parse(template), template, verbatim=Verbatim(text=source, paths=frozenset({path}))
    )

    # then
    assert rendered == template


def test_a_verbatim_attribute_is_added_as_it_was_written() -> None:
    # given
    source = "<application><option name='A' value='a>b' /></application>"
    template = '<application><option name="A" /></application>'
    wanted = ADAPTER.parse(source)
    path = ("option[A]", "@value")

    # when
    rendered = ADAPTER.render(wanted, template, verbatim=Verbatim(text=source, paths=frozenset({path})))

    # then
    assert rendered == "<application><option name=\"A\" value='a>b' /></application>"


def test_verbatim_text_is_copied_with_its_cdata() -> None:
    # given
    source = '<application><option name="A"><![CDATA[a<b]]></option></application>'
    template = '<application><option name="A">a&lt;b</option></application>'
    path = ("option[A]", "#text")

    # when
    rendered = ADAPTER.render(
        ADAPTER.parse(template), template, verbatim=Verbatim(text=source, paths=frozenset({path}))
    )

    # then
    assert rendered == source


def test_verbatim_text_is_copied_into_an_empty_element() -> None:
    # given
    source = '<application><option name="A">x</option></application>'
    template = '<application><option name="A" /></application>'

    # when
    rendered = ADAPTER.render(
        ADAPTER.parse(source), template, verbatim=Verbatim(text=source, paths=frozenset({("option[A]", "#text")}))
    )

    # then
    assert rendered == source


# --------------------------------------------------------------------------- merge semantics


def _merge(base: str, local: str, remote: str):
    return merge_content(base.encode(), local.encode(), remote.encode(), TextMergeDriver(), ADAPTER)


MERGE_BASE = """<application>
  <component name="Fonts">
    <option name="SIZE" value="14" />
    <option name="FACE" value="Mono" />
  </component>
</application>"""


def test_a_change_on_one_side_wins() -> None:
    # given
    local = MERGE_BASE.replace('value="14"', 'value="15"')
    remote = MERGE_BASE.replace('value="Mono"', 'value="Sans"')

    # when
    result = _merge(MERGE_BASE, local, remote)

    # then — each side's own key survives: this is a key merge, not a line merge
    assert result.conflicted is False
    assert result.data.decode() == MERGE_BASE.replace('value="14"', 'value="15"').replace(
        'value="Mono"', 'value="Sans"'
    )


def test_the_same_change_on_both_sides_is_no_conflict() -> None:
    # given
    both = MERGE_BASE.replace('value="14"', 'value="15"')

    # when
    result = _merge(MERGE_BASE, both + "\n", both)

    # then
    assert result.conflicted is False


def test_a_key_changed_differently_on_both_sides_is_the_only_conflict() -> None:
    # given
    local = MERGE_BASE.replace('value="14"', 'value="15"')
    remote = MERGE_BASE.replace('value="14"', 'value="16"')

    # when
    result = _merge(MERGE_BASE, local, remote)

    # then
    assert result.conflicted is True


def test_an_element_added_on_one_side_and_a_value_changed_on_the_other_merge() -> None:
    # given
    local = MERGE_BASE.replace('value="14" />', 'value="14" />\n    <option name="NEW" value="1" />')
    remote = MERGE_BASE.replace('value="Mono"', 'value="Sans"')

    # when
    result = _merge(MERGE_BASE, local, remote)

    # then
    assert result.conflicted is False
    assert 'name="NEW"' in result.data.decode()
    assert 'value="Sans"' in result.data.decode()


def test_an_array_changed_on_both_sides_is_one_conflict() -> None:
    # given
    base = '<application><component name="F"><mask>a</mask><mask>b</mask></component></application>'
    local = base.replace("<mask>b</mask>", "<mask>c</mask>")
    remote = base.replace("<mask>b</mask>", "<mask>d</mask>")

    # when
    result = _merge(base, local, remote)

    # then
    assert result.conflicted is True


def test_a_malformed_side_is_a_conflict_and_never_a_partial_file() -> None:
    # given
    local = MERGE_BASE.replace('value="14"', 'value="15"')
    remote = MERGE_BASE.replace("</application>", "")

    # when
    result = _merge(MERGE_BASE, local, remote)

    # then
    assert result.conflicted is True


# --------------------------------------------------------------------------- properties

_NAMES = st.sampled_from(["a", "b", "c"])
_TAGS = st.sampled_from(["component", "option", "entry", "mask", "set", "other"])
_ATTRIBUTE_NAMES = st.sampled_from(["name", "key", "value", "x"])
_TEXT = st.text(alphabet=list("xy z&<>\"'\n\t1]"), min_size=1, max_size=6).filter(lambda text: text.strip())


def _escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("\n", "&#10;")
        .replace("\t", "&#9;")
    )


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _write(element, indent: str) -> str:
    tag, attributes, text, children = element
    head = f"<{tag}" + "".join(f' {name}="{_escape_attribute(value)}"' for name, value in sorted(attributes.items()))
    if text is not None:
        return f"{indent}{head}>{_escape_text(text)}</{tag}>"
    if not children:
        return f"{indent}{head} />"
    body = "\n".join(_write(child, indent + "  ") for child in children)
    return f"{indent}{head}>\n{body}\n{indent}</{tag}>"


_elements = st.recursive(
    st.tuples(_TAGS, st.dictionaries(_ATTRIBUTE_NAMES, _NAMES, max_size=2), st.none() | _TEXT, st.just(())),
    lambda inner: st.tuples(
        _TAGS,
        st.dictionaries(_ATTRIBUTE_NAMES, _NAMES, max_size=2),
        st.none(),
        st.lists(inner, max_size=4).map(tuple),
    ),
    max_leaves=10,
)
_documents = st.tuples(st.dictionaries(_ATTRIBUTE_NAMES, _NAMES, max_size=2), st.lists(_elements, max_size=5)).map(
    lambda parts: _write(("application", parts[0], None, tuple(parts[1])), "")
)


def _rendered_or_none(wanted: str, template: str) -> str | None:
    try:
        return ADAPTER.render(ADAPTER.parse(wanted), template)
    except RenderRefused:
        return None


@settings(max_examples=200, deadline=None)
@given(text=_documents)
def test_property_a_document_renders_back_to_itself(text) -> None:
    # when
    rendered = ADAPTER.render(ADAPTER.parse(text), text)

    # then
    assert rendered == text


@settings(max_examples=300, deadline=None)
@given(wanted=_documents, template=_documents)
def test_property_a_render_parses_back_to_the_wanted_tree(wanted, template) -> None:
    # when
    rendered = _rendered_or_none(wanted, template)

    # then — a render either refuses or lands exactly on the wanted tree
    assert rendered is None or ADAPTER.parse(rendered) == ADAPTER.parse(wanted)


@settings(max_examples=200, deadline=None)
@given(base=_documents, remote=_documents)
def test_property_a_change_on_one_side_always_wins(base, remote) -> None:
    # given
    adapter_trees = (ADAPTER.parse(base), ADAPTER.parse(remote))

    # when
    merged = merge_tree(adapter_trees[0], adapter_trees[0], adapter_trees[1])

    # then
    assert merged.merged == adapter_trees[1]


@settings(max_examples=200, deadline=None)
@given(base=_documents, local=_documents, remote=_documents)
def test_property_merging_the_same_inputs_twice_is_a_no_op(base, local, remote) -> None:
    # given
    trees = [ADAPTER.parse(text) for text in (base, local, remote)]
    first = merge_tree(trees[0], trees[1], trees[2])

    # when — the merge result is the new local side
    again = merge_tree(trees[0], first.merged, trees[2])

    # then
    assert again.merged == first.merged


def test_importing_the_adapter_does_not_import_lxml() -> None:
    # given — a fresh interpreter, so the modules other tests imported are absent
    code = "import sys, syncintosh.adapters.xml; print('lxml' in sys.modules)"

    # when
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    # then — the heavy import happens at the first parse (AGENTS.md §4)
    assert result.stdout.strip() == "False"
