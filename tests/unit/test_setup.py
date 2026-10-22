# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `init` configuration use-case."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from syncintosh.core.errors import ConfigError
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.presets import BuiltinCatalog, zsh
from syncintosh.services.setup import mint_machine_id, new_machine_config


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
