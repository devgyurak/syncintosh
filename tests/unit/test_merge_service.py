# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the merge use-cases."""

from __future__ import annotations

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ContentParseError
from syncintosh.core.tree import tree_from_value
from syncintosh.services import merge_documents, merge_text


def test_merge_documents_delegates_to_the_engine() -> None:
    # when
    result = merge_documents(tree_from_value({"a": "1"}), tree_from_value({"a": "2"}), tree_from_value({"a": "1"}))

    # then
    assert result.merged == tree_from_value({"a": "2"})
    assert result.has_conflicts is False


def test_merge_documents_reports_conflicts_without_raising() -> None:
    # when
    result = merge_documents(tree_from_value({"a": "1"}), tree_from_value({"a": "2"}), tree_from_value({"a": "3"}))

    # then
    assert [conflict.path for conflict in result.conflicts] == [("a",)]


def test_merge_documents_passes_an_ignore_set_through() -> None:
    # when
    result = merge_documents(
        tree_from_value({"a": 1}),
        tree_from_value({"a": 2}),
        tree_from_value({"a": 3}),
        ignore=frozenset({("a",)}),
    )

    # then
    assert result.merged == tree_from_value({"a": 2})
    assert result.conflicts == ()


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
