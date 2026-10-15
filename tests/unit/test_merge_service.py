# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the merge use-case."""

from __future__ import annotations

from syncintosh.services import merge_documents


def test_merge_documents_delegates_to_the_engine() -> None:
    # when
    result = merge_documents({"a": "1"}, {"a": "2"}, {"a": "1"})

    # then
    assert result.merged == {"a": "2"}
    assert result.has_conflicts is False


def test_merge_documents_reports_conflicts_without_raising() -> None:
    # when
    result = merge_documents({"a": "1"}, {"a": "2"}, {"a": "3"})

    # then
    assert [conflict.key for conflict in result.conflicts] == ["a"]
