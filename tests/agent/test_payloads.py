# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden payloads per harness, plus wiring guards for the tool adapters.

The Python hooks are the single source of truth; these tests feed each harness's real
payload shape so an adapter that forwards the wrong field (a patch sent as `command`, a
stop event with no path) is caught as a regression rather than discovered in production.
The adapters themselves (a JS plugin, a TS extension) cannot run here, so their wiring is
asserted structurally.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "scripts" / "agent_hooks"
ADAPTERS = Path(__file__).resolve().parent / "adapters"
NODE = shutil.which("node")


def _run(
    script: str,
    tool: str,
    payload: dict,
    *,
    args: tuple[str, ...] = (),
    root: Path | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if root is not None:
        env["SYNCINTOSH_HOOK_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(HOOKS / script), "--tool", tool, *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
        cwd=cwd,
    )


def _stage_layer_violation(root: Path) -> None:
    target = root / "src" / "syncintosh" / "core" / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("from syncintosh import crypto\n", encoding="utf-8")


def _init_repo(root: Path) -> None:
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q"],
        cwd=root,
        capture_output=True,
        check=True,
    )


def _denied(result: subprocess.CompletedProcess[str]) -> bool:
    return "permissionDecision" in result.stdout and "deny" in result.stdout


def _run_node(harness: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    assert NODE is not None  # guarded by the skipif on each caller
    return subprocess.run(
        [NODE, "--no-warnings", str(ADAPTERS / harness), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def _home_with_ssh_known_hosts(root: Path) -> Path:
    home = root / "home"
    (home / ".ssh").mkdir(parents=True)
    # known_hosts is not blocked by name, so only SECRET_DIRS containment can block it.
    (home / ".ssh" / "known_hosts").write_text("", encoding="utf-8")
    return home


def _temp_session(root: Path) -> Path:
    """A fake session dir inside `root`, so a relative path stays below the filesystem root."""
    session = root / "repo" / "src" / "syncintosh"
    session.mkdir(parents=True)
    return session


def _temp_pi_tree(root: Path) -> tuple[Path, Path]:
    """Session and HOME in one temp tree, with scripts/ reachable so findRepoRoot finds it."""
    session = _temp_session(root)
    (root / "repo" / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    return session, _home_with_ssh_known_hosts(root)


def _old_python() -> str | None:
    """Path to a system interpreter older than 3.10, or None when there is none."""
    candidate = Path("/usr/bin/python3")
    if not candidate.exists():
        return None
    probe = subprocess.run(
        [str(candidate), "-c", "import sys; print(sys.version_info >= (3, 10))"],
        capture_output=True,
        text=True,
        check=False,
    )
    return str(candidate) if probe.stdout.strip() == "False" else None


OLD_PYTHON = _old_python()
PATCH_ADDING_KEY = "*** Begin Patch\n*** Add File: /tmp/leak/key.pem\n*** Update File: README.md\n*** End Patch"
PATCH_TOUCHING_CORE = (
    "*** Begin Patch\n*** Update File: src/syncintosh/core/mod.py\n@@\n+from syncintosh import crypto\n*** End Patch"
)


def test_cursor_stop_payload_scans_changed_files(tmp_path) -> None:
    # given — Cursor's stop payload carries no path, only a hook name
    _stage_layer_violation(tmp_path)
    _init_repo(tmp_path)

    # when
    result = _run(
        "check_layering.py",
        "cursor",
        {"hook_event_name": "stop"},
        args=("--event", "stop"),
        root=tmp_path,
        cwd=tmp_path,
    )

    # then
    assert result.returncode == 0
    assert "syncintosh.crypto" in result.stdout


def test_opencode_patch_payload_is_guarded() -> None:
    # when — the plugin forwards apply_patch's `patchText` as `command`
    result = _run("guard_secrets.py", "opencode", {"tool_input": {"command": PATCH_ADDING_KEY}})

    # then
    assert result.returncode == 2


def test_codex_apply_patch_payload_is_guarded() -> None:
    # when
    result = _run("guard_secrets.py", "codex", {"tool_input": {"command": PATCH_ADDING_KEY}})

    # then
    assert _denied(result)


def test_codex_apply_patch_payload_reaches_layering(tmp_path) -> None:
    # given
    _stage_layer_violation(tmp_path)

    # when
    result = _run(
        "check_layering.py",
        "codex",
        {"tool_input": {"command": PATCH_TOUCHING_CORE}},
        root=tmp_path,
        cwd=tmp_path,
    )

    # then
    assert "block" in result.stdout
    assert "syncintosh.crypto" in result.stdout


def test_pi_shell_payload_contract() -> None:
    # when
    result = _run("guard_secrets.py", "pi", {"tool_input": {"command": "cat .env"}})

    # then
    assert result.returncode == 2


def test_cursor_stop_does_not_re_prompt_after_reentry(tmp_path) -> None:
    # given — Cursor passes loop_count once it has already re-entered the agent
    _stage_layer_violation(tmp_path)
    _init_repo(tmp_path)

    # when
    result = _run(
        "check_layering.py",
        "cursor",
        {"hook_event_name": "stop", "loop_count": 1},
        args=("--event", "stop"),
        root=tmp_path,
        cwd=tmp_path,
    )

    # then — report, but do not send a followup that would start another loop
    assert "followup_message" not in result.stdout
    assert "syncintosh.crypto" in result.stdout


def test_guard_denies_when_resolve_fails_but_the_name_is_secret() -> None:
    # when — a NUL byte makes resolve() fail; the name check must still run (fail closed)
    result = _run("guard_secrets.py", "claude", {"tool_input": {"file_path": "a\u0000b.pem"}})

    # then
    assert _denied(result)


@pytest.mark.skipif(NODE is None, reason="node is required to exercise the JS/TS adapters")
def test_pi_extension_fails_closed_when_the_hook_cannot_run(tmp_path) -> None:
    # given — a PATH without python3 makes the hook spawn fail (status null -> code 1)
    session, home = _temp_pi_tree(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()

    # when
    result = _run_node(
        "pi_harness.mjs",
        str(ROOT / ".pi" / "extensions" / "syncintosh-gates.ts"),
        str(session),
        str(home),
        env={**os.environ, "PATH": str(empty_bin)},
    )

    # then — a benign command is blocked because the guard could not run (fail closed)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["bashBenign"]["block"] is True


@pytest.mark.skipif(NODE is None, reason="node is required to exercise the JS/TS adapters")
def test_opencode_plugin_fails_closed_when_the_hook_cannot_run(tmp_path) -> None:
    # given — a non-existent interpreter makes the hook spawn fail (status null -> exit 1)
    home = _home_with_ssh_known_hosts(tmp_path)
    session = _temp_session(tmp_path)

    # when
    result = _run_node(
        "opencode_harness.mjs",
        str(ROOT / ".opencode" / "plugin" / "syncintosh-gates.js"),
        str(ROOT),
        str(session),
        str(home),
        str(tmp_path / "no-such-python"),
    )

    # then — a benign command is blocked because the guard could not run (fail closed)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["bashBenign"]["blocked"] is True


def test_cursor_stop_wires_the_event_flag() -> None:
    # given
    data = json.loads((ROOT / ".cursor" / "hooks.json").read_text(encoding="utf-8"))

    # when
    wired = any("--event stop" in entry["command"] for entry in data["hooks"]["stop"])

    # then
    assert wired is True


@pytest.mark.skipif(NODE is None, reason="node is required to exercise the JS/TS adapters")
def test_pi_extension_blocks_secrets_via_a_fake_pi(tmp_path) -> None:
    # given — session and HOME share a temp tree, so the relative path stays below root
    session, home = _temp_pi_tree(tmp_path)

    # when
    result = _run_node(
        "pi_harness.mjs",
        str(ROOT / ".pi" / "extensions" / "syncintosh-gates.ts"),
        str(session),
        str(home),
    )

    # then
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["bashEnv"]["block"] is True
    # the relative path is resolved from the session cwd; from the repo root it would escape
    assert decision["bashRelativeSecret"]["block"] is True
    assert decision["bashBenign"] is None
    # path-argument tools (read/grep) are the most common route to a secret file
    assert decision["readSecret"]["block"] is True
    assert decision["grepSecret"]["block"] is True


@pytest.mark.skipif(NODE is None, reason="node is required to exercise the JS/TS adapters")
def test_opencode_plugin_resolves_relative_paths_from_the_session_cwd(tmp_path) -> None:
    # given — the real plugin is driven with a fake Bun Shell ($)
    home = _home_with_ssh_known_hosts(tmp_path)
    session = _temp_session(tmp_path)

    # when
    result = _run_node(
        "opencode_harness.mjs",
        str(ROOT / ".opencode" / "plugin" / "syncintosh-gates.js"),
        str(ROOT),
        str(session),
        str(home),
        sys.executable,
    )

    # then
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    # cwd must be the session dir, not the worktree root, or this relative path escapes
    assert decision["bashRelativeSecret"]["blocked"] is True
    assert decision["patchKeyPem"]["blocked"] is True
    assert decision["bashBenign"]["blocked"] is False
    # `read` delivers its target in `filePath`; the plugin must map that name
    assert decision["readSecret"]["blocked"] is True


def test_codex_matcher_covers_bash_and_patch() -> None:
    # given
    data = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))

    # when
    matchers = " ".join(group["matcher"] for group in data["hooks"]["PreToolUse"])

    # then
    assert {"Bash", "exec_command", "apply_patch"} <= set(matchers.replace("|", " ").split())


@pytest.mark.skipif(OLD_PYTHON is None, reason="no interpreter older than 3.10 available")
def test_layering_hook_does_not_crash_on_old_python(tmp_path) -> None:
    # given
    _stage_layer_violation(tmp_path)
    payload = {"tool_input": {"file_path": str(tmp_path / "src" / "syncintosh" / "core" / "mod.py")}}

    # when
    result = subprocess.run(
        [str(OLD_PYTHON), str(HOOKS / "check_layering.py"), "--tool", "claude"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
    )

    # then
    assert "Traceback" not in result.stderr
