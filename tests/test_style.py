# SPDX-License-Identifier: AGPL-3.0-or-later
"""Enforce the test rules from ``AGENTS.md`` §5 by walking the AST.

These checks are the structural half of the rules — a test written by any tool, not only one
whose edit hook fired, must still obey them.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Control-flow *statements* are banned inside a test. A comprehension is an expression and
# therefore allowed; `ast.walk` sees it as `ast.comprehension`, not `ast.For`.
CONTROL_FLOW = (ast.If, ast.For, ast.AsyncFor, ast.While)


def _functions() -> list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found: list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend(
            (path, node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
    return found


def _control_flow_lines(node: ast.AST) -> list[int]:
    return [child.lineno for child in ast.walk(node) if isinstance(child, CONTROL_FLOW)]


def _has_given_when_then(path: Path, node: ast.AST) -> bool:
    segment = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
    when = segment.find("# when")
    # `# when & then` marks both boundaries in one comment (act and assert are one unit).
    combined = segment.find("# when & then")
    then = combined if combined != -1 else segment.find("# then")
    if when == -1 or then == -1 or when > then:
        return False
    given = segment.find("# given")
    return given == -1 or given < when


def test_no_control_flow_statements() -> None:
    # when
    offenders = [f"{path.name}:{node.lineno}" for path, node in _functions() if _control_flow_lines(node)]

    # then
    assert offenders == []


def test_every_test_has_given_when_then() -> None:
    # when
    missing = [f"{path.name}:{node.lineno}" for path, node in _functions() if not _has_given_when_then(path, node)]

    # then
    assert missing == []
