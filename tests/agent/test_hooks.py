# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the agent-hook self-test from the default pytest run.

``scripts/agent_hooks/selftest.py`` holds the hook scenarios (the single source of truth).
Running it here means the hooks are checked by ``uv run pytest`` itself, not only by the
`agents` workflow (``AGENTS.md`` §7: local hooks are best-effort, so the gate must live in
tests too).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "scripts" / "agent_hooks"
SELFTEST = HOOKS / "selftest.py"


def test_hook_selftest_passes() -> None:
    # when
    result = subprocess.run(
        [sys.executable, str(SELFTEST)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    # then
    assert result.returncode == 0, result.stderr


def test_hook_entry_scripts_are_executable_and_have_a_shebang() -> None:
    # when — the OpenCode plugin runs each entry script directly (${script}), so it needs both
    # a shebang and the executable bit. `_`-prefixed helpers are imported, not executed.
    problems = [
        path.name
        for path in sorted(HOOKS.glob("*.py"))
        if not path.name.startswith("_")
        and (not path.read_text(encoding="utf-8").startswith("#!") or not os.access(path, os.X_OK))
    ]

    # then
    assert problems == []
