# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent-config integrity for the security-critical envelope layout (``AGENTS.md`` §3.1).

Check 11 of ``scripts/check_agent_config.py`` asserts that the envelope layout printed in
``AGENTS.md`` and in every ``crypto-auditor`` body are the same byte layout. The decision
lives in the script; the tests here only call it, so the two gates cannot drift
(``AGENTS.md`` §7).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _real_auditor_body() -> str:
    import check_agent_config  # ty: ignore[unresolved-import]  # scripts/ is on sys.path above

    _, body = check_agent_config.parse_frontmatter(
        (ROOT / ".claude" / "agents" / "crypto-auditor.md").read_text(encoding="utf-8")
    )
    return body


def test_real_canon_and_auditor_agree_on_the_envelope_layout() -> None:
    import check_agent_config  # ty: ignore[unresolved-import]  # scripts/ is on sys.path above

    # given
    agents_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    # when
    message = check_agent_config.envelope_layout_mismatch(agents_text, _real_auditor_body())

    # then
    assert message is None


def test_a_drifted_auditor_layout_is_rejected() -> None:
    import check_agent_config  # ty: ignore[unresolved-import]  # scripts/ is on sys.path above

    # given — an auditor that describes a different envelope (a 32-byte tag)
    drifted = _real_auditor_body().replace("tag(16B)", "tag(32B)")

    # when
    message = check_agent_config.envelope_layout_mismatch("x `version(1B) | tag(16B)` y", drifted)

    # then
    assert message is not None
    assert "envelope layout differs" in message


def test_the_agents_documented_layout_is_really_compared() -> None:
    import check_agent_config  # ty: ignore[unresolved-import]  # scripts/ is on sys.path above

    # given
    auditor = _real_auditor_body()

    # when — a canon with no layout line
    message = check_agent_config.envelope_layout_mismatch("no layout here", auditor)

    # then
    assert message is not None
    assert "exactly once" in message


def test_check_agent_config_passes_on_this_checkout() -> None:
    # when
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_agent_config.py")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    # then
    assert result.returncode == 0, result.stderr
