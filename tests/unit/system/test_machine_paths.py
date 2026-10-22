# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the machine-local path layout."""

from __future__ import annotations

from pathlib import Path

from syncintosh.system.paths import MachinePaths


def test_the_default_layout_is_derived_from_home(tmp_path, monkeypatch) -> None:
    # given
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    # when
    paths = MachinePaths.default()

    # then
    assert paths.config_file == tmp_path / ".config" / "syncintosh" / "config.json"
    assert paths.repo_dir == tmp_path / ".local" / "share" / "syncintosh" / "repo"


def test_xdg_overrides_are_honoured(tmp_path, monkeypatch) -> None:
    # given
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    # when
    paths = MachinePaths.default(tmp_path / "home")

    # then
    assert paths.config_dir == tmp_path / "cfg" / "syncintosh"
    assert paths.state_dir == tmp_path / "data" / "syncintosh"


def test_every_local_state_path_sits_outside_the_repo(tmp_path) -> None:
    # given — the ledger and the pending state must never live inside git (AGENTS.md §3.3)
    paths = MachinePaths.default(tmp_path / "home")

    # when
    outside = [
        path
        for path in (paths.ledger_file, paths.pending_dir, paths.lock_file, paths.scan_allow_file)
        if paths.repo_dir in path.parents or path == paths.repo_dir
    ]

    # then
    assert outside == []


def test_a_missing_home_uses_the_process_home(tmp_path, monkeypatch) -> None:
    # given — `default()` with no argument falls back to `Path.home()`
    monkeypatch.setenv("HOME", str(tmp_path / "other"))

    # when
    paths = MachinePaths.default()

    # then
    assert paths.home == Path(str(tmp_path / "other"))
