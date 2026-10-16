# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the merge use-case."""

from __future__ import annotations

from syncintosh.core.tree import tree_from_value
from syncintosh.services import merge_documents


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
