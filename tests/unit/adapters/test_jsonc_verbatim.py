# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for copying a value from another JSONC document as written (``Verbatim``).

A carried key must land with exactly the bytes of the object it was carried from, or the restore
round-trip never agrees with an earlier render (``docs/design/02-base-and-merge.md``).
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.ports import Verbatim

ADAPTER = JsoncAdapter()
K = frozenset({("k",)})


def _render(source: str, template: str, paths: frozenset = K) -> str:
    return ADAPTER.render(ADAPTER.parse(source), template, verbatim=Verbatim(source, paths))


@pytest.mark.parametrize(
    ("literal", "reserialized"),
    [("1e3", "1000.0"), ("1.50", "1.5"), ("10E2", "1000.0"), ('"\\u00e9"', '"é"')],
)
def test_a_scalar_keeps_the_literal_the_source_wrote(literal, reserialized) -> None:
    # given — the template holds an equal value written the way `json.dumps` would
    source = f'{{"a": 1, "k": {literal}}}\n'
    template = f'{{"a": 1, "k": {reserialized}}}\n'

    # when
    rendered = _render(source, template)

    # then
    assert rendered == source


def test_a_commented_block_is_copied_with_its_comments() -> None:
    # given
    source = (
        "{\n"
        '  "a": 1,\n'
        '  "[python]": {\n'
        "    // formatter for this machine\n"
        '    "editor.formatOnSave": true, /* keep */\n'
        '    "editor.tabSize": 4,\n'
        "  },\n"
        '  "z": 2\n'
        "}\n"
    )
    template = '{\n  "a": 1,\n  "[python]": {\n    "editor.tabSize": 2\n  },\n  "z": 2\n}\n'

    # when
    rendered = _render(source, template, frozenset({("[python]",)}))

    # then
    assert rendered == source


def test_the_templates_own_comments_outside_the_copied_value_stay() -> None:
    # given
    source = '{"a": 1, "k": 1e3}\n'
    template = '{\n  // mine\n  "a": 1,\n  "k": 5 // old\n}\n'

    # when
    rendered = _render(source, template)

    # then
    assert rendered == '{\n  // mine\n  "a": 1,\n  "k": 1e3 // old\n}\n'


def test_a_trailing_comma_inside_the_copied_value_is_kept() -> None:
    # given
    source = '{"k": [1, 2,], "a": 1}\n'
    template = '{"k": [9], "a": 1}\n'

    # when & then
    assert _render(source, template) == source


def test_a_one_line_object_takes_the_copy_in_place() -> None:
    # given
    source = '{"a": 1, "k": {"x": 1e2}}'
    template = '{"a": 1, "k": {"x": 7}}'

    # when & then
    assert _render(source, template) == source


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ('{\n  "a": 1\n}\n', '{\n  "a": 1,\n  "k": 1e3\n}\n'),
        ('{"a": 1}\n', '{"a": 1, "k": 1e3}\n'),
        ("{}\n", '{"a": 1, "k": 1e3}\n'),
    ],
)
def test_a_key_the_template_lacks_is_inserted_with_the_sources_text(template, expected) -> None:
    # given
    source = '{"a": 1, "k": 1e3}\n'

    # when
    rendered = _render(source, template)

    # then
    assert rendered == expected


def test_a_multiline_value_is_inserted_as_written() -> None:
    # given
    source = '{\n  "k": {\n    "x": 1e3\n  }\n}\n'

    # when
    rendered = _render(source, "{\n}\n")

    # then
    assert rendered == source


def test_a_nested_path_is_copied() -> None:
    # given
    source = '{"a": {"k": 1e3, "o": 1}}\n'
    template = '{"a": {"k": 1000.0, "o": 1}}\n'

    # when & then
    assert _render(source, template, frozenset({("a", "k")})) == source


def test_a_value_the_tree_changed_is_not_copied_from_the_source() -> None:
    # given — the source holds 1e3 but the tree asks for 5: the ordinary render wins
    source = '{"k": 1e3}\n'
    template = '{"k": 1}\n'

    # when
    rendered = ADAPTER.render(ADAPTER.parse('{"k": 5}'), template, verbatim=Verbatim(source, K))

    # then
    assert rendered == '{"k": 5}\n'


def test_a_path_the_source_lacks_renders_the_ordinary_way() -> None:
    # given
    source = '{"other": 1}\n'
    template = '{"k": 1}\n'

    # when
    rendered = ADAPTER.render(ADAPTER.parse('{"k": 2}'), template, verbatim=Verbatim(source, K))

    # then
    assert rendered == '{"k": 2}\n'


def test_a_template_that_already_holds_the_copy_is_returned_untouched() -> None:
    # given
    text = '{"a": 1,   "k": 1e3}   \n'

    # when & then
    assert _render(text, text) is text


def test_without_verbatim_an_equal_value_keeps_the_templates_text() -> None:
    # given — the unchanged path, so the new argument costs existing callers nothing
    template = '{"k": 1000.0}\n'

    # when & then
    assert ADAPTER.render(ADAPTER.parse('{"k": 1e3}'), template) == template


@given(
    literal=st.sampled_from(["1e3", "0.10", "-0", "1E+2", '"a\\/b"', "[1,2,]", "{ }", "{\n   }"]),
    indent=st.sampled_from(["", "\n  "]),
)
def test_rendering_with_the_same_source_twice_changes_nothing(literal, indent) -> None:
    # given
    source = f'{{{indent}"a": 1,{indent}"k": {literal}{indent and chr(10)}}}'
    template = f'{{{indent}"a": 1,{indent}"k": "old"{indent and chr(10)}}}'
    tree = ADAPTER.parse(source)
    verbatim = Verbatim(source, K)

    # when
    once = ADAPTER.render(tree, template, verbatim=verbatim)
    twice = ADAPTER.render(tree, once, verbatim=verbatim)

    # then
    assert (once, ADAPTER.parse(once)) == (twice, tree)
