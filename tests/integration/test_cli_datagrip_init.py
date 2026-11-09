# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: `init --preset datagrip` writes a config that tracks the preset's logical paths.

The CLI runs against a temp bare remote, a temp HOME and an in-memory key ring; no network, no keychain and no
Spotlight query (``AGENTS.md`` §5.6).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from syncintosh.cli import main as main_module
from syncintosh.presets import datagrip
from syncintosh.system.config import load_config

pytestmark = pytest.mark.integration

runner = CliRunner()


def _init(tmp_path: Path, monkeypatch, memory_key_store, *args: str):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    store = memory_key_store()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("SYNCINTOSH_PASSPHRASE", "a recovery passphrase")
    monkeypatch.setattr(main_module, "_key_store", lambda _remote: store)
    result = runner.invoke(main_module.app, ["init", "--remote", str(remote), *args])
    return result, home


def test_init_with_the_datagrip_preset_tracks_its_files_by_logical_path(
    tmp_path, monkeypatch, memory_key_store
) -> None:
    # when
    result, home = _init(tmp_path, monkeypatch, memory_key_store, "--preset", "datagrip")

    # then
    assert result.exit_code == 0, result.output
    config = load_config(home / ".config" / "syncintosh" / "config.json")
    expected = [file.path for file in datagrip.DATAGRIP.files]
    assert sorted([*(item.path for item in config.items), *(entry.glob.path for entry in config.globs)]) == sorted(
        expected
    )
    assert {item.preset for item in config.items} == {"datagrip"}
    assert [path for path in expected if "2026" in path] == []


def test_init_still_refuses_an_unknown_preset(tmp_path, monkeypatch, memory_key_store) -> None:
    # when
    result, _ = _init(tmp_path, monkeypatch, memory_key_store, "--preset", "datagrip-nope")

    # then
    assert result.exit_code == 1
    assert "unknown preset" in result.output
