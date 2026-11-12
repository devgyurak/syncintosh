# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for rewriting `config.json`: an atomic save and a refusal to drop what it does not model (issue #68)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.errors import ConfigError
from syncintosh.system.config import (
    TOP_LEVEL_KEYS,
    config_to_mapping,
    load_config,
    load_config_for_rewrite,
    save_config,
)


def _config(path: str = ".zshrc") -> SyncConfig:
    item = LocalItem.create(path=path, preset="zsh", scope="common", format="text")
    return SyncConfig.create(remote="git@example.test:s.git", machine_id="m1", items=(item,))


def test_a_save_leaves_no_temporary_file(tmp_path) -> None:
    # given
    target = tmp_path / "cfg" / "config.json"

    # when
    save_config(_config(), target)

    # then
    assert sorted(p.name for p in target.parent.iterdir()) == ["config.json"]
    assert load_config(target) == _config()


def test_a_save_over_an_existing_file_replaces_it(tmp_path) -> None:
    # given
    target = tmp_path / "config.json"
    save_config(_config(".zshrc"), target)

    # when
    save_config(_config(".zprofile"), target)

    # then
    assert [item.path for item in load_config(target).items] == [".zprofile"]


def test_a_failed_rename_keeps_the_original_and_removes_the_temporary_file(tmp_path, monkeypatch) -> None:
    # given
    target = tmp_path / "cfg" / "config.json"
    save_config(_config(".zshrc"), target)
    original = target.read_bytes()

    def broken(self: Path, _target) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", broken)

    # when & then
    with pytest.raises(OSError, match="disk full"):
        save_config(_config(".zprofile"), target)
    assert target.read_bytes() == original
    assert sorted(p.name for p in target.parent.iterdir()) == ["config.json"]


def test_a_rewrite_refuses_a_top_level_key_it_does_not_model(tmp_path) -> None:
    # given
    target = tmp_path / "config.json"
    target.write_text(json.dumps({**config_to_mapping(_config()), "zeta": 1, "alpha": 2}), encoding="utf-8")

    # when & then — both names are reported, sorted
    with pytest.raises(ConfigError, match=r"\['alpha', 'zeta'\]"):
        load_config_for_rewrite(target)


def test_a_plain_load_still_ignores_an_unknown_top_level_key(tmp_path) -> None:
    # given
    target = tmp_path / "config.json"
    target.write_text(json.dumps({**config_to_mapping(_config()), "zeta": 1}), encoding="utf-8")

    # when
    config = load_config(target)

    # then
    assert config == _config()


def test_a_rewrite_loads_a_config_that_holds_only_known_keys(tmp_path) -> None:
    # given
    target = tmp_path / "config.json"
    save_config(_config(), target)

    # when & then
    assert load_config_for_rewrite(target) == _config()


def test_the_known_keys_cover_everything_the_writer_emits() -> None:
    # given — a config that uses every optional section
    from syncintosh.core.agent_projects import AgentLayout, AgentProject
    from syncintosh.core.config import LlmConfig, LocalProfiles
    from syncintosh.presets import BuiltinCatalog, claude_memory, vscode

    config = SyncConfig.create(
        remote="git@example.test:s.git",
        machine_id="m1",
        items=_config().items,
        llm=LlmConfig.create(base_url="http://home.test/v1", model="m"),
        profiles=(LocalProfiles.create(preset="vscode", spec=vscode.PROFILES, scope="common"),),
        agent_projects=(
            AgentProject.create(
                name="p",
                path="Develop/p",
                layouts=[AgentLayout.create(preset="claude-memory", spec=claude_memory.CLAUDE_PROJECTS)],
            ),
        ),
    )

    # when
    written = set(config_to_mapping(config))

    # then
    assert (written - TOP_LEVEL_KEYS, BuiltinCatalog.default() is not None) == (set(), True)
    assert written == TOP_LEVEL_KEYS
