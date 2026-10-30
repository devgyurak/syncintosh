# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the system profile index: an in-memory read of the app's own list, and directory safety."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from syncintosh.core.ports import ProfileIndex
from syncintosh.presets import vscode
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.profile_index import MAX_INDEX_BYTES, SystemProfileIndex

SPEC = vscode.PROFILES
FIXTURE = Path(__file__).parents[2] / "fixtures" / "vscode" / "storage.json"


def _index(home: Path) -> SystemProfileIndex:
    return SystemProfileIndex(str(home), SystemDenyList(), vscode.read_profiles)


def _write_index(home: Path, text: str) -> None:
    path = home / SPEC.index
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def test_the_fixture_index_resolves_through_the_port(tmp_path: Path) -> None:
    # given
    _write_index(tmp_path, FIXTURE.read_text(encoding="utf-8"))

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert (isinstance(_index(tmp_path), ProfileIndex), [e.name for e in profiles.entries]) == (
        True,
        ["Work", "Writing"],
    )


def test_a_missing_index_is_unreadable_not_empty(tmp_path: Path) -> None:
    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert (profiles.readable, profiles.entries) == (False, ())


@pytest.mark.parametrize("text", ["{", "", "﻿[", "[]", '"x"', '{"userDataProfiles": 3}'])
def test_a_corrupt_index_is_unreadable_and_says_so_without_its_content(tmp_path: Path, text: str) -> None:
    # given
    _write_index(tmp_path, text)

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert (profiles.readable, profiles.entries) == (False, ())


def test_an_index_that_is_not_utf8_is_unreadable(tmp_path: Path) -> None:
    # given
    path = tmp_path / SPEC.index
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00")

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert profiles.readable is False


def test_an_index_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    # given
    real = tmp_path / "elsewhere.json"
    real.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / SPEC.index
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert (profiles.readable, profiles.entries) == (False, ())


def test_an_index_that_cannot_be_read_is_unreadable(tmp_path: Path) -> None:
    # given — a directory where the file should be
    (tmp_path / SPEC.index).mkdir(parents=True)

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert profiles.readable is False


def test_a_symlinked_profile_directory_is_dropped_and_reported(tmp_path: Path) -> None:
    # given
    _write_index(tmp_path, json.dumps({"userDataProfiles": [{"name": "Work", "location": "a1"}]}))
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / SPEC.home
    home.mkdir(parents=True)
    (home / "a1").symlink_to(outside)

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert (profiles.readable, profiles.entries, len(profiles.problems)) == (True, (), 1)


def test_a_profile_directory_that_leaves_home_is_dropped(tmp_path: Path) -> None:
    # given — `profiles` itself is a link out of the home, so the directory under it escapes
    fake_home = tmp_path / "home"
    _write_index(fake_home, json.dumps({"userDataProfiles": [{"name": "Work", "location": "a1"}]}))
    outside = tmp_path / "outside"
    (outside / "a1").mkdir(parents=True)
    (fake_home / SPEC.home).parent.mkdir(parents=True, exist_ok=True)
    (fake_home / SPEC.home).symlink_to(outside)

    # when
    profiles = _index(fake_home).read(SPEC)

    # then
    assert (profiles.entries, len(profiles.problems)) == ((), 1)


def test_a_profile_directory_inside_a_denied_path_is_dropped(tmp_path: Path) -> None:
    # given — `.ssh/profiles` stands in for a layout that was moved under a denied directory
    from syncintosh.core.profiles import ProfileFile, ProfileSpec

    spec = ProfileSpec.create(
        default_root=".ssh/User",
        home=".ssh/User/profiles",
        index=".ssh/User/storage.json",
        files=(ProfileFile.create(path="settings.json", resource="settings"),),
    )
    path = tmp_path / spec.index
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"userDataProfiles": [{"name": "Work", "location": "a1"}]}), encoding="utf-8")

    # when
    profiles = _index(tmp_path).read(spec)

    # then
    assert (profiles.entries, len(profiles.problems)) == ((), 1)


def test_a_missing_profile_directory_still_resolves_so_a_deferral_can_be_decided_later(tmp_path: Path) -> None:
    # given — VS Code can list a profile whose folder has not been written yet
    _write_index(tmp_path, json.dumps({"userDataProfiles": [{"name": "Work", "location": "a1"}]}))

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert [e.name for e in profiles.entries] == ["Work"]


def test_reading_never_writes_anything(tmp_path: Path) -> None:
    # given
    _write_index(tmp_path, FIXTURE.read_text(encoding="utf-8"))
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

    # when
    _index(tmp_path).read(SPEC)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

    # then
    assert after == before


def test_a_deeply_nested_index_is_unreadable_instead_of_crashing(tmp_path: Path) -> None:
    # given
    _write_index(tmp_path, "[" * 100000)

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert profiles.readable is False


def test_an_oversized_index_is_unreadable(tmp_path: Path) -> None:
    # given
    _write_index(tmp_path, " " * (MAX_INDEX_BYTES + 1))

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert profiles.readable is False


def test_an_index_under_a_symlinked_parent_that_leaves_home_is_refused(tmp_path: Path) -> None:
    # given — `globalStorage` is a link out of the home
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "storage.json").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (home / SPEC.index).parent.parent.mkdir(parents=True)
    (home / SPEC.index).parent.symlink_to(outside)

    # when
    profiles = _index(home).read(SPEC)

    # then
    assert (profiles.readable, profiles.entries) == (False, ())


def test_a_parser_that_raises_a_domain_error_gives_an_unreadable_map(tmp_path: Path) -> None:
    # given — a parser that cannot understand the document
    from syncintosh.core.errors import ConfigError

    def broken(_document, _homes):
        raise ConfigError("some detail that must not be echoed")

    _write_index(tmp_path, FIXTURE.read_text(encoding="utf-8"))

    # when
    profiles = SystemProfileIndex(str(tmp_path), SystemDenyList(), broken).read(SPEC)

    # then — the port's contract is that it never raises, and the detail stays out of the report
    assert (profiles.readable, profiles.entries, profiles.problems) == (
        False,
        (),
        ("the profile index could not be understood",),
    )


def test_the_parser_is_told_this_machines_profiles_home(tmp_path: Path) -> None:
    # given
    seen: list[tuple[str, ...]] = []

    def spy(document, homes):
        seen.append(homes)
        return vscode.read_profiles(document, homes)

    _write_index(tmp_path, FIXTURE.read_text(encoding="utf-8"))

    # when
    SystemProfileIndex(str(tmp_path), SystemDenyList(), spy).read(SPEC)

    # then — the home as written and as the filesystem resolves it
    written = str(tmp_path / SPEC.home)
    assert seen == [tuple(dict.fromkeys((written, os.path.realpath(written))))]


def test_an_old_uri_pointing_at_another_root_is_skipped_through_the_reader(tmp_path: Path) -> None:
    # given
    uri = {"scheme": "file", "path": "/Volumes/Other/Code/User/profiles/abc"}
    _write_index(tmp_path, json.dumps({"userDataProfiles": [{"name": "Work", "location": uri}]}))

    # when
    profiles = _index(tmp_path).read(SPEC)

    # then
    assert (profiles.readable, profiles.entries, len(profiles.problems)) == (True, (), 1)


def test_an_unreadable_profiles_directory_gives_an_unreadable_map_not_an_exception(tmp_path: Path) -> None:
    # given — a listed profile whose parent directory cannot be searched
    _write_index(tmp_path, json.dumps({"userDataProfiles": [{"name": "Work", "location": "abc"}]}))
    home = tmp_path / SPEC.home
    home.mkdir(parents=True)
    home.chmod(0)

    # when
    try:
        profiles = _index(tmp_path).read(SPEC)
    finally:
        home.chmod(0o755)

    # then — the port promises not to raise; the profile is not resolved and nothing is said about the file
    assert (profiles.entries, [p for p in profiles.problems if "abc" in p]) == ((), [])
