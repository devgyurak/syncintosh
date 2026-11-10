# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests: the coding agents' own state is refused even when a user's config names it (issue #56, decision D11)."""

from __future__ import annotations

import pytest

from syncintosh.system.deny_list import SystemDenyList

AGENT_STATE = [
    ".claude.json",
    ".claude.json.backup",
    ".claude/.credentials.json",
    ".claude/session-env/abc/env",
    ".claude/shell-snapshots/snapshot-zsh.sh",
    ".claude/file-history/abc/file",
    ".claude/paste-cache/x",
    ".claude/history.jsonl",
    ".claude/cache/x",
    ".claude/daemon.sock",
    ".claude/daemon/state.json",
    ".claude/sessions/123.json",
    ".codex/auth.json",
    ".codex/installation_id",
    ".codex/history.jsonl",
    ".codex/shell_snapshots/x",
    ".codex/sqlite/main.db",
    ".codex/logs_1.sqlite",
    ".codex/logs_1.sqlite-wal",
    ".codex/state_5.sqlite",
    ".codex/state_5.sqlite-shm",
    ".codex/thread-writer-locks/0197.lock",
    ".codex/thread-writer-locks/.coordination.lock",
    ".codex/app-server-1234",
    ".codex/ipc/socket",
]


@pytest.mark.parametrize("relative", AGENT_STATE)
def test_agent_state_is_refused_even_when_a_config_names_it(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when
    denied = SystemDenyList().is_denied(str(home), relative)

    # then
    assert denied is True


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/projects/-Users-a-src-x/memory/MEMORY.md",
        ".claude/projects/-Users-a-src-x/0197.jsonl",
        ".claude/settings.json",
        ".codex/sessions/2026/11/09/rollout-2026-11-09T10-00-00-0197.jsonl",
        ".codex/config.toml",
        ".claudeX/notes.md",
    ],
)
def test_what_a_preset_may_reach_is_not_denied(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when
    denied = SystemDenyList().is_denied(str(home), relative)

    # then
    assert denied is False


@pytest.mark.parametrize("relative", [".CLAUDE.JSON", ".Claude/Sessions/1.json", ".CODEX/State_5.SQLite"])
def test_a_case_variant_of_agent_state_is_refused(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when
    denied = SystemDenyList().is_denied(str(home), relative)

    # then
    assert denied is True
