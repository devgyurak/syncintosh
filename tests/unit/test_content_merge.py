# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the byte-level content merge.

The load-bearing rule: the driver runs only on a both-sides change, and an undecodable
both-sides change is a conflict rather than a decode crash (``docs/design/03-manifest.md``).
"""

from __future__ import annotations

import json

from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import ContentParseError
from syncintosh.core.models import ContentMerge, TextMerge
from syncintosh.core.resolution import Proposal
from syncintosh.core.tree import Tree, tree_from_value


class RecordingDriver:
    """A `MergeDriver` that records its inputs and returns a canned result."""

    def __init__(self, result: TextMerge) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._result = result

    def merge(self, base: str, local: str, remote: str) -> TextMerge:
        self.calls.append((base, local, remote))
        return self._result


class JsonAdapter:
    """A minimal `ContentAdapter` over JSON, enough to exercise the routing."""

    def __init__(self, *, parse_raises: bool = False, corrupt_render: bool = False) -> None:
        self.parse_calls = 0
        self._parse_raises = parse_raises
        self._corrupt_render = corrupt_render

    def parse(self, text: str) -> Tree:
        self.parse_calls += 1
        if self._parse_raises:
            raise ContentParseError("nope")
        try:
            return tree_from_value(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ContentParseError(str(exc)) from exc

    def render(self, tree: Tree, template: str) -> str:
        if self._corrupt_render:
            return "{}"
        return json.dumps(tree.to_value())


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


def test_a_both_sides_change_routes_through_an_adapter() -> None:
    # given — different keys changed on each side
    driver = RecordingDriver(TextMerge(text="unused", conflicted=False))
    adapter = JsonAdapter()

    # when
    result = merge_content(b'{"a": 1}', b'{"a": 2, "b": 3}', b'{"a": 1, "c": 4}', driver, adapter)

    # then — the adapter merged the keys, and the driver was not consulted
    assert result.conflicted is False
    assert json.loads(result.data) == {"a": 2, "b": 3, "c": 4}
    assert driver.calls == []


def test_a_one_sided_change_never_touches_the_adapter() -> None:
    # given
    adapter = JsonAdapter()

    # when
    result = merge_content(
        b'{"a": 1}', b'{"a": 2}', b'{"a": 1}', RecordingDriver(TextMerge(text="", conflicted=False)), adapter
    )

    # then
    assert result == ContentMerge(data=b'{"a": 2}', conflicted=False)
    assert adapter.parse_calls == 0


def test_an_unparseable_side_is_a_conflict() -> None:
    # given — local is not valid JSON
    adapter = JsonAdapter()

    # when
    result = merge_content(
        b"{}", b"{not json", b'{"a": 1}', RecordingDriver(TextMerge(text="", conflicted=False)), adapter
    )

    # then
    assert result == ContentMerge(data=b"{not json", conflicted=True)


def test_a_parse_that_raises_is_a_conflict() -> None:
    # given
    adapter = JsonAdapter(parse_raises=True)

    # when
    result = merge_content(b"a", b"b", b"c", RecordingDriver(TextMerge(text="", conflicted=False)), adapter)

    # then
    assert result == ContentMerge(data=b"b", conflicted=True)


def test_a_both_sides_conflict_on_one_key_is_a_conflict() -> None:
    # given — the adapter's engine finds a conflicting key
    adapter = JsonAdapter()

    # when
    result = merge_content(
        b'{"a": 1}', b'{"a": 2}', b'{"a": 3}', RecordingDriver(TextMerge(text="", conflicted=False)), adapter
    )

    # then — the colliding path is reported (never its values) and the local side is the candidate
    assert result.data == b'{"a": 2}'
    assert result.conflicted is True
    assert result.conflict_paths == (("a",),)


def test_a_render_that_does_not_round_trip_is_a_conflict() -> None:
    # given — the render loses the merged content
    adapter = JsonAdapter(corrupt_render=True)

    # when
    result = merge_content(
        b'{"a": 1}',
        b'{"a": 2, "b": 3}',
        b'{"a": 1, "c": 4}',
        RecordingDriver(TextMerge(text="", conflicted=False)),
        adapter,
    )

    # then
    assert result == ContentMerge(data=b'{"a": 2, "b": 3}', conflicted=True)


def test_an_undecodable_both_sides_change_skips_the_adapter() -> None:
    # given
    adapter = JsonAdapter()
    local = b"\xff\xfe local"

    # when
    result = merge_content(
        b"base", local, b"\xff\xfe remote", RecordingDriver(TextMerge(text="", conflicted=False)), adapter
    )

    # then
    assert result == ContentMerge(data=local, conflicted=True)
    assert adapter.parse_calls == 0


def test_a_key_proposal_settles_its_key_to_the_proposed_value() -> None:
    # given
    driver = RecordingDriver(TextMerge(text="unused", conflicted=True))

    # when
    result = merge_content(
        b'{"a": 1}', b'{"a": 2}', b'{"a": 3}', driver, JsonAdapter(), key_choices={("a",): Proposal("4")}
    )

    # then
    assert result == ContentMerge(data=b'{"a": 4}', conflicted=False)
    assert driver.calls == []


def test_a_key_proposal_that_does_not_parse_leaves_its_key_in_conflict() -> None:
    # when
    result = merge_content(
        b'{"a": 1}',
        b'{"a": 2}',
        b'{"a": 3}',
        RecordingDriver(TextMerge(text="unused", conflicted=True)),
        JsonAdapter(),
        key_choices={("a",): Proposal("{")},
    )

    # then
    assert result.conflicted is True
    assert result.conflict_paths == (("a",),)


def test_a_whole_file_proposal_that_does_not_parse_is_a_conflict() -> None:
    # when
    result = merge_content(
        b'{"a": 1}',
        b'{"a": 2}',
        b'{"a": 3}',
        RecordingDriver(TextMerge(text="unused", conflicted=True)),
        JsonAdapter(),
        choice=Proposal("{"),
    )

    # then
    assert result == ContentMerge(data=b'{"a": 2}', conflicted=True)


def test_a_whole_file_proposal_is_taken_verbatim_when_there_is_no_adapter() -> None:
    # given
    driver = RecordingDriver(TextMerge(text="unused", conflicted=True))

    # when
    result = merge_content(b"base\n", b"local\n", b"remote\n", driver, choice=Proposal("typed\n"))

    # then
    assert result == ContentMerge(data=b"typed\n", conflicted=False)
    assert driver.calls == []


def test_a_proposal_that_cannot_be_encoded_is_a_conflict() -> None:
    # given — a lone surrogate is a str that UTF-8 cannot hold
    driver = RecordingDriver(TextMerge(text="unused", conflicted=True))

    # when
    result = merge_content(b"base\n", b"local\n", b"remote\n", driver, choice=Proposal("\ud800"))

    # then
    assert result == ContentMerge(data=b"local\n", conflicted=True)
    assert driver.calls == []
