# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the JSONC adapter.

The load-bearing property is the round-trip (``AGENTS.md`` §2.5): parse then render with no edit
returns the original bytes, comments and trailing commas intact, and an edit touches only the
span that changed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from syncintosh.adapters import jsonc
from syncintosh.adapters.jsonc import LONE_COMMA_REFUSED, JsoncAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import ContentParseError, RenderRefused
from syncintosh.core.merge import merge_tree
from syncintosh.core.tree import Scalar, Tree, tree_from_value

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "jsonc"

_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(), children, max_size=3),
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _merged(base: str, local: str, remote: str) -> Tree:
    adapter = JsoncAdapter()
    return merge_tree(adapter.parse(base), adapter.parse(local), adapter.parse(remote)).merged


@pytest.mark.parametrize("name", ["vscode.jsonc", "vscode-tabs-crlf.jsonc"])
def test_a_fixture_round_trips_byte_for_byte(name) -> None:
    # given
    adapter = JsoncAdapter()
    text = _fixture(name)

    # when
    rendered = adapter.render(adapter.parse(text), text)

    # then
    assert rendered == text


def test_a_single_key_edit_changes_only_that_value_span() -> None:
    # given
    adapter = JsoncAdapter()
    text = _fixture("vscode.jsonc")

    # when — only editor.fontSize changes
    merged = _merged(text, text.replace('"editor.fontSize": 13', '"editor.fontSize": 15'), text)
    rendered = adapter.render(merged, text)

    # then
    assert rendered == text.replace('"editor.fontSize": 13', '"editor.fontSize": 15')


def test_an_added_key_lands_after_the_sibling_the_remote_had_before_it() -> None:
    # given
    adapter = JsoncAdapter()
    text = '{\n  "a": 1,\n  "c": 3\n}\n'
    remote = '{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n'

    # when — the remote added `b` between a and c
    rendered = adapter.render(_merged(text, text, remote), text)

    # then
    assert rendered == remote


def test_an_added_key_keeps_the_trailing_comma_style() -> None:
    # given
    adapter = JsoncAdapter()
    text = '{\n  "a": 1,\n}\n'
    remote = '{\n  "a": 1,\n  "b": 2,\n}\n'

    # when
    rendered = adapter.render(_merged(text, text, remote), text)

    # then
    assert rendered == '{\n  "a": 1,\n  "b": 2,\n}\n'


def test_a_removed_key_takes_its_comma_and_trailing_comment_but_not_the_comment_above() -> None:
    # given
    adapter = JsoncAdapter()
    text = '{\n  // keep this\n  "a": 1,\n  "b": 2, // drop this too\n  "c": 3\n}\n'
    local = '{\n  // keep this\n  "a": 1,\n  "c": 3\n}\n'

    # when — local deleted `b`
    rendered = adapter.render(_merged(text, local, text), text)

    # then
    assert rendered == local


def test_removing_the_last_key_does_not_leave_a_dangling_comma() -> None:
    # given
    adapter = JsoncAdapter()
    text = '{\n  "a": 1,\n  "b": 2\n}\n'
    local = '{\n  "a": 1\n}\n'

    # when
    rendered = adapter.render(_merged(text, local, text), text)

    # then
    assert rendered == local


def test_a_changed_array_is_rewritten_whole() -> None:
    # given
    adapter = JsoncAdapter()
    text = '{\n  "editor.rulers": [80, 100]\n}\n'
    local = '{\n  "editor.rulers": [80, 100, 120]\n}\n'

    # when
    rendered = adapter.render(_merged(text, local, text), text)

    # then
    assert json.loads(rendered) == {"editor.rulers": [80, 100, 120]}


def test_a_bom_is_preserved() -> None:
    # given
    adapter = JsoncAdapter()
    text = '\ufeff{\n  "a": 1\n}\n'

    # when
    rendered = adapter.render(adapter.parse(text), text)

    # then
    assert rendered == text


def test_crlf_and_tab_indentation_are_preserved_when_a_key_is_added() -> None:
    # given
    adapter = JsoncAdapter()
    text = '{\r\n\t"a": 1\r\n}\r\n'
    remote = '{\r\n\t"a": 1,\r\n\t"b": 2\r\n}\r\n'

    # when
    rendered = adapter.render(_merged(text, text, remote), text)

    # then
    assert rendered == '{\r\n\t"a": 1,\r\n\t"b": 2\r\n}\r\n'


def test_a_top_level_array_round_trips() -> None:
    # given
    adapter = JsoncAdapter()
    text = "[1, 2, 3]\n"

    # when
    rendered = adapter.render(adapter.parse(text), text)

    # then
    assert rendered == text


def test_two_escapes_for_one_string_are_the_same_key() -> None:
    # given
    adapter = JsoncAdapter()

    # when
    escaped = adapter.parse('{\n  "k": "\\u00e9"\n}\n')
    literal = adapter.parse('{\n  "k": "\u00e9"\n}\n')

    # then
    assert escaped == literal


@pytest.mark.parametrize(
    "text",
    [
        '{\n  "a": 1,\n  "a": 2\n}\n',  # duplicate key
        '{\n  "a": 1\n',  # unterminated object
        "[1, 2",  # unterminated array
        '"unterminated',  # unterminated string
        "/* never closed",  # unterminated block comment
        '{\n  "a" 1\n}\n',  # missing colon
        "{\n  1: 2\n}\n",  # non-string key
        '{\n  "a": 1\n}\nextra\n',  # trailing content
        '{\n  "a": 01\n}\n',  # invalid number
        '{\n  "a": zzz\n}\n',  # invalid value
    ],
)
def test_malformed_jsonc_is_a_parse_error(text) -> None:
    # given
    adapter = JsoncAdapter()

    # when & then
    with pytest.raises(ContentParseError):
        adapter.parse(text)


@pytest.mark.parametrize("text", ["", "   \n\t ", "// only a comment\n", "/* only a block */"])
def test_an_empty_document_is_the_empty_object(text) -> None:
    # given — a file with nothing, or only comments, is `{}`, not a parse failure
    adapter = JsoncAdapter()

    # when & then
    assert adapter.parse(text) == tree_from_value({})


def test_an_empty_template_gains_the_merged_object() -> None:
    # given — the local file is empty but the merge has content
    adapter = JsoncAdapter()

    # when
    rendered = adapter.render(tree_from_value({"a": 1, "b": [2]}), "")

    # then — the empty document is replaced with a valid object
    assert adapter.parse(rendered) == tree_from_value({"a": 1, "b": [2]})


def test_an_array_of_objects_round_trips() -> None:
    # given
    adapter = JsoncAdapter()
    text = '{\n  "list": [\n    {"a": 1},\n    {"b": [true, null]}\n  ]\n}\n'

    # when
    rendered = adapter.render(adapter.parse(text), text)

    # then
    assert rendered == text


def test_a_nested_value_inside_an_inline_object_changes() -> None:
    # given — the whole object is on one line, so it is rewritten as a unit
    adapter = JsoncAdapter()
    text = '{\n  "outer": {"inner": 1}\n}\n'
    local = '{\n  "outer": {"inner": 2}\n}\n'

    # when
    rendered = adapter.render(_merged(text, local, text), text)

    # then
    assert adapter.parse(rendered) == adapter.parse(local)


def test_a_commented_merge_keeps_the_comments() -> None:
    # given
    adapter = JsoncAdapter()
    base = '{\n  // shared\n  "a": 1,\n  "b": 2\n}\n'
    local = '{\n  // shared\n  "a": 1,\n  "b": 3\n}\n'
    remote = '{\n  // shared\n  "a": 1,\n  "b": 2,\n  "c": 4\n}\n'

    # when
    rendered = adapter.render(_merged(base, local, remote), local)

    # then
    assert "// shared" in rendered
    assert adapter.parse(rendered) == adapter.parse('{\n  "a": 1,\n  "b": 3,\n  "c": 4\n}\n')


def test_an_unchanged_tree_returns_the_template_untouched() -> None:
    # given
    adapter = JsoncAdapter()
    text = '{\n  "a": 1, // keep\n}\n'

    # when
    rendered = adapter.render(Tree.create({("a",): Scalar("int", 1)}), text)

    # then
    assert rendered == text


@given(value=_json_values)
def test_a_serialized_json_document_round_trips(value) -> None:
    # given
    adapter = JsoncAdapter()

    # when
    compact = json.dumps(value, ensure_ascii=False)
    pretty = json.dumps(value, ensure_ascii=False, indent=2)

    # then
    assert adapter.render(adapter.parse(compact), compact) == compact
    assert adapter.render(adapter.parse(pretty), pretty) == pretty


def test_deleting_the_last_member_then_adding_a_new_last_keeps_the_comma() -> None:
    # given — remote drops the last member `b` and adds `c`; local changed `x`
    adapter = JsoncAdapter()
    base = '{\n  "x": 0,\n  "a": 1,\n  "b": 2\n}\n'
    local = '{\n  "x": 9,\n  "a": 1,\n  "b": 2\n}\n'
    remote = '{\n  "x": 0,\n  "a": 1,\n  "c": 3\n}\n'

    # when
    merged = _merged(base, local, remote)
    rendered = adapter.render(merged, local)

    # then — the result is valid JSONC that re-parses to the merge
    assert adapter.parse(rendered) == merged


def test_a_key_added_after_a_block_comment_does_not_double_the_comma() -> None:
    # given
    adapter = JsoncAdapter()
    base = '{\n  "a": 1 /* n */,\n  "b": 2\n}\n'
    remote = '{\n  "a": 1 /* n */,\n  "new": 9,\n  "b": 2\n}\n'

    # when
    rendered = adapter.render(_merged(base, base, remote), base)

    # then
    assert rendered == remote


@pytest.mark.parametrize(
    "base",
    [
        '{\n  "a": 1 }',  # closing brace shares the member's line
        '{\n  "a": 1, "b": 2\n}\n',  # two members on one line
    ],
)
def test_an_irregular_object_layout_is_rewritten_whole(base) -> None:
    # given — a new key `c` arrives, but the layout has no per-member line to splice, and the
    # object cannot be spliced in place either (a shared line, or a comment inside it)
    adapter = JsoncAdapter()
    remote = '{"a": 1, "b": 2, "c": 3}'

    # when
    merged = _merged(base, base, remote)
    rendered = adapter.render(merged, base)

    # then — the object is reserialized and re-parses to the merge
    assert adapter.parse(rendered) == merged


def test_a_reordered_array_object_is_reserialized_and_round_trips() -> None:
    # given — remote reorders the keys inside an object held in an array
    adapter = JsoncAdapter()
    base = '{"a": [{"b": 1, "c": 2}]}'
    local = '{"a": [{"b": 1, "c": 2}], "x": 1}'
    remote = '{"a": [{"c": 2, "b": 1}]}'

    # when
    merged = _merged(base, local, remote)
    rendered = adapter.render(merged, local)

    # then — the render reproduces the reordered array (not the template's order)
    assert adapter.parse(rendered) == merged


def test_a_whitespace_only_template_gains_a_valid_object() -> None:
    # given — a template with no members at all
    adapter = JsoncAdapter()

    # when
    rendered = adapter.render(tree_from_value({"b": 1}), "\n\n")

    # then
    assert adapter.parse(rendered) == tree_from_value({"b": 1})


@pytest.mark.parametrize(
    ("base", "remote"),
    [
        # The closing brace shares the last member's line, so the whole object is rewritten.
        ('{\n  // theme\n  "theme": "dark" }', '{\n  "theme": "light",\n  "b": 1\n}'),
        # Two members share a line.
        (
            '{\n  "[python]": { // fmt\n    "editor.tabSize": 4, "editor.insertSpaces": true }\n}\n',
            '{\n  "[python]": { // fmt\n    "editor.tabSize": 8, "editor.insertSpaces": true }\n}\n',
        ),
        # A comment-only document is the empty object, but rewriting it would drop the comment.
        ("// nothing yet\n", '{\n  "b": 1\n}'),
    ],
)
def test_a_rewrite_that_would_drop_a_comment_is_refused(base, remote) -> None:
    # given
    adapter = JsoncAdapter()

    # when & then — never a silent strip of the comment
    with pytest.raises(ContentParseError):
        adapter.render(_merged(base, base, remote), base)


def test_a_refused_comment_rewrite_surfaces_as_a_conflict() -> None:
    # given — both sides changed the same irregular object, which holds a comment
    adapter = JsoncAdapter()
    base = '{\n  // theme\n  "a": 1 }'
    local = '{\n  // theme\n  "a": 1, "c": 2 }'
    remote = '{\n  "a": 1,\n  "b": 3\n}'

    # when
    result = merge_content(base.encode(), local.encode(), remote.encode(), TextMergeDriver(), adapter)

    # then — a conflict, not a published file with the comment stripped
    assert result.conflicted is True


@pytest.mark.parametrize("value", ['"https://x"', '"a\\"b"', '"/* not a comment */"', '"// nope"'])
def test_a_comment_like_sequence_inside_a_string_does_not_block_a_rewrite(value) -> None:
    # given — an inline object whose string holds something comment-shaped
    adapter = JsoncAdapter()
    inline = '{"u": ' + value + "}"
    merged = _merged(inline, inline, '{"u": ' + value + ', "b": 2}')

    # when
    rendered = adapter.render(merged, inline)

    # then — the string is not mistaken for a comment, so the rewrite is allowed
    assert adapter.parse(rendered) == merged


def test_a_brace_delimited_empty_object_still_splices_and_keeps_its_comment() -> None:
    # given — an empty object with a comment is a real object with a line to splice into
    adapter = JsoncAdapter()
    template = "{\n  // note\n}\n"

    # when
    rendered = adapter.render(tree_from_value({"b": 2}), template)

    # then
    assert "// note" in rendered
    assert adapter.parse(rendered) == tree_from_value({"b": 2})


def test_a_huge_integer_is_a_parse_error_not_a_crash() -> None:
    # given — a Python int conversion limit would raise ValueError
    adapter = JsoncAdapter()

    # when & then
    with pytest.raises(ContentParseError):
        adapter.parse('{"n": ' + "1" * 5000 + "}")


def test_a_deeply_nested_document_is_a_parse_error_not_a_crash() -> None:
    # given — nesting deep enough to exhaust the stack
    adapter = JsoncAdapter()

    # when & then
    with pytest.raises(ContentParseError):
        adapter.parse("[" * 3000 + "]" * 3000)


def test_a_crlf_delete_removes_the_whole_line() -> None:
    # given — CRLF line endings must be handled by the "alone on its line" check
    adapter = JsoncAdapter()
    base = '{\r\n\t"a": 1,\r\n\t"b": 2,\r\n\t"c": 3\r\n}\r\n'
    local = '{\r\n\t"a": 1,\r\n\t"c": 3\r\n}\r\n'

    # when
    rendered = adapter.render(_merged(base, local, base), base)

    # then
    assert rendered == local


@given(base=_json_values, local=_json_values, remote=_json_values)
def test_a_render_always_reparses_to_the_merged_tree(base, local, remote) -> None:
    # given
    adapter = JsoncAdapter()
    base_text = json.dumps(base, ensure_ascii=False, indent=2)
    local_text = json.dumps(local, ensure_ascii=False, indent=2)
    remote_text = json.dumps(remote, ensure_ascii=False, indent=2)

    # when
    merged = merge_tree(adapter.parse(base_text), adapter.parse(local_text), adapter.parse(remote_text))
    rendered = adapter.render(merged.merged, local_text)

    # then — the invariant content_merge relies on
    assert adapter.parse(rendered) == merged.merged


def test_a_key_added_as_the_first_member_opens_the_object_with_a_comma_after_it() -> None:
    # given — a tree whose *first* member is not in the template (the shape `projection.graft`
    # produces, since it keeps the live file's order)
    adapter = JsoncAdapter()
    text = '{\n  "credsStore": "desktop",\n  "features": {\n    "hooks": "true"\n  }\n}\n'
    tree = tree_from_value({"auths": {"a": {"b": "c"}}, "credsStore": "desktop", "features": {"hooks": "true"}})
    expected = (
        '{\n  "auths": {\n    "a": {\n      "b": "c"\n    }\n  },\n'
        '  "credsStore": "desktop",\n  "features": {\n    "hooks": "true"\n  }\n}\n'
    )

    # when — rendering must put it first, and separate it from what follows
    rendered = adapter.render(tree, text)

    # then — reaching for the closing brace instead would leave the last member without its comma
    assert rendered == expected
    assert adapter.parse(rendered) == tree


def test_a_single_line_object_is_spliced_in_place() -> None:
    # given — a one-line object, the shape a re-attached machine-local key meets
    adapter = JsoncAdapter()

    # when — a key arrives
    added = adapter.render(tree_from_value({"a": 1, "b": 2}), '{"a": 1}')

    # then — the layout is kept, not re-flowed to member-per-line
    assert added == '{"a": 1, "b": 2}'


@pytest.mark.parametrize(
    ("template", "tree", "expected"),
    [
        ('{"a": 1, "auths": {"x": 1}}', {"a": 1}, '{"a": 1}'),  # a middle member goes
        ('{"auths": {"x": 1}, "a": 1}', {"a": 1}, '{"a": 1}'),  # the first member goes
        ('{"a": 1,"auths": {"x": 1}}', {"a": 1}, '{"a": 1}'),  # a tight separator goes with it
        ('{"auths": {"x": 1}}', {}, "{}"),  # the only member goes
        ("{ }", {}, "{ }"),  # and the padding survives
    ],
)
def test_a_member_leaving_a_single_line_object_keeps_the_layout(template, tree, expected) -> None:
    # when
    rendered = JsoncAdapter().render(tree_from_value(tree), template)

    # then
    assert rendered == expected


def test_a_single_line_object_with_a_comment_cannot_be_spliced() -> None:
    # given — a splice would have to move or swallow the comment inside the object
    # when & then — refusing is the verdict, so the caller reports it instead of dropping it
    with pytest.raises(ContentParseError):
        JsoncAdapter().render(tree_from_value({"a": 1, "b": 2}), '{"a": 1 /* note */}')


@pytest.mark.parametrize(
    ("template", "repo", "worked"),
    [
        ('{"a": 1}', {"a": 1}, {"a": 1, "noise": 5}),
        ('{"a": 1,"b": 2}', {"a": 1, "b": 2}, {"a": 1, "b": 2, "noise": 5}),
        ('{ "a": 1 }', {"a": 1}, {"a": 1, "noise": 5}),
        ("{ }", {}, {"noise": 5}),
    ],
)
def test_splicing_a_single_line_object_is_a_byte_exact_round_trip(template, repo, worked) -> None:
    # given — the projection re-attaches a stripped key and then strips it again
    adapter = JsoncAdapter()

    # when
    restored = adapter.render(tree_from_value(worked), template)
    projected = adapter.render(tree_from_value(repo), restored)

    # then — anything else would make the apply ledger's digest drift forever
    assert projected == template


def test_a_removed_member_is_not_copied_back_into_the_gap() -> None:
    # given — a member in the middle goes, and a new one arrives
    adapter = JsoncAdapter()
    text = '{"a":1,"x":2,"b":3}'
    remote = '{"a":1,"b":3,"c":4}'

    # when
    merged = _merged(text, text, remote)
    rendered = adapter.render(merged, text)

    # then — the separator between `a` and the new member must not be the slice that still holds
    # `x`, or the removed member comes back and the render stops matching the tree
    assert merged == _merged(text, text, remote)
    assert "x" not in rendered
    assert adapter.parse(rendered) == merged


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ('{"auths": {"x": 1},}', "{}"),  # a trailing comma goes with the last member
        ('{"auths": {"x": 1}}', "{}"),
        ('{ "auths": {"x": 1} , }', "{ }"),  # the padding after `{` stays
    ],
)
def test_emptying_a_single_line_object_leaves_a_valid_object(template, expected) -> None:
    # when — the tree says the object is empty
    rendered = JsoncAdapter().render(tree_from_value({}), template)

    # then — `{,}` would not parse, and the file would be unprojectable on every later sync
    assert rendered == expected
    assert JsoncAdapter().parse(rendered) == tree_from_value({})


def test_a_block_comment_whose_last_line_starts_with_slashes_moves_as_one_note_with_its_member() -> None:
    # given — a block comment documents `a`; its closing line starts with `//`, and a tree adds `b` before `a`
    adapter = JsoncAdapter()
    text = '{\n  /* note\n  // still inside */\n  "a": 1\n}\n'
    tree = tree_from_value({"b": 2, "a": 1})

    # when
    rendered = adapter.render(tree, text)

    # then — `b` goes above the whole block, so the note stays directly above `a` and is not handed to `b`;
    # the new member is never placed inside the comment, where it would parse as nothing
    assert rendered == '{\n  "b": 2,\n  /* note\n  // still inside */\n  "a": 1\n}\n'
    assert adapter.parse(rendered) == tree


# The renderer's real contract, fuzzed: a settings file as an editor might have written it, against
# a tree a merge could produce from it. Every layout shape that has bitten this renderer is a knob
# here — one line or member-per-line, tight or spaced, padded braces, a trailing comma, CRLF, and a
# comment in each position it can sit in — and the tree is either derived from the file (a key
# reordered, added, dropped, or changed) or an unrelated document.
# Keys come from a tiny pool on purpose: a merge hands the renderer a tree that shares most of its
# keys with the file, so the interesting shapes are reorderings and one-key edits, not two unrelated
# documents. An unrelated key set exercises the "replace everything" path and little else.
_FUZZ_KEYS = st.sampled_from(["a", "b", "**/*.pyc", "**/.git", "d"])

# A value may *contain* comment punctuation — a glob like `**/*.pyc`, a URL, a snippet. Those bytes
# are inside a string and are not a comment, which is a distinction the renderer has to keep.
_FUZZ_TEXT = st.sampled_from(
    ["", "x", "**/*.pyc", "**/.git", "http://host:8080/path", "a // b", "a /* b", "*/", "//", "\t"]
)

_FUZZ_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | _FUZZ_TEXT,
    lambda children: st.lists(children, max_size=2) | st.dictionaries(_FUZZ_KEYS, children, max_size=3),
)

_MUTATIONS = st.sampled_from(["reorder", "add", "drop", "change", "nested", "independent"])

# Where a comment can sit, and what it can say. The *text* matters as much as the position: a
# settings file documents a path glob inside a line comment (`// hides **/*.map below`), which is a
# `/*` and a `*/` and a `"` that must never be read as syntax.
_COMMENT_PLACEMENTS = st.sampled_from(
    [
        "none",
        "before-first",  # right after the opening brace
        "after-first",  # at the end of the first member's line
        "own-line",  # on its own line between two members
        "before-close",  # above the closing brace
        "in-empty-object",  # inside the first member's `{}`, where an added key has to land after it
    ]
)

# Every text here is one a person writes and a raw byte scan reads wrongly: a `/*` inside a `//`
# comment, a `*/` where none closes anything, a quote that is not a string, a glob that is both. The
# benign `// fuzz` is in the unit tests, which do not need a generator to find it.
_COMMENT_TEXTS = st.sampled_from(
    [
        "// hides **/*.map below",
        "// /* nested */",
        "// */",
        '// say "hi"',
        "/* fuzz\nstill */",
        '/* say "hi" */',
    ]
)

_LAYOUT_FIELDS = {
    "indent": st.sampled_from([None, "", "  ", "\t", " "]),
    "separators": st.sampled_from([(",", ":"), (", ", ": ")]),
    "newline": st.sampled_from(["\n", "\r\n"]),
    "trailing": st.booleans(),
    "pad": st.booleans(),
}

# An indent, so a comment between two *root* members can be placed unambiguously — an empty indent
# makes a nested member's line look exactly like a root one, which puts a comment inside a value.
_LAYOUT_FIELDS_INDENTED = {**_LAYOUT_FIELDS, "indent": st.sampled_from(["  ", "\t", " "])}

# The same fields as a strategy, for a property that draws its comments separately.
_LAYOUT_INDENTED = st.fixed_dictionaries(_LAYOUT_FIELDS_INDENTED)

# Up to two comments at *different* places: the shapes that broke the renderer need one whose text
# holds `/*` and a later one that closes it, which is a document a person writes and a single comment
# cannot express. Two at the same place would put one comment inside the other's text — text the
# parser would read as a member, which is not a document anyone has.
_COMMENT_LISTS = st.lists(st.tuples(_COMMENT_PLACEMENTS, _COMMENT_TEXTS), max_size=2, unique_by=lambda pair: pair[0])

# The same, with at least one comment: every example of the edit property has to exercise one.
_COMMENT_LISTS_AT_LEAST_ONE = st.lists(
    st.tuples(_COMMENT_PLACEMENTS, _COMMENT_TEXTS), min_size=1, max_size=2, unique_by=lambda pair: pair[0]
)
# A comma alone on its line has no member line to leave with, so the renderer rewrites such an object whole
# — which refuses when a comment is inside it. That refusal is the right answer, so the two axes are never
# drawn together: a commented document keeps its commas at the line's end, and an own-line comma has none.
_LAYOUT = st.one_of(
    st.fixed_dictionaries({**_LAYOUT_FIELDS_INDENTED, "comma": st.just("end"), "comments": _COMMENT_LISTS}),
    st.fixed_dictionaries({**_LAYOUT_FIELDS_INDENTED, "comma": st.just("own-line"), "comments": st.just([])}),
)

# Where a document has a trailing comma, as the paths of the containers that end in one. The parser
# decides, not a regex over the text: it tells a string from a comma, and `_comma_after` steps over a
# comment, so `, // c\n}` counts as a trailing comma, and so does `[1,]` or a second one at another depth.
_Path = tuple[str | int, ...]


def _trailing_comma_paths(text: str) -> frozenset[_Path]:
    """The paths of every object or array whose last entry is followed by a comma."""
    parser = jsonc._Parser(text)
    root = parser.parse()
    comments = jsonc._Comments(text, tuple(parser.comments))

    def walk(node: jsonc._AstNode, path: _Path) -> frozenset[_Path]:
        if isinstance(node, jsonc._ObjectNode):
            children = [(member, (*path, member.key), member.value) for member in node.members]
        elif isinstance(node, jsonc._ArrayNode):
            children = [(element, (*path, index), element) for index, element in enumerate(node.elements)]
        else:
            return frozenset()
        found = frozenset().union(*(walk(child, child_path) for _, child_path, child in children))
        last = children[-1][0] if children else None
        if last is not None and jsonc._comma_after(comments, last) is not None:
            return found | {path}
        return found

    return walk(root, ())


def _mutated(value, how):
    """A tree a merge could hand the renderer, given the file it read."""
    if not isinstance(value, dict) or not value:
        return value if how == "independent" else {"d": "v"}
    items = list(value.items())
    if how == "reorder":
        return dict(reversed(items))
    if how == "add":
        return {**value, "d": "v"}
    if how == "drop":
        return dict(items[1:]) or {"d": "v"}
    if how == "change":
        return {**dict(items[:-1]), items[-1][0]: "changed"}
    if how == "nested":
        key = items[0][0]
        return {**value, key: _mutated(value[key], "drop") if isinstance(value[key], dict) else {"c": 1}}
    return {"d": "v"}


def _comment_in_first_empty_object(value, text: str, comment_text: str) -> str:
    """The document with a comment inside the first root member's `{}`, or unchanged when that is not one.

    Only the first of two or more members: the shape under test is an *added* key landing in the empty
    object, and a later member is the one a `change` edit rewrites whole (which would refuse, correctly).
    """
    first_key, first = next(iter(value.items()))
    # `_mutated` builds `add` and `independent` trees around a key `d`, so a commented `d: {}` would be
    # *overwritten* with a string, which rewrites the comment away and is refused (rightly). That is not the
    # shape under test, and the properties that draw this placement assume nothing here is refused.
    if first != {} or len(value) < 2 or first_key == "d":
        return text
    # A `//` comment runs to the end of its line, so the brace must be on the next one.
    body = "{ " + comment_text + ("\n}" if comment_text.startswith("//") else " }")
    return text.replace("{}", body, 1)


def _fuzz_template(value, *, indent, separators, newline, trailing, pad, comments, comma="end") -> str:
    """One settings file, laid out the way this combination describes.

    Comments go where a member edit cannot need to rewrite one: between the *root* members, found
    through the root's own indent so a nested object's separator is never mistaken for one of them.
    The text may be a `//` comment that *contains* `/*`, `*/`, or a quote — a path glob documented in
    a comment — which is syntax only where the parser says it is.
    """
    text = json.dumps(value, indent=indent, separators=separators, ensure_ascii=False)
    if pad and "\n" not in text and text[0] in "{[":
        # Only the document's own delimiters: padding every brace would also pad one that is part of
        # a *string*, and the template would no longer be the document this case means to fuzz.
        text = text[0] + " " + text[1:-1] + " " + text[-1]
    body = text.rstrip()
    if trailing and body.endswith(("}", "]")) and body.replace(" ", "").replace("\t", "") not in ("{}", "[]"):
        # A trailing comma after a *member*, at the end of the member's own line when the document is
        # member-per-line — a comma alone on the closing brace's line is an irregular layout. An empty
        # container is skipped: `[  ,]` is not JSONC.
        line_start = body.rfind("\n")
        cut = len(body) - 1 if line_start == -1 else line_start
        text = body[:cut] + "," + body[cut:] + text[len(body) :]
    unit = indent if isinstance(indent, str) else ""
    root_comma = ",\n" + unit + '"'
    for placement, comment_text in comments:
        if not (isinstance(value, dict) and value and "\n" in text) or placement == "none":
            continue
        head, found, _ = text.rpartition("\n}")
        text = {
            "before-first": text.replace("{", "{ " + comment_text, 1),
            "after-first": text.replace(root_comma, ", " + comment_text + "\n" + unit + '"', 1),
            "own-line": text.replace(root_comma, ",\n" + comment_text + "\n" + unit + '"', 1),
            "before-close": (head + "\n" + comment_text + found + text[len(head) + len(found) :]) if found else text,
            "in-empty-object": _comment_in_first_empty_object(value, text, comment_text),
        }[placement]
    if comma == "own-line" and unit:
        # After the comments, which find the root's `,\n<indent>"` separators; and only for an indented
        # document, because with no indent a nested member's separator looks like a root one.
        text = text.replace(root_comma, "\n" + unit + "," + "\n" + unit + '"')
    return text if newline == "\n" else text.replace("\n", newline)


@settings(max_examples=500, deadline=None)
@example(
    value={"d": {}, "a": None},
    how="independent",
    layout={
        "indent": "  ",
        "separators": (",", ":"),
        "newline": "\n",
        "trailing": False,
        "pad": False,
        "comma": "end",
        "comments": [("in-empty-object", "// hides **/*.map below")],
    },
)
@given(value=_FUZZ_VALUES, how=_MUTATIONS, layout=_LAYOUT)
def test_a_render_never_disagrees_with_the_tree_it_was_given(value, how, layout) -> None:
    # given — a file laid out however an editor might have written it, commented wherever a comment
    # can sit, and a tree a merge could hand back
    adapter = JsoncAdapter()
    template = _fuzz_template(value, **layout)
    tree = tree_from_value(_mutated(value, how))

    # given again — the template has to be the document this case means to fuzz, or a failure below
    # would be about the generator rather than the renderer
    assert adapter.parse(template) == tree_from_value(value)

    # when — nothing here may refuse. The comments sit between root members, where a member edit
    # never needs to rewrite one, so a refusal is a bug and not a verdict — and a tolerated one would
    # hide the very failures this property exists to catch.
    rendered = adapter.render(tree, template)

    # then — the render must be the tree it was handed, or the merge publishes what no one decided,
    # and it must not invent layout the file did not have: a trailing comma may stay where the
    # template had one, but never appear anywhere else — the one that broke Docker's `config.json`
    assert adapter.parse(rendered) == tree
    assert _trailing_comma_paths(rendered) <= _trailing_comma_paths(template)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param('{"o": {"a": 1,},}', frozenset({(), ("o",)}), id="root-and-nested"),
        pytest.param("[1,]", frozenset({()}), id="array"),
        pytest.param('[{"a": 1,}, 2]', frozenset({(0,)}), id="nested-in-array"),
        pytest.param('{"a": 1 /* n */,}', frozenset({()}), id="comment-then-comma"),
        pytest.param('{"a": 1, // c\n}', frozenset({()}), id="comma-then-comment"),
        pytest.param('{"a": 1 // c\n}', frozenset(), id="comment-without-comma"),
        pytest.param('{"a": ", }"}', frozenset(), id="comma-inside-string"),
        pytest.param("{}", frozenset(), id="empty-object"),
        pytest.param("[]", frozenset(), id="empty-array"),
    ],
)
def test_trailing_comma_paths_names_every_container_ending_in_a_comma(text, expected) -> None:
    # when
    found = _trailing_comma_paths(text)

    # then
    assert found == expected


@pytest.mark.parametrize(
    ("text", "want", "expected"),
    [
        pytest.param(
            "{ /* note\n   more */ }\n",
            {"x": 1},
            '{ /* note\n   more */\n  "x": 1\n}\n',
            id="multi-line-block-comment-shares-the-brace-line",
        ),
        pytest.param(
            "{\n  /* a\n  b */ }\n",
            {"x": 1},
            '{\n  /* a\n  b */\n  "x": 1\n}\n',
            id="block-comment-on-its-own-lines-then-brace",
        ),
        pytest.param(
            '{\n  "o": { /* a\n  b */ }\n}\n',
            {"o": {"x": 1}},
            '{\n  "o": { /* a\n  b */\n    "x": 1\n  }\n}\n',
            id="nested-object-closes-at-the-indent-it-opened-at",
        ),
        pytest.param(
            "{ /* a\r\n b */ }\r\n",
            {"x": 1},
            '{ /* a\r\n b */\r\n  "x": 1\r\n}\r\n',
            id="crlf-document-keeps-its-newline",
        ),
        pytest.param(
            "{ /* note */ }\n",
            {"x": 1},
            '{ /* note */ "x": 1 }\n',
            id="one-line-block-comment-takes-the-member-after-it",
        ),
        pytest.param(
            "{ /* note */ }\n",
            {"x": 1, "y": 2},
            '{ /* note */ "x": 1, "y": 2 }\n',
            id="one-line-block-comment-takes-several-members-separated",
        ),
        pytest.param(
            '{\n  "a": 1\n  ,\n  "b": 2\n}\n',
            {"b": 2},
            '{\n  "b": 2\n}\n',
            id="own-line-comma-member-removed",
        ),
        pytest.param(
            '{\n  "a": 1\n  ,\n  "b": 2\n}\n',
            {"a": 1, "x": 0, "b": 2},
            '{\n  "a": 1,\n  "x": 0,\n  "b": 2\n}\n',
            id="own-line-comma-member-added",
        ),
        pytest.param('{\n  "a": 1\n  ,\n}\n', {}, "{}\n", id="own-line-comma-every-member-removed"),
        pytest.param(
            '{\n  "a": 1\n  ,\n  "b": 2\n}\n',
            {"a": 1},
            '{\n  "a": 1\n}\n',
            id="own-line-comma-leaves-no-whitespace-only-line",
        ),
        pytest.param(
            '{\n  "a": 1\n  , "b": 2\n}\n',
            {"a": 1, "b": 2, "c": 3},
            '{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n',
            id="leading-comma-is-still-rewritten-whole",
        ),
        pytest.param(
            "{\n  // Place your settings in this file\n}\n",
            {"x": 1},
            '{\n  // Place your settings in this file\n  "x": 1\n}\n',
            id="line-comment-above-the-brace-is-unchanged",
        ),
        pytest.param(
            "{\n  /* a\n     b */\n}\n",
            {"x": 1},
            '{\n  /* a\n     b */\n  "x": 1\n}\n',
            id="block-comment-then-brace-on-its-own-line-is-unchanged",
        ),
        pytest.param(
            "{ // note\n}\n", {"x": 1}, '{ // note\n  "x": 1\n}\n', id="line-comment-after-the-brace-is-unchanged"
        ),
    ],
)
def test_a_member_edit_lands_outside_a_comment_and_beside_a_lone_comma(text, want, expected) -> None:
    # given
    adapter = JsoncAdapter()
    tree = tree_from_value(want)

    # when
    rendered = adapter.render(tree, text)

    # then — the layout is the one the file had, and the result is the document the tree asked for
    assert rendered == expected
    assert adapter.parse(rendered) == tree


_LONE_COMMA_NOTED = (
    '{\n  // my settings\n  "editor.fontSize": 13\n  ,\n  "editor.tabSize": 2,\n'
    '  "files.exclude": {\n    "**/.git": true\n  },\n  "z": 1\n}\n'
)
_LONE_COMMA_TREE = {
    "editor.fontSize": 13,
    "editor.tabSize": 2,
    "files.exclude": {"**/.git": True},
    "z": 1,
}


@pytest.mark.parametrize(
    "want",
    [
        pytest.param(
            {k: v for k, v in _LONE_COMMA_TREE.items() if k != "z"}, id="delete-a-member-with-its-comma-at-line-end"
        ),
        pytest.param({**_LONE_COMMA_TREE, "n": 1}, id="add-a-member-at-the-end"),
        pytest.param(
            {**_LONE_COMMA_TREE, "files.exclude": {"**/.git": True, "**/.DS_Store": True}},
            id="add-a-key-in-a-nested-object",
        ),
        pytest.param({**_LONE_COMMA_TREE, "editor.fontSize": 14}, id="change-the-value-that-owns-the-lone-comma"),
    ],
)
def test_an_edit_away_from_a_lone_comma_keeps_the_layout_and_the_note(want) -> None:
    # given — a note in the object and a comma alone on one line, but the edit never works on that comma
    adapter = JsoncAdapter()
    tree = tree_from_value(want)

    # when
    rendered = adapter.render(tree, _LONE_COMMA_NOTED)

    # then — the object is edited in place, so the note and the user's comma layout are both still there
    assert adapter.parse(rendered) == tree
    assert "  // my settings\n" in rendered
    assert '"editor.fontSize": 1' in rendered
    assert "\n  ,\n" in rendered


@pytest.mark.parametrize(
    "want",
    [
        pytest.param(
            {k: v for k, v in _LONE_COMMA_TREE.items() if k != "editor.fontSize"},
            id="delete-the-member-that-owns-the-comma",
        ),
        pytest.param(
            {"editor.fontSize": 13, "n": 1, **{k: v for k, v in _LONE_COMMA_TREE.items() if k != "editor.fontSize"}},
            id="insert-after-the-member-that-owns-the-comma",
        ),
    ],
)
def test_an_edit_that_works_on_a_lone_comma_beside_a_note_is_refused_with_its_own_reason(want) -> None:
    # given — the edit would strand the lone comma, and rewriting the object whole would lose the note
    adapter = JsoncAdapter()

    # when & then — the reason names the comma, not "this value has a comment"
    with pytest.raises(RenderRefused) as refused:
        adapter.render(tree_from_value(want), _LONE_COMMA_NOTED)
    assert refused.value.reason == LONE_COMMA_REFUSED


def test_a_member_edit_beside_a_lone_comma_and_a_note_is_refused() -> None:
    # given — the comma sits alone on its line, so the object cannot be spliced by line, and the
    # comment above `a` would be lost by rewriting the object whole
    adapter = JsoncAdapter()
    text = '{\n  // keep\n  "a": 1\n  ,\n  "b": 2\n}\n'

    # when & then — an honest refusal rather than a file that does not parse
    with pytest.raises(ContentParseError):
        adapter.render(tree_from_value({"b": 2}), text)


_NESTED_VALUES = st.dictionaries(
    _FUZZ_KEYS, st.none() | st.booleans() | st.integers() | _FUZZ_TEXT, min_size=1, max_size=3
)


@settings(max_examples=300, deadline=None)
@given(
    others=st.dictionaries(_FUZZ_KEYS, st.none() | st.booleans() | st.integers() | _FUZZ_TEXT, min_size=1, max_size=2),
    nested=_NESTED_VALUES,
    how=st.sampled_from(["drop", "add", "change"]),
    comments=_COMMENT_LISTS,
    layout=_LAYOUT_INDENTED,
)
def test_an_edit_inside_the_last_member_leaves_lone_commas_and_notes_before_it_byte_for_byte(
    others, nested, how, comments, layout
) -> None:
    # given — every root comma alone on its line, notes in the document, and an edit that only reaches into
    # the last member's own object; the document stays as written above that member
    adapter = JsoncAdapter()
    value = {**{key: val for key, val in others.items() if key != "n"}, "n": nested}
    template = _fuzz_template(value, comments=comments, comma="own-line", **layout)
    edited = {
        "drop": dict(list(nested.items())[1:]) or {"d": "v"},
        "add": {**nested, "d": "v"},
        "change": {**dict(list(nested.items())[:-1]), list(nested)[-1]: "changed"},
    }[how]
    tree = tree_from_value({**value, "n": edited})
    assert adapter.parse(template) == tree_from_value(value)

    # when
    rendered = adapter.render(tree, template)

    # then
    head = template[: template.index('"n"')]
    assert rendered.startswith(head)
    assert adapter.parse(rendered) == tree


def _render_or_refusal(adapter, tree, template) -> tuple[str | None, str | None]:
    """`(render, None)`, or `(None, reason)` when the render was refused; any other failure propagates."""
    try:
        return adapter.render(tree, template), None
    except RenderRefused as refused:
        return None, refused.reason


@settings(max_examples=500, deadline=None)
@example(
    value={"d": {}, "a": None},
    how="add",
    comments=[("in-empty-object", "// hides **/*.map below")],
    layout={"indent": "  ", "separators": (",", ":"), "newline": "\n", "trailing": False, "pad": False},
)
@given(
    value=_FUZZ_VALUES,
    how=_MUTATIONS,
    comments=_COMMENT_LISTS,
    layout=_LAYOUT_INDENTED,
)
def test_a_root_edit_beside_lone_commas_and_notes_is_exact_or_refused_for_the_comma(
    value, how, comments, layout
) -> None:
    # given — every root comma alone on its line, notes in the document, and any edit of the root: this
    # pins which edits `_lone_comma_touched` sends to a whole rewrite, since that decision restates the
    # ones `_object` and `_remove_deleted` make
    adapter = JsoncAdapter()
    template = _fuzz_template(value, comments=comments, comma="own-line", **layout)
    tree = tree_from_value(_mutated(value, how))
    assert adapter.parse(template) == tree_from_value(value)

    # when
    rendered, reason = _render_or_refusal(adapter, tree, template)

    # then — a refusal names the comma and never the general reason; anything else is the tree it was handed
    assert reason in (None, LONE_COMMA_REFUSED)
    assert rendered is None or adapter.parse(rendered) == tree


def test_the_last_comment_end_before_a_position_with_no_comment_is_refused() -> None:
    # given — a comment that starts after the position
    comments = jsonc._Comments("{} /* a */", (jsonc._Span(3, 10),))

    # when & then — index -1 must not hand back the file's last comment
    with pytest.raises(ValueError, match="no comment ends"):
        comments.last_end_before(2)


@settings(max_examples=200, deadline=None)
@given(
    others=st.dictionaries(_FUZZ_KEYS, st.none() | st.booleans() | st.integers() | _FUZZ_TEXT, max_size=2),
    comment=_COMMENT_TEXTS,
    layout=_LAYOUT_INDENTED,
)
def test_a_key_added_to_a_commented_empty_object_lands_after_the_comment(others, comment, layout) -> None:
    # given — the first member is an empty object that holds a comment; the document is laid out every which way
    adapter = JsoncAdapter()
    value = {"e": {}, **{key: val for key, val in others.items() if key != "e"}, "z": 1}
    template = _fuzz_template(value, comments=[("in-empty-object", comment)], comma="end", **layout)
    tree = tree_from_value({**value, "e": {"x": 1}})
    assert adapter.parse(template) == tree_from_value(value)
    note = comment.replace("\n", layout["newline"])
    assert note in template

    # when
    rendered = adapter.render(tree, template)

    # then — the key is a member, not part of the comment, and the comment is still there
    assert adapter.parse(rendered) == tree
    assert note in rendered


def test_a_member_edit_in_a_one_line_object_that_has_members_and_a_note_is_still_refused() -> None:
    # given — splicing around a comment between members would move it, so this layout is not spliced
    adapter = JsoncAdapter()
    text = '{ "a": 1, /* note */ "b": 2 }\n'

    # when & then
    with pytest.raises(ContentParseError):
        adapter.render(tree_from_value({"a": 1, "b": 2, "c": 3}), text)


def test_a_block_comment_that_closes_below_a_member_keeps_the_next_member_out_of_it() -> None:
    # given — a block comment opens on `a`'s line and closes on the line below
    adapter = JsoncAdapter()
    text = '{\n  "a": 1, /* note\n  still note */\n  "b": 2\n}\n'
    tree = tree_from_value({"a": 1, "c": 3, "b": 2})

    # when — the next line after `a` is still inside that comment
    rendered = adapter.render(tree, text)

    # then — the new member goes after the comment, where it parses as a member
    assert rendered == '{\n  "a": 1, /* note\n  still note */\n  "c": 3,\n  "b": 2\n}\n'
    assert adapter.parse(rendered) == tree


def test_deleting_a_member_below_a_block_comment_does_not_leave_its_tail_behind() -> None:
    # given — a block comment opens on the deleted member's line and closes on the next
    adapter = JsoncAdapter()
    text = '{\n  "a": 1, /* note\n  still note */\n  "b": 2\n}\n'
    tree = tree_from_value({"b": 2})

    # when — the deletion takes the member's line, and the comment straddles it
    rendered = adapter.render(tree, text)

    # then — the comment goes with the member instead of leaving `still note */` as loose text
    assert rendered == '{\n  "b": 2\n}\n'
    assert adapter.parse(rendered) == tree


def test_a_glob_that_looks_like_a_comment_is_not_one() -> None:
    # given — `**/*.pyc` and `**/.git` are two members whose text, read as raw bytes, spells a block
    # comment opening and closing around them
    adapter = JsoncAdapter()
    text = '{\n  "files.exclude": {\n    "**/*.pyc": true,\n    "**/.git": true\n  },\n  "editor.fontSize": 13\n}\n'
    tree = tree_from_value({"files.exclude": {"**/.git": True}, "editor.fontSize": 13})

    # when — the glob is deleted
    rendered = adapter.render(tree, text)

    # then — only that member goes; the sibling that happens to spell the comment's end stays
    assert rendered == '{\n  "files.exclude": {\n    "**/.git": true\n  },\n  "editor.fontSize": 13\n}\n'
    assert adapter.parse(rendered) == tree


def test_a_member_added_between_reordered_members_gets_its_comma() -> None:
    # given — the file lists `b` before `a`, and the tree wants `a`, `b`, and a new `c`
    adapter = JsoncAdapter()
    text = '{\n  "b": 2,\n  "a": 1\n}\n'
    tree = tree_from_value({"a": 1, "b": 2, "c": 3})

    # when — the block lands after `b` in the file, which is not the file's last member
    rendered = adapter.render(tree, text)

    # then — it carries the comma that separates it from `a`
    assert rendered == '{\n  "b": 2,\n  "c": 3,\n  "a": 1\n}\n'
    assert adapter.parse(rendered) == tree


@settings(max_examples=400, deadline=None)
@example(
    value={"d": {}, "a": None},
    how="add",
    comments=[("in-empty-object", "// hides **/*.map below")],
    layout={"indent": "  ", "separators": (",", ":"), "newline": "\n", "trailing": False, "pad": False},
)
@given(
    value=_FUZZ_VALUES,
    how=st.sampled_from(["drop", "add"]),
    comments=_COMMENT_LISTS_AT_LEAST_ONE,
    layout=_LAYOUT_INDENTED,
)
def test_a_commented_document_renders_a_member_edit_cleanly(value, how, comments, layout) -> None:
    # given — a commented document and the edit the projection actually makes: remove a noise key, or
    # add an excluded one. The comments' *text* holds `/*`, `*/`, and quotes (a documented glob), so
    # this is where a renderer that re-finds syntax by hand goes wrong.
    adapter = JsoncAdapter()
    template = _fuzz_template(value, comments=comments, **layout)
    tree = tree_from_value(_mutated(value, how))

    # given again — the template has to be the document this case means to fuzz, or a refusal below
    # would be about the generator rather than the renderer
    assert adapter.parse(template) == tree_from_value(value)

    # when
    rendered = adapter.render(tree, template)

    # then — a trailing comma may stay where the template had one, but never appear anywhere else
    assert adapter.parse(rendered) == tree
    assert _trailing_comma_paths(rendered) <= _trailing_comma_paths(template)


def test_deleting_the_last_member_does_not_invent_a_trailing_comma() -> None:
    # given — Docker's `config.json` shape: two members, no trailing comma, and a strict JSON reader
    adapter = JsoncAdapter()
    text = '{\n  "psFormat": "b",\n  "imagesFormat": "table"\n}\n'
    tree = tree_from_value({"psFormat": "b", "imagesFormat": "table", "detachKeys": "ctrl-e,e"})

    # when — a member is added after the first, and the second is deleted
    rendered = adapter.render(tree_from_value({"psFormat": "b", "detachKeys": "ctrl-e,e"}), text)

    # then — no comma is invented where the file had none
    assert rendered == '{\n  "psFormat": "b",\n  "detachKeys": "ctrl-e,e"\n}\n'
    assert adapter.parse(rendered) == tree_from_value({"psFormat": "b", "detachKeys": "ctrl-e,e"})
    assert tree != tree_from_value({"psFormat": "b", "detachKeys": "ctrl-e,e"})


def test_a_carriage_return_inside_a_block_comment_does_not_make_the_file_crlf() -> None:
    # given — an LF file whose *block* comment holds a Windows line ending, which is inside the
    # comment's text because a block comment spans lines
    adapter = JsoncAdapter()
    text = '{\n  /* pasted\nfrom Windows\r\n*/\n  "a": 1,\n  "b": 2\n}\n'
    tree = tree_from_value({"a": 1, "b": 2, "c": 3})

    # when
    rendered = adapter.render(tree, text)

    # then — the comment is not the file's own line ending, so the inserted line is LF like the lines
    # around it instead of splitting the file between two styles
    assert rendered == '{\n  /* pasted\nfrom Windows\r\n*/\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n'
    assert adapter.parse(rendered) == tree


def test_a_crlf_file_whose_lines_end_in_comments_is_still_crlf() -> None:
    # given — every line ends in a `//` comment, so the `\r` of each CRLF pair sits right before the
    # `\n` the parser stops the comment at
    adapter = JsoncAdapter()
    text = '{\r\n  "a": 1, // note\r\n  "b": 2 // note\r\n}\r\n'
    tree = tree_from_value({"a": 1, "b": 2, "c": 3})

    # when
    rendered = adapter.render(tree, text)

    # then — the `\r` belongs to the line ending, not the comment, so the file is still CRLF and the
    # inserted line matches the lines around it
    assert rendered == '{\r\n  "a": 1, // note\r\n  "b": 2, // note\r\n  "c": 3\r\n}\r\n'
    assert adapter.parse(rendered) == tree
