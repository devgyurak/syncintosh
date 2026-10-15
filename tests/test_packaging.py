# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-cutting packaging invariants (``AGENTS.md`` §4, §7).

These tests encode rules that would otherwise live only in prose: the SPDX header on every
source file, the Python floor, the coverage floor, the git-tag version, and the lazy import
of heavy dependencies.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "syncintosh"
SPDX_HEADER = "# SPDX-License-Identifier: AGPL-3.0-or-later"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_source_file_has_spdx_header() -> None:
    # when
    missing = [
        path.relative_to(ROOT).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if not path.read_text(encoding="utf-8").startswith(SPDX_HEADER)
    ]

    # then
    assert missing == []


def test_coverage_floor_is_90() -> None:
    # given
    addopts = _pyproject()["tool"]["pytest"]["ini_options"]["addopts"]

    # when
    gated = "--cov-fail-under=90" in addopts

    # then
    assert gated is True


def test_supported_python_floor() -> None:
    # given
    project = _pyproject()["project"]

    # when
    floor = project["requires-python"]

    # then
    assert floor == ">=3.12"


def test_version_comes_from_the_git_tag() -> None:
    # given
    project = _pyproject()["project"]

    # when
    dynamic = project.get("dynamic", [])

    # then
    assert "version" in dynamic
    assert "version" not in project


def test_cli_does_not_import_heavy_dependencies() -> None:
    # given — run in a fresh interpreter so the result cannot depend on test order
    probe = (
        "import sys; import syncintosh.cli.main; "
        "print('|'.join(sorted({'cryptography', 'lxml', 'openai'} & set(sys.modules))))"
    )

    # when
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    # then
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
