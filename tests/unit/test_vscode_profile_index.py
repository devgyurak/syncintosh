# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for reading VS Code's profile list out of a parsed `storage.json`.

The key names (`userDataProfiles`, `name`, `location`, `icon`, `useDefaultFlags`) are the ones VS Code's
own source uses (issue #22); `tests/fixtures/vscode/storage.json` carries only those, plus two keys the
file really holds so the tests can show they are never read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncintosh.presets.vscode import read_profiles

FIXTURE = Path(__file__).parents[1] / "fixtures" / "vscode" / "storage.json"


def _read(entries: object) -> tuple[list[tuple[str, str]], list[str], bool]:
    profiles = read_profiles({"userDataProfiles": entries})
    return [(e.name, e.directory) for e in profiles.entries], list(profiles.problems), profiles.readable


def test_the_fixture_resolves_the_two_user_profiles_and_skips_the_builtin_one() -> None:
    # given
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # when
    profiles = read_profiles(document)

    # then
    assert ([(e.name, e.directory) for e in profiles.entries], len(profiles.problems)) == (
        [("Work", "-3f2a1b"), ("Writing", "7c91e0aa")],
        1,
    )


def test_a_negative_id_is_a_valid_directory() -> None:
    # when
    entries, problems, _ = _read([{"name": "Work", "location": "-80000000"}])

    # then
    assert (entries, problems) == ([("Work", "-80000000")], [])


def test_only_the_borrowed_resources_are_reported() -> None:
    # given
    flags = {"settings": True, "keybindings": False, "snippets": True}

    # when
    profiles = read_profiles({"userDataProfiles": [{"name": "W", "location": "a1", "useDefaultFlags": flags}]})

    # then
    assert profiles.entries[0].borrowed == frozenset({"settings", "snippets"})


@pytest.mark.parametrize("key_absent", [{}, {"windowsState": {}}])
def test_no_profile_key_means_no_profiles_and_is_readable(key_absent: dict) -> None:
    # when
    profiles = read_profiles(key_absent)

    # then
    assert (profiles.entries, profiles.problems, profiles.readable) == ((), (), True)


@pytest.mark.parametrize("document", [[], "x", None, 3, {"userDataProfiles": {}}, {"userDataProfiles": "Work"}])
def test_a_document_that_is_not_a_profile_list_is_unreadable(document: object) -> None:
    # when
    profiles = read_profiles(document)

    # then
    assert (profiles.readable, profiles.entries) == (False, ())


@pytest.mark.parametrize(
    "location",
    [
        "builtin/agents",
        "builtin/other",
        "a/b",
        "..",
        "",
        "ABC",
        "-",
        "/abs",
        None,
        5,
        {"path": "/x/User/profiles/a/b"},
        {"path": "/x/Other/profiles/a1"},
        {"path": "/x/User/other/a1"},
        {"path": "a1"},
        {"path": 5},
        {},
    ],
)
def test_a_location_that_is_not_one_profile_id_is_skipped_and_reported(location: object) -> None:
    # when
    entries, problems, readable = _read([{"name": "Work", "location": location}])

    # then
    assert (entries, len(problems), readable) == ([], 1, True)


MINE = "/Users/example/Library/Application Support/Code/User/profiles"


def _read_uri(uri: object, *homes: str) -> tuple[list[tuple[str, str]], int]:
    profiles = read_profiles({"userDataProfiles": [{"name": "Old", "location": uri}]}, homes)
    return [(e.name, e.directory) for e in profiles.entries], len(profiles.problems)


def test_an_old_uri_location_is_read_when_it_is_exactly_this_machines_profiles_home() -> None:
    # given — the old shape: a URI object (scheme, path, ...)
    uri = {"$mid": 1, "path": f"{MINE}/1a2b3c", "scheme": "file"}

    # when
    result = _read_uri(uri, MINE)

    # then
    assert result == ([("Old", "1a2b3c")], 0)


@pytest.mark.parametrize(
    "uri",
    [
        # another user-data-dir (`--user-data-dir`, a portable install) that merely ends the same way
        {"scheme": "file", "path": "/Volumes/Other/User/profiles/1a2b3c"},
        {"scheme": "file", "path": "/Users/other/Library/Application Support/Code/User/profiles/1a2b3c"},
        # not a local file
        {"scheme": "vscode-remote", "path": f"{MINE}/1a2b3c"},
        {"path": f"{MINE}/1a2b3c"},
        # a deeper or a differently spelled path
        {"scheme": "file", "path": f"{MINE}/1a2b3c/"},
        {"scheme": "file", "path": f"{MINE}/x/1a2b3c"},
        {"scheme": "file", "path": MINE.upper() + "/1a2b3c"},
    ],
)
def test_an_old_uri_location_elsewhere_is_skipped_and_reported(uri: dict) -> None:
    # when
    result = _read_uri(uri, MINE)

    # then
    assert result == ([], 1)


def test_an_old_uri_location_is_skipped_when_no_home_is_given() -> None:
    # when — fail closed: with nothing to compare against, a URI proves nothing
    result = _read_uri({"scheme": "file", "path": f"{MINE}/1a2b3c"})

    # then
    assert result == ([], 1)


def test_a_uri_is_read_against_either_spelling_of_the_home() -> None:
    # given — the home as written and as the filesystem resolves it
    real = "/private/Users/example/Library/Application Support/Code/User/profiles"

    # when
    result = _read_uri({"scheme": "file", "path": f"{real}/1a2b3c"}, MINE, real)

    # then
    assert result == ([("Old", "1a2b3c")], 0)


@pytest.mark.parametrize(
    "entry", [None, "Work", 3, [], {"location": "a1"}, {"name": "", "location": "a1"}, {"name": 4, "location": "a1"}]
)
def test_a_malformed_entry_is_skipped_without_echoing_it(entry: object) -> None:
    # when
    entries, problems, _ = _read([entry, {"name": "Ok", "location": "b2"}])

    # then
    assert (entries, len(problems)) == ([("Ok", "b2")], 1)


@pytest.mark.parametrize("flags", ["settings", ["settings"], {"settings": "yes"}, {"settings": 1}, None])
def test_flags_that_are_not_booleans_skip_the_profile(flags: object) -> None:
    # when
    entries, problems, _ = _read([{"name": "Work", "location": "a1", "useDefaultFlags": flags}])

    # then
    assert (entries, len(problems)) == ([], 1)


def test_a_lone_surrogate_name_is_skipped() -> None:
    # when
    entries, problems, _ = _read([{"name": "bad\ud800", "location": "a1"}])

    # then
    assert (entries, len(problems)) == ([], 1)


@pytest.mark.parametrize(
    "pair",
    [
        [{"name": "Work", "location": "a1"}, {"name": "work", "location": "b2"}],
        [{"name": "A", "location": "a1"}, {"name": "B", "location": "a1"}],
        [{"name": "café", "location": "a1"}, {"name": "café", "location": "b2"}],
    ],
)
def test_colliding_profiles_are_both_dropped_whatever_their_order(pair: list[dict]) -> None:
    # when
    forward, backward = _read(pair), _read(pair[::-1])

    # then
    assert (forward[0], backward[0], len(forward[1])) == ([], [], 2)


def test_the_error_text_never_echoes_a_path_or_icon() -> None:
    # given
    secret = "/Users/example/very-private-path"

    # when
    _, problems, _ = _read([{"name": "Work", "location": secret, "icon": secret}])

    # then
    assert [p for p in problems if secret in p] == []


def test_a_name_equal_to_another_profiles_directory_is_not_a_clash() -> None:
    # when
    entries, problems, _ = _read([{"name": "a1", "location": "b2"}, {"name": "Other", "location": "a1"}])

    # then
    assert (entries, problems) == ([("a1", "b2"), ("Other", "a1")], [])


@pytest.mark.parametrize("flags", [{"": True}, {"settings": True, "": False}])
def test_an_empty_flag_key_skips_only_that_profile(flags: dict) -> None:
    # when
    entries, problems, readable = _read(
        [{"name": "Work", "location": "abc", "useDefaultFlags": flags}, {"name": "Home", "location": "def"}]
    )

    # then — the rest of the list survives, and the skipped one is reported once without its content
    assert (entries, len(problems), readable) == ([("Home", "def")], 1, True)
