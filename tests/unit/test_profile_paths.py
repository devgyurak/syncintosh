# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the live-path resolver, the profile config entry, and what a watch adds for profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from syncintosh.core.config import LocalProfiles, SyncConfig
from syncintosh.core.errors import ConfigError
from syncintosh.core.profiles import ProfileEntry, ProfileMap
from syncintosh.core.watch import FileEvent, WatchFilter
from syncintosh.presets import BuiltinCatalog, vscode
from syncintosh.services.profile_paths import (
    LivePaths,
    Located,
    ProfileView,
    Unresolved,
    describe_with,
    open_live_paths,
    profile_summary,
    profile_targets,
)
from syncintosh.services.setup import new_machine_config
from syncintosh.system.config import load_config, save_config

SPEC = vscode.PROFILES
CATALOG = BuiltinCatalog.default()
HOME = vscode.PROFILE_HOME


def _paths(profiles: ProfileMap) -> LivePaths:
    return LivePaths(catalog=CATALOG, views=(ProfileView(spec=SPEC, profiles=profiles),))


def _work(**kwargs) -> ProfileMap:
    return ProfileMap.create((ProfileEntry.create(name="Work", directory="7c91e0aa", **kwargs),))


def _config(names="*") -> SyncConfig:
    entry = LocalProfiles.create(preset="vscode", spec=SPEC, scope="common", names=names)
    return SyncConfig.create(remote="r", machine_id="m", items=(), profiles=(entry,))


class _Index:
    def __init__(self, profiles: ProfileMap) -> None:
        self._profiles = profiles
        self.reads = 0

    def read(self, spec):
        self.reads += 1
        return self._profiles


# --------------------------------------------------------------------------- resolve


def test_an_ordinary_path_resolves_to_itself() -> None:
    # when
    located = LivePaths(catalog=CATALOG).resolve("Library/Application Support/Code/User/settings.json")

    # then
    assert located == Located("Library/Application Support/Code/User/settings.json")


def test_a_logical_path_resolves_to_this_macs_directory() -> None:
    # given
    path = SPEC.logical_path("Work", "snippets/js.json")

    # when
    located = _paths(_work()).resolve(path)

    # then
    assert located == Located(f"{HOME}/7c91e0aa/snippets/js.json", profile_directory=f"{HOME}/7c91e0aa")


@pytest.mark.parametrize(
    ("paths", "name", "expected"),
    [
        (_paths(ProfileMap.create(())), "Missing", 'create the profile "Missing" in the app'),
        (_paths(ProfileMap.unreadable("gone")), "Work", "could not be read"),
        (LivePaths(catalog=CATALOG), "Work", "does not track profiles"),
        (_paths(_work(borrowed=["settings"])), "Work", "uses the default profile's settings"),
    ],
)
def test_a_logical_path_with_no_real_one_says_why(paths: LivePaths, name: str, expected: str) -> None:
    # given
    path = SPEC.logical_path(name, "settings.json")

    # when
    result = paths.resolve(path)

    # then
    assert expected in getattr(result, "reason", "")


def test_an_unresolved_path_is_never_returned_as_its_logical_text() -> None:
    # given
    path = SPEC.logical_path("Work", "settings.json")

    # when
    result = _paths(ProfileMap.create(())).resolve(path)

    # then
    assert isinstance(result, Unresolved)


def test_a_name_is_matched_by_its_folded_key() -> None:
    # when
    result = _paths(_work()).resolve(SPEC.logical_path("WORK", "settings.json"))

    # then
    assert isinstance(result, Located)


def test_a_path_is_described_as_a_profile_file() -> None:
    # when
    shown = describe_with(CATALOG, SPEC.logical_path("My Work", "settings.json"))

    # then
    assert shown == 'vscode profile "My Work": settings.json'


@pytest.mark.parametrize("path", [".zshrc", f"{HOME}/-3f2a1b/settings.json"])
def test_any_other_path_is_shown_as_it_is(path: str) -> None:
    # when
    shown = describe_with(CATALOG, path)

    # then
    assert shown == path


def test_the_index_is_read_once_per_layout() -> None:
    # given
    index = _Index(_work())

    # when
    open_live_paths(_config(), CATALOG, index)

    # then
    assert index.reads == 1


def test_no_index_port_means_no_profile_resolves() -> None:
    # when
    paths = open_live_paths(_config(), CATALOG, None)

    # then
    assert isinstance(paths.resolve(SPEC.logical_path("Work", "settings.json")), Unresolved)


# --------------------------------------------------------------------------- watch


def test_a_watch_adds_each_profiles_files_and_follows_the_index() -> None:
    # given
    paths = _paths(_work(borrowed=["keybindings"]))

    # when
    targets = profile_targets(_config(), paths)
    filter_ = WatchFilter.create(_config(), targets)

    # then — settings and the snippet globs, not the borrowed keybindings; the index's directory too
    assert targets.paths == (f"{HOME}/7c91e0aa/settings.json",)
    assert len(targets.globs) == 2
    assert filter_.matches(FileEvent(path=vscode.PROFILE_INDEX)) is True
    assert filter_.matches(FileEvent(path=f"{HOME}/7c91e0aa/snippets/a.json")) is True
    assert filter_.matches(FileEvent(path=f"{HOME}/7c91e0aa/keybindings.json")) is False
    assert f"{vscode.PROFILE_ROOT}/globalStorage" in filter_.directories()


def test_a_watch_skips_a_profile_the_config_does_not_allow() -> None:
    # when
    targets = profile_targets(_config(names=("Other",)), _paths(_work()))

    # then
    assert (targets.paths, targets.globs, targets.indexes) == ((), (), (vscode.PROFILE_INDEX,))


# --------------------------------------------------------------------------- doctor


def test_the_summary_names_profiles_without_paths() -> None:
    # when
    summary = profile_summary(_config(), _paths(_work()))

    # then
    assert summary is not None
    assert (summary.name, summary.ok, summary.detail) == ("profiles", True, 'profiles here: "Work"')


def test_the_summary_says_an_unreadable_list_keeps_the_records() -> None:
    # when
    summary = profile_summary(_config(), _paths(ProfileMap.unreadable("the profile index does not exist")))

    # then
    assert summary is not None and "records are kept" in summary.detail


def test_no_summary_without_a_profile_entry() -> None:
    # when
    summary = profile_summary(SyncConfig.create(remote="r", machine_id="m", items=()), LivePaths(catalog=CATALOG))

    # then
    assert summary is None


# --------------------------------------------------------------------------- init and config


def test_init_writes_a_star_entry_for_a_preset_with_profiles() -> None:
    # when
    config = new_machine_config(remote="r", machine_id="m", preset_names=("vscode", "zsh"), catalog=CATALOG)

    # then
    assert [(entry.preset, entry.names) for entry in config.profiles] == [("vscode", "*")]


def test_a_profiles_entry_round_trips_through_the_config_file(tmp_path: Path) -> None:
    # given
    path = tmp_path / "config.json"
    entry = LocalProfiles.create(preset="vscode", spec=SPEC, scope="common", names=("Work", "Play"))
    config = SyncConfig.create(remote="r", machine_id="m", items=(), profiles=(entry,))

    # when
    save_config(config, path)
    loaded = load_config(path)

    # then
    assert [(e.preset, e.names) for e in loaded.profiles] == [("vscode", ("Work", "Play"))]


def test_a_config_without_profiles_keeps_its_shape(tmp_path: Path) -> None:
    # given
    path = tmp_path / "config.json"

    # when
    save_config(SyncConfig.create(remote="r", machine_id="m", items=()), path)

    # then
    assert "profiles" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "entry",
    [
        '"x"',
        '{"preset": "vscode", "scope": "common"}',
        '{"preset": "zsh", "scope": "common", "names": "*"}',
        '{"preset": "nope", "scope": "common", "names": "*"}',
        '{"preset": "vscode", "scope": "common", "names": "*", "extra": 1}',
        '{"preset": "vscode", "scope": "common", "names": "Work"}',
    ],
)
def test_a_malformed_profiles_entry_is_a_config_error(tmp_path: Path, entry: str) -> None:
    # given
    path = tmp_path / "config.json"
    path.write_text('{"remote": "r", "machine_id": "m", "items": [], "profiles": [' + entry + "]}", encoding="utf-8")

    # when & then
    with pytest.raises(ConfigError):
        load_config(path)


def test_profiles_must_be_a_list(tmp_path: Path) -> None:
    # given
    path = tmp_path / "config.json"
    path.write_text('{"remote": "r", "machine_id": "m", "items": [], "profiles": {}}', encoding="utf-8")

    # when & then
    with pytest.raises(ConfigError):
        load_config(path)


def test_a_diff_does_not_read_through_a_symlinked_file(tmp_path: Path) -> None:
    # given — a tracked file that is a symlink; the diff must report it instead of reading the target
    from syncintosh.core.manifest import ManifestRecord
    from syncintosh.services.diff import _diff_record
    from syncintosh.services.sync import _Presets
    from syncintosh.system.deny_list import SystemDenyList
    from syncintosh.system.live_files import SystemLiveFiles

    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    (tmp_path / "settings.json").symlink_to(target)
    record = ManifestRecord.create(
        file_id="0f3c4b5a69788796a5b4c3d2e1f0abcd",
        path="settings.json",
        preset="community",
        scope="common",
        machine=None,
        format="jsonc",
        sha256="a" * 64,
    )

    # when
    # transport and cipher are never reached: the symlink check returns first
    entry = _diff_record(
        cast(Any, None),
        cast(Any, None),
        SystemLiveFiles(),
        _Presets(catalog=CATALOG, adapters={}, validators={}),
        str(tmp_path),
        None,
        "0f3c4b5a69788796a5b4c3d2e1f0abcd",
        record,
        LivePaths(catalog=CATALOG),
        SystemDenyList(),
    )

    # then
    assert (entry.status, entry.here) == ("deferred", None)


@pytest.mark.parametrize(
    "path",
    [f"{HOME}/-3f2a1b/settings.json", f"{HOME}/@Cafe%CC%81/settings.json", f"{HOME}/@wo%72k/settings.json"],
)
def test_resolve_alone_refuses_a_reserved_path_that_is_not_logical(path: str) -> None:
    # when
    result = _paths(_work()).resolve(path)

    # then — never handed back as a real path, even if a caller forgot to ask the catalog first
    assert isinstance(result, Unresolved)
