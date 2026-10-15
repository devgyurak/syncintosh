#!/usr/bin/env python3
"""Post-hook that applies `ruff check --fix` + `ruff format` to just-edited Python files.

Follows pyproject.toml (line-length 120). The repository is kept ruff-clean, so formatting
an edited file leaves a diff only where the file actually changed. ruff availability is
optional: when it is missing (or the project is not yet set up with uv) the hook is a
silent no-op.

Usage:
    format_python.py --tool claude|codex|cursor|opencode|pi
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hookio import (
    emit_allow,
    emit_note,
    extract_paths,
    parse_args,
    read_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Import order matters: lint autofix, then format.
STEPS: tuple[tuple[str, list[str]], ...] = (
    ("ruff check", ["check", "--fix", "--quiet"]),
    ("ruff format", ["format", "--quiet"]),
)


def _prefix() -> list[str]:
    """How to invoke ruff: prefer `uv run`, else a PATH executable."""
    if shutil.which("uv") and (REPO_ROOT / "pyproject.toml").exists():
        return ["uv", "run", "--no-sync"]
    return []


def _run(tool_name: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not _prefix() and not shutil.which(tool_name):
        return None
    try:
        return subprocess.run(
            [*_prefix(), tool_name, *args],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _eligible(raw: str) -> str | None:
    """Return the repo-relative path when the file is a formattable target, else None."""
    path = Path(raw).expanduser()
    if path.suffix != ".py" or not path.exists():
        return None
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return None  # outside the repository


def main() -> None:
    tool = parse_args()
    payload = read_payload()

    targets = [rel for rel in (_eligible(p) for p in extract_paths(payload)) if rel]
    if not targets:
        emit_allow(tool)

    applied: list[str] = []
    for label, args in STEPS:
        result = _run("ruff", [*args, *targets])
        # `uv run` fails when the tool is absent — treat that as a skip.
        if result is not None and (result.returncode == 0 or result.stdout):
            applied.append(label)

    if not applied:
        emit_allow(tool)  # formatter not installed — pass quietly

    names = ", ".join(Path(t).name for t in targets)
    message = f"{' + '.join(applied)} applied: {names}"

    # Surface errors ruff could not auto-fix, so the agent sees them now instead of
    # discovering them at the `verify` gate. Do not block — the agent is mid-edit.
    remaining = _run("ruff", ["check", "--quiet", "--output-format", "concise", *targets])
    if remaining is not None and remaining.returncode != 0 and remaining.stdout.strip():
        message += "\nruff issues that need manual fixes:\n" + remaining.stdout.strip()

    emit_note(tool, message)


if __name__ == "__main__":
    main()
