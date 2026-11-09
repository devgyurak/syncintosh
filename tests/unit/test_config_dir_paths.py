# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the resolver's config-directory half: resolve, describe, glob directories, watch targets, doctor row.

The layout and preset are TEST-ONLY (`widget`); the real JetBrains presets are a later change.
"""

from __future__ import annotations

import pytest

from syncintosh.core.config_dirs import ConfigDirMap, ConfigDirSpec
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.watch import FileEvent
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.profile_paths import (
    ConfigDirView,
    LivePaths,
    Located,
    Unresolved,
    config_dir_summary,
    config_dir_targets,
    describe_with,
    open_live_paths,
    watch_targets,
)
from syncintosh.services.setup import new_machine_config

HOME = "Library/Application Support/JetBrains"
SPEC = ConfigDirSpec.create(home=HOME, prefix="Widget", vendor="JetBrains", bundle_id="com.example.widget")
PRESET = Preset.create(
    name="widget",
    files=(
        PresetFile.create(path=SPEC.logical_path("options/editor.xml"), format="text"),
        PresetFile.create(path=SPEC.logical_path("colors/*.xml"), format="text"),
    ),
    bundle_id=SPEC.bundle_id,
    config_dir=SPEC,
)
CATALOG = BuiltinCatalog.create((PRESET,))
LOGICAL = f"{HOME}/@Widget/options/editor.xml"


class _Dirs:
    def __init__(self, found: ConfigDirMap) -> None:
        self._found = found
        self.reads = 0

    def read(self, spec: ConfigDirSpec) -> ConfigDirMap:
        self.reads += 1
        return self._found


def _paths(found: ConfigDirMap) -> LivePaths:
    return LivePaths(catalog=CATALOG, config_views=(ConfigDirView(spec=SPEC, preset="widget", directory=found),))


def _config():
    return new_machine_config(remote="r", machine_id="m", preset_names=("widget",), catalog=CATALOG)


# --------------------------------------------------------------------------- resolve


def test_a_logical_path_resolves_to_the_release_folder_the_app_names() -> None:
    # when
    located = _paths(ConfigDirMap.resolved("Widget2026.3")).resolve(LOGICAL)

    # then
    assert located == Located(f"{HOME}/Widget2026.3/options/editor.xml", config_directory=f"{HOME}/Widget2026.3")


def test_two_machines_on_different_releases_resolve_one_record_to_their_own_folders() -> None:
    # when
    older = _paths(ConfigDirMap.resolved("Widget2026.2")).resolve(LOGICAL)
    newer = _paths(ConfigDirMap.resolved("Widget2026.3")).resolve(LOGICAL)

    # then
    assert (older.path, newer.path) == (  # ty: ignore[unresolved-attribute]
        f"{HOME}/Widget2026.2/options/editor.xml",
        f"{HOME}/Widget2026.3/options/editor.xml",
    )


def test_an_unresolved_folder_yields_the_reason_and_never_the_logical_text() -> None:
    # when
    result = _paths(ConfigDirMap.unresolved("the app is not installed on this machine")).resolve(LOGICAL)

    # then
    assert result == Unresolved("the app is not installed on this machine")


def test_a_resolver_with_no_view_for_the_layout_resolves_nothing() -> None:
    # when
    result = LivePaths(catalog=CATALOG).resolve(LOGICAL)

    # then
    assert isinstance(result, Unresolved)


def test_a_port_that_returns_a_bad_name_never_reaches_a_path() -> None:
    # given — a misbehaving implementation hands back a name the layout does not allow
    found = ConfigDirMap.resolved("../../Evil")

    # when
    result = _paths(found).resolve(LOGICAL)

    # then
    assert isinstance(result, Unresolved)


@pytest.mark.parametrize(
    "path",
    [
        f"{HOME}/@widget/options/editor.xml",
        f"{HOME}/@Widget/options/unowned.xml",
        f"{HOME}/Widget2026.2/options/editor.xml",
    ],
)
def test_a_path_on_the_layout_that_no_file_owns_is_not_a_path(path: str) -> None:
    # when
    result = _paths(ConfigDirMap.resolved("Widget2026.2")).resolve(path)

    # then — never read as a literal directory
    assert isinstance(result, Unresolved)


def test_an_ordinary_path_resolves_to_itself_with_a_config_view_present() -> None:
    # when & then
    assert _paths(ConfigDirMap.resolved("Widget2026.2")).resolve(".zshrc") == Located(".zshrc")


# --------------------------------------------------------------------------- glob directories


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        (f"{HOME}/@Widget/colors", f"{HOME}/Widget2026.2/colors"),
        (f"{HOME}/@Widget", f"{HOME}/Widget2026.2"),
        ("some/other/dir", "some/other/dir"),
    ],
)
def test_a_glob_directory_is_listed_at_the_resolved_real_directory(directory: str, expected: str) -> None:
    # when
    real = _paths(ConfigDirMap.resolved("Widget2026.2")).resolve_directory("widget", directory)

    # then
    assert real == expected


def test_a_glob_directory_that_does_not_resolve_says_why() -> None:
    # when
    real = _paths(ConfigDirMap.unresolved("gone")).resolve_directory("widget", f"{HOME}/@Widget/colors")

    # then
    assert real == Unresolved("gone")


def test_a_directory_of_a_preset_with_no_layout_is_left_alone() -> None:
    # when
    real = _paths(ConfigDirMap.resolved("Widget2026.2")).resolve_directory("unknown", f"{HOME}/@Widget/colors")

    # then
    assert real == f"{HOME}/@Widget/colors"


# --------------------------------------------------------------------------- describe


def test_a_label_names_the_preset_the_file_and_the_release_without_a_path() -> None:
    # when
    label = _paths(ConfigDirMap.resolved("Widget2026.2")).describe(LOGICAL)

    # then
    assert label == "widget: options/editor.xml (Widget2026.2)"


def test_a_label_without_a_resolved_release_leaves_the_release_out() -> None:
    # when
    labels = (
        _paths(ConfigDirMap.unresolved("gone")).describe(LOGICAL),
        describe_with(CATALOG, LOGICAL),
    )

    # then
    assert labels == ("widget: options/editor.xml",) * 2


# --------------------------------------------------------------------------- reading


def test_each_layout_is_read_once_for_the_cycle() -> None:
    # given
    dirs = _Dirs(ConfigDirMap.resolved("Widget2026.2"))

    # when
    paths = open_live_paths(_config(), CATALOG, None, dirs)

    # then
    assert (dirs.reads, [view.directory.directory for view in paths.config_views]) == (1, ["Widget2026.2"])


def test_without_a_port_nothing_logical_resolves() -> None:
    # when
    paths = open_live_paths(_config(), CATALOG, None, None)

    # then
    assert (paths.config_views, isinstance(paths.resolve(LOGICAL), Unresolved)) == ((), True)


def test_a_preset_the_config_does_not_track_is_never_read() -> None:
    # given
    dirs = _Dirs(ConfigDirMap.resolved("Widget2026.2"))
    untracked = new_machine_config(remote="r", machine_id="m", preset_names=(), catalog=CATALOG)

    # when
    open_live_paths(untracked, CATALOG, None, dirs)

    # then
    assert dirs.reads == 0


# --------------------------------------------------------------------------- watch


def test_a_watch_follows_the_real_files_and_directories() -> None:
    # given
    paths = _paths(ConfigDirMap.resolved("Widget2026.2"))

    # when
    real = config_dir_targets(_config(), paths)

    # then — the items and globs name the folder the app really uses
    assert [item.path for item in real.items] == [f"{HOME}/Widget2026.2/options/editor.xml"]
    assert [entry.glob.path for entry in real.globs] == [f"{HOME}/Widget2026.2/colors/*.xml"]


def test_a_watch_event_on_the_real_file_matches_and_one_on_the_logical_text_does_not() -> None:
    # given
    dirs = _Dirs(ConfigDirMap.resolved("Widget2026.2"))
    targets = watch_targets(_config(), CATALOG, None, dirs)

    # when
    real = targets.matches(FileEvent(f"{HOME}/Widget2026.2/options/editor.xml"))
    glob = targets.matches(FileEvent(f"{HOME}/Widget2026.2/colors/dark.xml"))
    logical = targets.matches(FileEvent(LOGICAL))

    # then
    assert (real, glob, logical) == (True, True, False)


def test_an_unresolved_layout_adds_nothing_to_the_watch() -> None:
    # when
    real = config_dir_targets(_config(), _paths(ConfigDirMap.unresolved("gone")))

    # then
    assert (real.items, real.globs) == ((), ())


# --------------------------------------------------------------------------- doctor


def test_the_doctor_row_names_the_release_and_the_notes() -> None:
    # given
    found = ConfigDirMap.resolved(
        "Widget2026.3", ("several releases are installed (Widget2026.2); using Widget2026.3",)
    )

    # when
    row = config_dir_summary(_config(), _paths(found))

    # then
    assert row is not None and row.ok is True
    assert "widget: Widget2026.3" in row.detail and "several releases" in row.detail


def test_the_doctor_row_says_when_the_app_cannot_be_resolved() -> None:
    # when
    row = config_dir_summary(_config(), _paths(ConfigDirMap.unresolved("the app is not installed on this machine")))

    # then
    assert row is not None and row.ok is True
    assert "not installed" in row.detail and "kept as they are" in row.detail


def test_no_doctor_row_without_a_config_directory_preset() -> None:
    # when & then
    assert config_dir_summary(_config(), LivePaths(catalog=CATALOG)) is None


# --------------------------------------------------------------------------- the app's own settings sync


def test_a_folder_whose_own_sync_is_on_resolves_to_the_presets_sentence_not_a_path() -> None:
    # given
    spec = ConfigDirSpec.create(
        home=HOME, prefix="Widget", vendor="JetBrains", bundle_id="com.example.widget", sync_conflict="turn one off"
    )
    preset = Preset.create(
        name="widget",
        files=(PresetFile.create(path=spec.logical_path("options/editor.xml"), format="text"),),
        bundle_id=spec.bundle_id,
        config_dir=spec,
    )
    found = ConfigDirMap.resolved("Widget2026.3", settings_sync_enabled=True)
    paths = LivePaths(
        catalog=BuiltinCatalog.create((preset,)),
        config_views=(ConfigDirView(spec=spec, preset="widget", directory=found),),
    )

    # when
    located = paths.resolve(LOGICAL)

    # then
    assert located == Unresolved("turn one off")


def test_without_a_sentence_of_its_own_the_spec_falls_back_to_a_generic_one() -> None:
    # when
    located = _paths(ConfigDirMap.resolved("Widget2026.3", settings_sync_enabled=True)).resolve(LOGICAL)

    # then
    assert isinstance(located, Unresolved) and "settings sync is on" in located.reason


def test_the_doctor_row_fails_when_the_apps_own_sync_is_on() -> None:
    # when
    row = config_dir_summary(_config(), _paths(ConfigDirMap.resolved("Widget2026.3", settings_sync_enabled=True)))

    # then
    assert row is not None and row.ok is False
    assert "widget: Widget2026.3" in row.detail and "settings sync is on" in row.detail
