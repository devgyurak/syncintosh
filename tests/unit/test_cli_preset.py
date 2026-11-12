# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for `syncintosh preset add`: the wiring, the lock, and the report (issue #68)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from syncintosh.cli import main as main_module
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.setup import new_machine_config
from syncintosh.system.config import load_config, save_config

runner = CliRunner()


@pytest.fixture
def config_file(tmp_path, monkeypatch) -> Path:
    """A temp HOME whose config tracks only `zsh`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    path = tmp_path / ".config" / "syncintosh" / "config.json"
    save_config(
        new_machine_config(
            remote="git@example.test:s.git", machine_id="m1", preset_names=("zsh",), catalog=BuiltinCatalog.default()
        ),
        path,
    )
    return path


def _output(result) -> str:
    return (result.stdout or "") + (getattr(result, "stderr", "") or "")


def test_preset_add_tracks_the_preset_and_reports_what_changed(config_file) -> None:
    # when
    result = runner.invoke(main_module.app, ["preset", "add", "vscode"])

    # then
    assert result.exit_code == 0, _output(result)
    assert [item.preset for item in load_config(config_file).items].count("vscode") > 0
    assert [entry.names for entry in load_config(config_file).profiles] == ["*"]
    assert "  added: Library/Application Support/Code/User/settings.json" in _output(result)
    assert "profiles: tracking all" in _output(result)


def test_preset_add_says_how_to_reload_the_daemon_and_about_adopt(config_file) -> None:
    # when
    result = runner.invoke(main_module.app, ["preset", "add", "vscode"])

    # then
    assert "syncintosh daemon install" in _output(result)
    assert "--adopt local|remote" in _output(result)


def test_preset_add_reports_a_repeat_as_already_tracked(config_file) -> None:
    # given
    runner.invoke(main_module.app, ["preset", "add", "vscode"])
    before = config_file.read_bytes()

    # when
    result = runner.invoke(main_module.app, ["preset", "add", "vscode"])

    # then
    assert result.exit_code == 0
    assert "already tracked: Library/Application Support/Code/User/settings.json" in _output(result)
    assert "added:" not in _output(result)
    assert config_file.read_bytes() == before


def test_preset_add_passes_the_profile_names_through(config_file) -> None:
    # when
    result = runner.invoke(main_module.app, ["preset", "add", "vscode", "--profile", "Work", "--profile", "Play"])

    # then
    assert result.exit_code == 0, _output(result)
    assert [entry.names for entry in load_config(config_file).profiles] == [("Work", "Play")]
    assert "profiles: tracking Work, Play" in _output(result)


@pytest.mark.parametrize(
    "args, message",
    [
        (["nope"], "unknown preset"),
        (["claude-memory"], "per-project"),
        (["zsh", "--profile", "Work"], "no profiles"),
    ],
)
def test_preset_add_refuses_and_writes_nothing(config_file, args, message) -> None:
    # given
    before = config_file.read_bytes()

    # when
    result = runner.invoke(main_module.app, ["preset", "add", *args])

    # then
    assert result.exit_code == 1
    assert message in _output(result)
    assert config_file.read_bytes() == before


def test_preset_add_refuses_a_config_with_a_key_it_would_drop(config_file) -> None:
    # given
    mapping = json.loads(config_file.read_text(encoding="utf-8"))
    config_file.write_text(json.dumps({**mapping, "from_the_future": 1}), encoding="utf-8")
    before = config_file.read_bytes()

    # when
    result = runner.invoke(main_module.app, ["preset", "add", "vscode"])

    # then
    assert result.exit_code == 1
    assert "from_the_future" in _output(result)
    assert config_file.read_bytes() == before


class _RecordingLock:
    events: list[str] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def acquire(self) -> None:
        self.events.append("acquire")

    def release(self) -> None:
        self.events.append("release")


@pytest.mark.parametrize("args, exit_code", [(["vscode"], 0), (["nope"], 1)], ids=["after a save", "after a refusal"])
def test_preset_add_holds_the_sync_lock_around_the_rewrite_and_releases_it(
    config_file, monkeypatch, args, exit_code
) -> None:
    # given
    _RecordingLock.events = []
    monkeypatch.setattr("syncintosh.system.lock.FileLock", _RecordingLock)

    # when
    result = runner.invoke(main_module.app, ["preset", "add", *args])

    # then
    assert result.exit_code == exit_code
    assert _RecordingLock.events == ["acquire", "release"]


def test_preset_add_without_a_name_prints_help() -> None:
    # when
    result = runner.invoke(main_module.app, ["preset"])

    # then
    assert "add" in _output(result)
