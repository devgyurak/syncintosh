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
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ContentParseError
from syncintosh.core.merge import merge_tree
from syncintosh.core.tree import Scalar, Tree

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
        "",  # empty file
        "   \n\t ",  # whitespace only
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
