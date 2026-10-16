# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and property tests for the line-level text merge driver.

The properties (``AGENTS.md`` §5.3) pin the two rules the design leans on: merging one side
against itself is that side, and a one-sided change always wins. A conflict is reported by the
driver's own verdict, so a file that legitimately contains a marker line never reads as
conflicted.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from syncintosh.adapters.text_merge import LOCAL_LABEL, REMOTE_LABEL, TextMergeDriver
from syncintosh.core.models import TextMerge


def _merge(base: str, local: str, remote: str) -> TextMerge:
    return TextMergeDriver().merge(base, local, remote)


def test_non_overlapping_changes_merge_cleanly() -> None:
    # given — each side changed a different line
    base = "one\ntwo\nthree\n"
    local = "ONE\ntwo\nthree\n"
    remote = "one\ntwo\nTHREE\n"

    # when
    result = _merge(base, local, remote)

    # then
    assert result == TextMerge(text="ONE\ntwo\nTHREE\n", conflicted=False)


def test_the_same_line_changed_on_both_sides_is_a_conflict() -> None:
    # given
    base = "one\ntwo\nthree\n"

    # when
    result = _merge(base, "one\nLOCAL\nthree\n", "one\nREMOTE\nthree\n")

    # then
    assert result.conflicted is True
    assert f"<<<<<<< {LOCAL_LABEL}\nLOCAL\n=======\nREMOTE\n>>>>>>> {REMOTE_LABEL}\n" in result.text


def test_a_marker_line_in_real_content_is_not_a_conflict() -> None:
    # given — the content itself contains a marker-shaped line, changed on one side only
    base = "keep\n"
    local = "keep\n<<<<<<< local\n"

    # when
    result = _merge(base, local, base)

    # then — one-sided, so it is applied, not reported as a conflict
    assert result == TextMerge(text=local, conflicted=False)


def test_a_missing_trailing_newline_is_preserved() -> None:
    # given
    base = "one\ntwo"
    local = "one\nTWO"
    remote = "one\ntwo"

    # when
    result = _merge(base, local, remote)

    # then
    assert result == TextMerge(text="one\nTWO", conflicted=False)


@given(st.text())
def test_merging_identical_sides_is_the_identity(text: str) -> None:
    # when
    result = _merge(text, text, text)

    # then
    assert result == TextMerge(text=text, conflicted=False)


@given(st.text(), st.text())
def test_a_local_only_change_wins(base: str, local: str) -> None:
    # when
    result = _merge(base, local, base)

    # then
    assert result == TextMerge(text=local, conflicted=False)


@given(st.text(), st.text())
def test_a_remote_only_change_wins(base: str, remote: str) -> None:
    # when
    result = _merge(base, base, remote)

    # then
    assert result == TextMerge(text=remote, conflicted=False)


@given(st.text(), st.text())
def test_identical_both_sides_changes_agree(base: str, changed: str) -> None:
    # when
    result = _merge(base, changed, changed)

    # then
    assert result == TextMerge(text=changed, conflicted=False)


@given(st.text(), st.text(), st.text())
def test_a_conflict_free_merge_is_stable(base: str, local: str, remote: str) -> None:
    # given
    first = _merge(base, local, remote)
    assume(not first.conflicted)

    # when — the merged text replayed as the local side
    second = _merge(base, first.text, remote)

    # then
    assert second.text == first.text
