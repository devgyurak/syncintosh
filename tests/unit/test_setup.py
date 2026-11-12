# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `init` configuration use-case."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

import pytest

from syncintosh.core.config import LocalGlob, LocalItem, LocalProfiles, SyncConfig
from syncintosh.core.errors import ConfigError
from syncintosh.core.manifest import FileFormat
from syncintosh.core.models import Scope
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.presets import BuiltinCatalog, vscode, zsh
from syncintosh.services.setup import add_preset, mint_machine_id, new_machine_config


class _StubCatalog:
    """A minimal `PresetCatalog` over an explicit preset list, for the error paths."""

    def __init__(self, presets: Iterable[Preset]) -> None:
        self._presets = tuple(presets)

    def get(self, name: str) -> Preset | None:
        return next((preset for preset in self._presets if preset.name == name), None)

    def policy_for(self, path: str) -> PresetFile | None:
        return None

    def knows(self, preset: str, path: str) -> bool:
        return True

    def ignore_list_files(self) -> tuple:
        return ()

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        return None

    def preset_for(self, path: str) -> Preset | None:
        return next(
            (preset for preset in self._presets if any(file.path == path for file in preset.files)),
            None,
        )


def test_mint_machine_id_is_a_file_id() -> None:
    # given
    class Source:
        def new_file_id(self) -> str:
            return "a" * 32

    # when
    machine_id = mint_machine_id(Source())

    # then
    assert machine_id == "a" * 32


def test_a_named_preset_expands_to_every_file_it_owns() -> None:
    # when
    config = new_machine_config(
        remote="git@example.test:settings.git",
        machine_id="machine-a",
        preset_names=("zsh",),
        catalog=BuiltinCatalog.default(),
    )

    # then
    assert [item.path for item in config.items] == list(zsh.STARTUP_FILES)
    assert {item.preset for item in config.items} == {"zsh"}
    assert {item.format for item in config.items} == {"text"}
    assert {item.scope for item in config.items} == {"common"}


def test_an_unknown_preset_name_is_refused() -> None:
    # when & then — tracking nothing looks exactly like a successful setup
    with pytest.raises(ConfigError):
        new_machine_config(
            remote="git@example.test:settings.git",
            machine_id="machine-a",
            preset_names=("nonesuch",),
            catalog=BuiltinCatalog.default(),
        )


def test_a_repeated_preset_name_is_deduplicated() -> None:
    # given — `--preset zsh --preset zsh` names one preset, not two
    catalog = _StubCatalog((zsh.ZSH,))

    # when
    config = new_machine_config(
        remote="git@example.test:settings.git",
        machine_id="machine-a",
        preset_names=("zsh", "zsh"),
        catalog=catalog,
    )

    # then — expanding it twice would collide on every sync, so the repeat is dropped
    assert [item.path for item in config.items] == list(zsh.STARTUP_FILES)


def test_a_preset_glob_becomes_a_config_glob_not_an_item() -> None:
    # given — the VS Code preset owns two exact files and two snippet globs
    # when
    config = new_machine_config(
        remote="git@example.com:me/settings.git",
        machine_id="mac-studio",
        preset_names=("vscode",),
        catalog=BuiltinCatalog.default(),
    )

    # then — which snippet files exist is per-machine, so only the pattern is written
    assert [item.path for item in config.items] == [
        "Library/Application Support/Code/User/settings.json",
        "Library/Application Support/Code/User/keybindings.json",
    ]
    assert [entry.glob.path for entry in config.globs] == [
        "Library/Application Support/Code/User/snippets/*.json",
        "Library/Application Support/Code/User/snippets/*.code-snippets",
    ]
    assert {entry.preset for entry in config.globs} == {"vscode"}


# --------------------------------------------------------------------------- add_preset (issue #68)

REMOTE = "git@example.test:settings.git"
MACHINE = "m" * 32
CATALOG = BuiltinCatalog.default()


def _config(*presets: str, profiles: tuple[LocalProfiles, ...] = (), extra: tuple[LocalItem, ...] = ()) -> SyncConfig:
    base = new_machine_config(remote=REMOTE, machine_id=MACHINE, preset_names=presets, catalog=CATALOG)
    return SyncConfig.create(
        remote=REMOTE,
        machine_id=MACHINE,
        items=(*base.items, *extra),
        globs=base.globs,
        profiles=(*base.profiles, *profiles),
    )


def _profiles(
    names: Literal["*"] | tuple[str, ...], *, scope: Scope = "common", machine: str | None = None
) -> LocalProfiles:
    return LocalProfiles.create(
        preset="vscode",
        spec=vscode.PROFILES,
        scope=scope,
        machine=machine,
        names=names,
    )


def _vscode_paths() -> list[str]:
    return [file.path for file in vscode.VSCODE.files]


def test_adding_vscode_to_a_zsh_config_adds_its_items_globs_and_all_profiles() -> None:
    # given
    config = _config("zsh")

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG)

    # then
    assert sorted(addition.added) == sorted(_vscode_paths())
    assert addition.skipped == ()
    assert (addition.profiles, addition.profile_names) == ("added", ())
    assert addition.config == new_machine_config(
        remote=REMOTE, machine_id=MACHINE, preset_names=("zsh", "vscode"), catalog=CATALOG
    )


def test_adding_the_same_preset_again_changes_nothing() -> None:
    # given
    config = _config("zsh", "vscode")

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG)

    # then
    assert addition.config == config
    assert (addition.added, addition.profiles) == ((), "unchanged")
    assert sorted(addition.skipped) == sorted(_vscode_paths())


def test_only_the_files_not_yet_tracked_are_added() -> None:
    # given — one vscode item already tracked
    first = vscode.VSCODE.files[0]
    config = _config(
        "zsh", extra=(LocalItem.create(path=first.path, preset="vscode", scope="common", format=first.format),)
    )

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG)

    # then
    assert addition.skipped == (first.path,)
    assert sorted(addition.added) == sorted(path for path in _vscode_paths() if path != first.path)


@pytest.mark.parametrize(
    "preset, fmt",
    [("zsh", "jsonc"), ("vscode", "text")],
    ids=["other preset", "other format"],
)
def test_a_path_tracked_as_another_preset_or_format_is_refused(preset: str, fmt: FileFormat) -> None:
    # given
    first = next(file for file in vscode.VSCODE.files if not file.is_glob)
    config = _config(
        "zsh", extra=(LocalItem.create(path=first.path.upper(), preset=preset, scope="common", format=fmt),)
    )

    # when & then
    with pytest.raises(ConfigError, match="already tracked"):
        add_preset(config, name="vscode", catalog=CATALOG)


def test_a_glob_tracked_as_another_preset_is_refused() -> None:
    # given
    pattern = next(file for file in vscode.VSCODE.files if file.is_glob)
    other = SyncConfig.create(
        remote=REMOTE,
        machine_id=MACHINE,
        items=(),
        globs=(LocalGlob.create(path=pattern.path, preset="zsh", scope="common", format=pattern.format),),
    )

    # when & then
    with pytest.raises(ConfigError, match="already tracked"):
        add_preset(other, name="vscode", catalog=CATALOG)


@pytest.mark.parametrize(
    "name, message",
    [
        ("nope", "unknown preset"),
        ("claude-memory", "per-project"),
        ("codex-sessions", "per-project"),
        ("claude-sessions", "per-project"),
    ],
)
def test_an_unknown_or_per_project_preset_is_refused(name: str, message: str) -> None:
    # when & then
    with pytest.raises(ConfigError, match=message):
        add_preset(_config("zsh"), name=name, catalog=CATALOG)


@pytest.mark.parametrize(
    "existing, asked, expected_names, outcome, gained",
    [
        (None, None, "*", "added", ()),
        (None, ("Work", "Play"), ("Work", "Play"), "added", ("Work", "Play")),
        ("*", None, "*", "unchanged", ()),
        ("*", ("Work",), "*", "unchanged", ()),
        (("Work",), ("Play",), ("Work", "Play"), "merged", ("Play",)),
        (("Work",), None, ("Work",), "kept", ()),
        (("Work",), ("work", "Play", "PLAY"), ("Work", "Play"), "merged", ("Play",)),
        (("Work",), ("WORK",), ("Work",), "unchanged", ()),
    ],
    ids=[
        "no entry, no names",
        "no entry, names",
        "star stays",
        "star ignores names",
        "union",
        "list kept",
        "case-only duplicates fold",
        "nothing new",
    ],
)
def test_profiles_merge_by_the_union_rule_and_never_widen(existing, asked, expected_names, outcome, gained) -> None:
    # given
    config = _swap_profiles(_config("zsh", "vscode"), None if existing is None else _profiles(existing))

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG, profile_names=asked)

    # then
    assert [entry.names for entry in addition.config.profiles] == [expected_names]
    assert (addition.profiles, addition.profile_names) == (outcome, gained)


def _swap_profiles(config: SyncConfig, entry: LocalProfiles | None) -> SyncConfig:
    return SyncConfig.create(
        remote=config.remote,
        machine_id=config.machine_id,
        items=config.items,
        globs=config.globs,
        profiles=() if entry is None else (entry,),
    )


def test_an_existing_entry_keeps_its_scope_and_machine_when_names_are_merged() -> None:
    # given
    config = _swap_profiles(_config("zsh", "vscode"), _profiles(("Work",), scope="machine", machine=MACHINE))

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG, profile_names=("Play",))

    # then
    assert [(e.scope, e.machine, e.names) for e in addition.config.profiles] == [("machine", MACHINE, ("Work", "Play"))]


def test_an_existing_machine_scope_item_keeps_its_scope() -> None:
    # given
    first = next(file for file in vscode.VSCODE.files if not file.is_glob)
    config = _config(
        "zsh",
        extra=(
            LocalItem.create(path=first.path, preset="vscode", scope="machine", machine=MACHINE, format=first.format),
        ),
    )

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG)

    # then
    kept = [item for item in addition.config.items if item.path == first.path]
    assert [(item.scope, item.machine) for item in kept] == [("machine", MACHINE)]


def test_other_entries_and_their_order_are_carried_over() -> None:
    # given
    config = _config("zsh")

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG)

    # then
    assert addition.config.items[: len(config.items)] == config.items
    assert addition.config.llm == config.llm


@pytest.mark.parametrize(
    "name, names",
    [("zsh", ("Work",)), ("vscode", ())],
    ids=["preset without profiles", "empty names"],
)
def test_profile_names_are_refused_where_they_do_not_apply(name: str, names: tuple[str, ...]) -> None:
    # when & then
    with pytest.raises(ConfigError):
        add_preset(_config("zsh", "vscode"), name=name, catalog=CATALOG, profile_names=names)


def test_a_repeated_name_for_a_new_entry_is_one_name() -> None:
    # given
    config = _swap_profiles(_config("zsh", "vscode"), None)

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG, profile_names=("Work", "work"))

    # then
    assert [entry.names for entry in addition.config.profiles] == [("Work",)]


@pytest.mark.parametrize(
    "existing, asked, changed",
    [(None, None, True), ("*", None, False), (("Work",), None, False), (("Work",), ("Play",), True)],
    ids=["new entry", "star", "kept", "merged"],
)
def test_changed_is_true_only_when_there_is_something_to_save(existing, asked, changed) -> None:
    # given — every vscode file is already tracked, so only the profiles entry can change
    config = _swap_profiles(_config("zsh", "vscode"), None if existing is None else _profiles(existing))

    # when
    addition = add_preset(config, name="vscode", catalog=CATALOG, profile_names=asked)

    # then
    assert addition.changed is changed


def test_the_addition_is_frozen() -> None:
    # given
    addition = add_preset(_config("zsh"), name="vscode", catalog=CATALOG)

    # when & then
    with pytest.raises(AttributeError):
        addition.added = ()  # ty: ignore[invalid-assignment]  # the point of the test
