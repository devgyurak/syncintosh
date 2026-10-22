# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the `resolve` command, interactive and with `--take`, across two machines.

The CLI runs against a temp bare remote, temp HOMEs, and in-memory key rings, as `test_cli_sync`
does; no network and no real keychain (``AGENTS.md`` §5.6).
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
BASE = b"one\ntwo\nthree\n"
FROM_A = b"one\nFROM A\nthree\n"
FROM_B = b"one\nFROM B\nthree\n"


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


def _activate(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _combined(result) -> str:
    return (result.stdout or "") + (getattr(result, "stderr", "") or "")


def _conflicting_homes(tmp_path: Path, monkeypatch, memory_key_store) -> tuple[Path, Path]:
    """Two machines that synced `BASE`, then both edited the same line; B's sync stops on it."""
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    stores = {str(home_a): memory_key_store(), str(home_b): memory_key_store()}
    remote = _bare_remote(tmp_path)
    monkeypatch.setenv("SYNCINTOSH_PASSPHRASE", PASSPHRASE)
    monkeypatch.setattr(main_module, "_key_store", lambda _remote: stores[os.environ["HOME"]])

    _activate(monkeypatch, home_a)
    _write(home_a / ".zshrc", BASE)
    runner.invoke(main_module.app, ["init", "--remote", remote])
    runner.invoke(main_module.app, ["sync"])
    recovery = home_a / "syncintosh-recovery.sntr"

    _activate(monkeypatch, home_b)
    runner.invoke(main_module.app, ["init", "--remote", remote, "--import", str(recovery)])
    runner.invoke(main_module.app, ["sync"])

    _activate(monkeypatch, home_a)
    _write(home_a / ".zshrc", FROM_A)
    runner.invoke(main_module.app, ["sync"])

    _activate(monkeypatch, home_b)
    _write(home_b / ".zshrc", FROM_B)
    stopped = runner.invoke(main_module.app, ["sync"])
    assert stopped.exit_code == 1, _combined(stopped)
    return home_a, home_b


def test_interactive_resolve_publishes_the_chosen_side_after_approval(tmp_path, monkeypatch, memory_key_store) -> None:
    # given — B is stopped on a conflict
    _, home_b = _conflicting_homes(tmp_path, monkeypatch, memory_key_store)

    # when — B chooses the remote side and approves the publish
    result = runner.invoke(main_module.app, ["resolve"], input="remote\ny\n")

    # then — the conflict is shown with the file's text, settled, and B's file follows
    assert result.exit_code == 0, _combined(result)
    assert "FROM A" in result.output
    assert (home_b / ".zshrc").read_bytes() == FROM_A


def test_a_declined_approval_writes_nothing_and_keeps_the_pending_state(
    tmp_path, monkeypatch, memory_key_store
) -> None:
    # given
    _, home_b = _conflicting_homes(tmp_path, monkeypatch, memory_key_store)

    # when — the choice is made, then the publish is declined
    result = runner.invoke(main_module.app, ["resolve"], input="remote\nn\n")

    # then — exit 1, B's live file is untouched, and the conflict is still pending
    assert result.exit_code == 1, _combined(result)
    assert (home_b / ".zshrc").read_bytes() == FROM_B
    assert (home_b / ".local" / "share" / "syncintosh" / "pending" / "state.json").is_file()


def test_an_answer_outside_the_options_is_asked_again(tmp_path, monkeypatch, memory_key_store) -> None:
    # given
    _, home_b = _conflicting_homes(tmp_path, monkeypatch, memory_key_store)

    # when — a wrong word first, then local, then approval
    result = runner.invoke(main_module.app, ["resolve"], input="bogus\nlocal\ny\n")

    # then — the wrong word is refused, B's own edit is published
    assert result.exit_code == 0, _combined(result)
    assert "choose again" in result.output
    assert (home_b / ".zshrc").read_bytes() == FROM_B


def test_take_with_yes_settles_without_any_prompt(tmp_path, monkeypatch, memory_key_store) -> None:
    # given
    _, home_b = _conflicting_homes(tmp_path, monkeypatch, memory_key_store)

    # when — no input is offered, so any prompt would fail the run
    result = runner.invoke(main_module.app, ["resolve", "--take", "remote", "--yes"])

    # then
    assert result.exit_code == 0, _combined(result)
    assert (home_b / ".zshrc").read_bytes() == FROM_A


def test_take_without_yes_still_asks_before_publishing(tmp_path, monkeypatch, memory_key_store) -> None:
    # given
    _, home_b = _conflicting_homes(tmp_path, monkeypatch, memory_key_store)

    # when — the approval is declined
    result = runner.invoke(main_module.app, ["resolve", "--take", "remote"], input="n\n")

    # then
    assert result.exit_code == 1, _combined(result)
    assert (home_b / ".zshrc").read_bytes() == FROM_B


def test_an_unknown_take_side_is_refused(tmp_path, monkeypatch, memory_key_store) -> None:
    # given
    _conflicting_homes(tmp_path, monkeypatch, memory_key_store)

    # when & then
    result = runner.invoke(main_module.app, ["resolve", "--take", "both"])
    assert result.exit_code != 0
