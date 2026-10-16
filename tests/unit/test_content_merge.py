# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the byte-level content merge.

The load-bearing rule: the driver runs only on a both-sides change, and an undecodable
both-sides change is a conflict rather than a decode crash (``docs/design/03-manifest.md``).
"""

from __future__ import annotations

from syncintosh.core.content_merge import merge_content
from syncintosh.core.models import ContentMerge, TextMerge


class RecordingDriver:
    """A `MergeDriver` that records its inputs and returns a canned result."""

    def __init__(self, result: TextMerge) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._result = result

    def merge(self, base: str, local: str, remote: str) -> TextMerge:
        self.calls.append((base, local, remote))
        return self._result


def test_identical_sides_need_no_driver() -> None:
    # given
    driver = RecordingDriver(TextMerge(text="unused", conflicted=True))

    # when
    result = merge_content(b"a\n", b"same\n", b"same\n", driver)

    # then
    assert result == ContentMerge(data=b"same\n", conflicted=False)
    assert driver.calls == []


def test_a_local_only_change_is_taken_without_the_driver() -> None:
    # given
    driver = RecordingDriver(TextMerge(text="unused", conflicted=True))

    # when
    result = merge_content(b"base\n", b"local\n", b"base\n", driver)

    # then
    assert result == ContentMerge(data=b"local\n", conflicted=False)
    assert driver.calls == []


def test_a_remote_only_change_is_taken_without_the_driver() -> None:
    # given
    driver = RecordingDriver(TextMerge(text="unused", conflicted=True))

    # when
    result = merge_content(b"base\n", b"base\n", b"remote\n", driver)

    # then
    assert result == ContentMerge(data=b"remote\n", conflicted=False)
    assert driver.calls == []


def test_a_both_sides_change_runs_the_driver_on_decoded_text() -> None:
    # given
    driver = RecordingDriver(TextMerge(text="merged\n", conflicted=False))

    # when
    result = merge_content(b"base\n", b"local\n", b"remote\n", driver)

    # then
    assert result == ContentMerge(data=b"merged\n", conflicted=False)
    assert driver.calls == [("base\n", "local\n", "remote\n")]


def test_a_conflicted_driver_result_is_passed_through() -> None:
    # given
    driver = RecordingDriver(TextMerge(text="<<<<<<< local\n", conflicted=True))

    # when
    result = merge_content(b"base\n", b"local\n", b"remote\n", driver)

    # then
    assert result == ContentMerge(data=b"<<<<<<< local\n", conflicted=True)


def test_an_undecodable_both_sides_change_is_a_conflict() -> None:
    # given — a byte that is not valid UTF-8 on both changed sides
    driver = RecordingDriver(TextMerge(text="unused", conflicted=False))
    local = b"\xff\xfe local"

    # when
    result = merge_content(b"base", local, b"\xff\xfe remote", driver)

    # then — the driver is never handed bytes it cannot decode
    assert result == ContentMerge(data=local, conflicted=True)
    assert driver.calls == []


def test_an_undecodable_one_sided_change_is_still_applied() -> None:
    # given
    driver = RecordingDriver(TextMerge(text="unused", conflicted=True))
    local = b"\xff\xfe local"

    # when
    result = merge_content(b"base", local, b"base", driver)

    # then
    assert result == ContentMerge(data=local, conflicted=False)
    assert driver.calls == []
