# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the merge use-case."""

from __future__ import annotations

import json

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ContentParseError
from syncintosh.core.tree import Tree, tree_from_value
from syncintosh.services import merge_text


def test_merge_text_renders_against_the_local_template() -> None:
    # given — only the local side changed, so the comment survives the render
    base = '{\n  "a": 1,\n  "b": 2\n}\n'
    local = '{\n  "a": 1, // keep\n  "b": 3\n}\n'

    # when
    result = merge_text(base, local, base, JsoncAdapter())

    # then
    assert result.has_conflicts is False
    assert result.text == local


def test_merge_text_reports_the_conflicting_key_paths() -> None:
    # given
    base = '{\n  "a": 1\n}\n'

    # when
    result = merge_text(base, '{\n  "a": 2\n}\n', '{\n  "a": 3\n}\n', JsoncAdapter())

    # then
    assert [conflict.path for conflict in result.conflicts] == [("a",)]
    assert result.has_conflicts is True


def test_merge_text_raises_on_a_side_that_does_not_parse() -> None:
    # when & then — a malformed side is a usage error, not a conflict
    with pytest.raises(ContentParseError):
        merge_text("{", "{}", "{}", JsoncAdapter())


def test_merge_text_is_conflicted_when_the_render_does_not_round_trip() -> None:
    # given — an adapter whose render drops what was merged
    class BrokenRender:
        def parse(self, text: str) -> Tree:
            return tree_from_value(json.loads(text))

        def render(self, tree: Tree, template: str, arrays=None) -> str:
            return "{}"

    # when
    result = merge_text('{"a": 1}', '{"a": 1}', '{"a": 1}', BrokenRender())

    # then — never a silent success with text that is not the merge
    assert result.has_conflicts is True
    assert result.conflicts == ()
