# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the `init` and `sync` commands across two machines.

The CLI is wired to real infrastructure (a temp bare remote, temp HOMEs, an in-memory key ring)
so the commands are exercised end to end, but no network and no real keychain (``AGENTS.md`` §5.6).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from syncintosh.cli import main as main_module

pytestmark = pytest.mark.integration

runner = CliRunner()
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


def _activate(monkeypatch, home: Path) -> None:
    """Point every machine-local path at `home` for the next CLI invocation."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _combined(result) -> str:
    return (result.stdout or "") + (getattr(result, "stderr", "") or "")


def test_init_then_sync_on_a_second_machine(tmp_path, monkeypatch, memory_key_store) -> None:
    # given — two homes with their own key rings, wired in place of the keychain
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    stores = {str(home_a): memory_key_store(), str(home_b): memory_key_store()}
    remote = _bare_remote(tmp_path)
    monkeypatch.setenv("SYNCINTOSH_PASSPHRASE", PASSPHRASE)
    monkeypatch.setattr(main_module, "_key_store", lambda _remote: stores[os.environ["HOME"]])

    # when — A initializes and publishes its file
    _activate(monkeypatch, home_a)
    _write(home_a / ".zshrc", b"from A\n")
    init_a = runner.invoke(main_module.app, ["init", "--remote", remote, "--path", ".zshrc"])
    sync_a = runner.invoke(main_module.app, ["sync"])

    # then
    assert init_a.exit_code == 0, _combined(init_a)
    assert sync_a.exit_code == 0, _combined(sync_a)
    recovery = home_a / "syncintosh-recovery.sntr"
    assert recovery.is_file()

    # when — B initializes from A's recovery export and syncs
    _activate(monkeypatch, home_b)
    init_b = runner.invoke(main_module.app, ["init", "--remote", remote, "--import", str(recovery)])
    sync_b = runner.invoke(main_module.app, ["sync"])

    # then — B's file matches A's
    assert init_b.exit_code == 0, _combined(init_b)
    assert sync_b.exit_code == 0, _combined(sync_b)
    assert (home_b / ".zshrc").read_bytes() == b"from A\n"


def test_a_first_connect_exits_1_until_a_side_is_adopted(tmp_path, monkeypatch, memory_key_store) -> None:
    # given — A published, and B already has a different file
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    stores = {str(home_a): memory_key_store(), str(home_b): memory_key_store()}
    remote = _bare_remote(tmp_path)
    monkeypatch.setenv("SYNCINTOSH_PASSPHRASE", PASSPHRASE)
    monkeypatch.setattr(main_module, "_key_store", lambda _remote: stores[os.environ["HOME"]])
    _activate(monkeypatch, home_a)
    _write(home_a / ".zshrc", b"from A\n")
    runner.invoke(main_module.app, ["init", "--remote", remote])
    runner.invoke(main_module.app, ["sync"])
    recovery = home_a / "syncintosh-recovery.sntr"
    _activate(monkeypatch, home_b)
    _write(home_b / ".zshrc", b"from B\n")
    runner.invoke(main_module.app, ["init", "--remote", remote, "--import", str(recovery)])

    # when — no choice made yet, then B takes the repo's side
    unresolved = runner.invoke(main_module.app, ["sync"])
    adopted = runner.invoke(main_module.app, ["sync", "--adopt", "remote"])

    # then
    assert unresolved.exit_code == 1
    assert ".zshrc" in _combined(unresolved)
    assert adopted.exit_code == 0, _combined(adopted)
    assert (home_b / ".zshrc").read_bytes() == b"from A\n"
