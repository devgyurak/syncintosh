# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pending-push marker file and its value type."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import RotationMarkerCorrupt
from syncintosh.core.models import PendingPush
from syncintosh.system.rotation_marker import FileRotationMarker

COMMIT = "a" * 40


def test_a_recorded_marker_reads_back(tmp_path) -> None:
    # given
    store = FileRotationMarker(tmp_path / "state" / "pending-push.json")

    # when
    store.record(PendingPush.create(generation=3, commit=COMMIT))

    # then
    assert store.load() == PendingPush(generation=3, commit=COMMIT)


def test_a_second_record_replaces_the_first(tmp_path) -> None:
    # given
    store = FileRotationMarker(tmp_path / "pending-push.json")
    store.record(PendingPush.create(generation=2, commit=COMMIT))

    # when
    store.record(PendingPush.create(generation=3, commit="b" * 40))

    # then
    assert store.load() == PendingPush(generation=3, commit="b" * 40)


def test_no_marker_reads_as_none_and_clearing_is_idempotent(tmp_path) -> None:
    # given
    store = FileRotationMarker(tmp_path / "pending-push.json")

    # when
    store.clear()

    # then
    assert store.load() is None


def test_clear_removes_the_marker(tmp_path) -> None:
    # given
    store = FileRotationMarker(tmp_path / "pending-push.json")
    store.record(PendingPush.create(generation=2, commit=COMMIT))

    # when
    store.clear()

    # then
    assert store.load() is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"generation": 2}',
        '{"generation": "2", "commit": "x"}',
        '{"generation": true, "commit": "x"}',
        '{"generation": 1, "commit": "x"}',
        '{"generation": 2, "commit": ""}',
    ],
    ids=[
        "empty",
        "not_json",
        "list",
        "empty_object",
        "no_commit",
        "string_generation",
        "bool_generation",
        "gen_one",
        "empty_commit",
    ],
)
def test_a_corrupt_marker_is_an_error(tmp_path, text) -> None:
    # given
    path = tmp_path / "pending-push.json"
    path.write_text(text, encoding="utf-8")

    # when & then
    with pytest.raises(RotationMarkerCorrupt):
        FileRotationMarker(path).load()


def test_an_unreadable_marker_is_an_error(tmp_path) -> None:
    # given — a directory where the file should be
    path = tmp_path / "pending-push.json"
    path.mkdir()

    # when & then
    with pytest.raises(RotationMarkerCorrupt):
        FileRotationMarker(path).load()


@pytest.mark.parametrize(
    ("generation", "commit"), [(1, COMMIT), (0, COMMIT), (2, "")], ids=["one", "zero", "no_commit"]
)
def test_a_marker_without_a_rotation_to_name_is_refused(generation, commit) -> None:
    # when & then
    with pytest.raises(ValueError):
        PendingPush.create(generation=generation, commit=commit)
