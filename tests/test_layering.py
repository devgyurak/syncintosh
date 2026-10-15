# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI gate for layer boundaries under ``src/syncintosh`` (``AGENTS.md`` §2, §7).

The decision logic is not reimplemented here — it is imported from the hook
(``scripts/agent_hooks/check_layering.py``) so the edit-hook and the CI gate can never
drift. This runs the same ``check_file`` the hook runs, over the whole tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "syncintosh"
HOOKS = ROOT / "scripts" / "agent_hooks"
sys.path.insert(0, str(HOOKS))


def _violations_for(path: Path) -> list[str]:
    import check_layering  # ty: ignore[unresolved-import]  # hook dir is on sys.path above

    return check_layering.check_file(path)


def test_no_layer_or_stdlib_violations() -> None:
    # when
    violations = [line for path in sorted(SRC.rglob("*.py")) for line in _violations_for(path)]

    # then
    assert violations == []
