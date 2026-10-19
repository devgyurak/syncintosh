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
from hypothesis import given, settings
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import ContentParseError
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


def test_a_block_comment_whose_last_line_starts_with_slashes_stops_the_walk() -> None:
    # given — a block comment whose closing line starts with `//`, and a tree that adds a member
    # before the commented member
    adapter = JsoncAdapter()
    text = '{\n  /* note\n  // still inside */\n  "a": 1\n}\n'
    tree = tree_from_value({"b": 2, "a": 1})

    # when
    rendered = adapter.render(tree, text)

    # then — the new member goes after the block comment, not inside it (where it would parse as
    # part of the comment and the render would not match the tree)
    assert rendered == '{\n  /* note\n  // still inside */\n  "b": 2,\n  "a": 1\n}\n'
    assert adapter.parse(rendered) == tree


# The renderer's real contract, fuzzed: a settings file as an editor might have written it, against
# a tree a merge could produce from it. Every layout shape that has bitten this renderer is a knob
# here — one line or member-per-line, tight or spaced, padded braces, a trailing comma, CRLF, and a
# comment in each position it can sit in — and the tree is either derived from the file (a key
# reordered, added, dropped, or changed) or an unrelated document.
# Keys come from a tiny pool on purpose: a merge hands the renderer a tree that shares most of its
# keys with the file, so the interesting shapes are reorderings and one-key edits, not two unrelated
# documents. An unrelated key set exercises the "replace everything" path and little else.
_FUZZ_KEYS = st.sampled_from(["a", "b", "c", "d"])

_FUZZ_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=3),
    lambda children: st.lists(children, max_size=2) | st.dictionaries(_FUZZ_KEYS, children, max_size=3),
)

_MUTATIONS = st.sampled_from(["reorder", "add", "drop", "change", "nested", "independent"])

_COMMENTS = st.sampled_from(
    [
        "none",
        "before-first",
        "after-first",
        "own-line",
        "before-close",
        "block-after-first",  # a block comment opening on a member's line and closing below it
        "block-before-close",
    ]
)

_LAYOUT = st.fixed_dictionaries(
    {
        "indent": st.sampled_from([None, "", "  ", "\t", " "]),
        "separators": st.sampled_from([(",", ":"), (", ", ": ")]),
        "newline": st.sampled_from(["\n", "\r\n"]),
        "trailing": st.booleans(),
        "pad": st.booleans(),
        "comment": _COMMENTS,
    }
)


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


def _fuzz_template(value, *, indent, separators, newline, trailing, pad, comment) -> str:
    """One settings file, laid out the way this combination describes."""
    text = json.dumps(value, indent=indent, separators=separators, ensure_ascii=False)
    if pad and "\n" not in text and text[0] in "{[":
        # Only the document's own delimiters: padding every brace would also pad one that is part of
        # a *string*, and the template would no longer be the document this case means to fuzz.
        text = text[0] + " " + text[1:-1] + " " + text[-1]
    body = text.rstrip()
    if trailing and body.endswith(("}", "]")) and body.replace(" ", "").replace("\t", "") not in ("{}", "[]"):
        # A trailing comma after a *member*; `[  ,]` is not JSONC, so an empty container is skipped.
        text = body[:-1] + "," + body[-1] + text[len(body) :]
    multiline_object = isinstance(value, dict) and value and "\n" in text
    if multiline_object:
        text = {
            "none": text,
            "before-first": text.replace("{", "{ // fuzz", 1),
            "after-first": text.replace(",\n", ", // fuzz\n", 1) if ",\n" in text else text,
            "own-line": text.replace(",\n", ",\n// fuzz\n", 1) if ",\n" in text else text,
            "before-close": text.replace("\n}", "\n// fuzz\n}", 1),
            "block-after-first": text.replace(",\n", ", /* fuzz\nstill */\n", 1) if ",\n" in text else text,
            "block-before-close": text.replace("\n}", "\n/* fuzz\nstill */\n}", 1),
        }[comment]
    return text if newline == "\n" else text.replace("\n", newline)


# `deadline=None`: a single example parses and re-parses a whole document, and a per-example time
# limit here measures the interpreter's warm-up rather than a bug (AGENTS.md §5.6 — no clock).
@settings(max_examples=500, deadline=None)
@given(value=_FUZZ_VALUES, how=_MUTATIONS, layout=_LAYOUT)
def test_a_render_never_disagrees_with_the_tree_it_was_given(value, how, layout) -> None:
    # given — a file laid out however an editor might have written it, and a tree derived from it
    adapter = JsoncAdapter()
    template = _fuzz_template(value, **layout)
    tree = tree_from_value(_mutated(value, how))

    # when
    try:
        rendered = adapter.render(tree, template)
    except ContentParseError:
        # A rewrite that would drop a comment is the only legitimate refusal, so the template has to
        # be one that holds a comment; any other refusal is a bug this test exists to catch.
        assert "//" in template or "/*" in template
        return

    # then — the render must be the tree it was handed, or the merge publishes what no one decided
    assert adapter.parse(rendered) == tree


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
