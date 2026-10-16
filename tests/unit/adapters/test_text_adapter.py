# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the line-level `text` path."""

from __future__ import annotations

from syncintosh.adapters.text import TextAdapter
from syncintosh.core.models import TextMerge


def test_text_adapter_merges_non_overlapping_lines() -> None:
    # when
    result = TextAdapter().merge("one\ntwo\nthree\n", "ONE\ntwo\nthree\n", "one\ntwo\nTHREE\n")

    # then
    assert result == TextMerge(text="ONE\ntwo\nTHREE\n", conflicted=False)


def test_text_adapter_reports_a_conflict_it_cannot_reconcile() -> None:
    # when
    result = TextAdapter().merge("one\ntwo\n", "one\nA\n", "one\nB\n")

    # then
    assert result.conflicted is True


def test_text_adapter_delegates_to_the_injected_driver() -> None:
    # given
    class Recording:
        def merge(self, base: str, local: str, remote: str) -> TextMerge:
            return TextMerge(text=f"{base}|{local}|{remote}", conflicted=False)

    # when
    result = TextAdapter(Recording()).merge("a", "b", "c")

    # then
    assert result.text == "a|b|c"
