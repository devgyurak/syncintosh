# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the merge command end to end over real temp files.

This is the filesystem-level flow (temp HOME from ``conftest`` + temp files). The git-remote
half of the flow (collect → encrypt → commit → pull → decrypt → merge → apply) arrives with
the ``vcs``/``crypto`` layers; until then this test guards the command contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from syncintosh.cli import main as main_module

pytestmark = pytest.mark.integration

runner = CliRunner()


def test_merge_flow_applies_one_sided_changes_and_flags_one_conflict(tmp_path: Path) -> None:
    # given — a shared key changed on both sides, a key changed on one side, and a new key
    base = tmp_path / "base.json"
    home = tmp_path / "home.json"
    work = tmp_path / "work.json"
    base.write_text(json.dumps({"theme": "dark", "fontSize": 12}), encoding="utf-8")
    home.write_text(json.dumps({"theme": "light", "fontSize": 12, "homeOnly": True}), encoding="utf-8")
    work.write_text(json.dumps({"theme": "solarized", "fontSize": 14}), encoding="utf-8")

    # when
    result = runner.invoke(main_module.app, ["merge", str(base), str(home), str(work)])

    # then
    assert result.exit_code == 1
    merged = json.loads(result.stdout)
    assert merged == {"theme": "light", "fontSize": 14, "homeOnly": True}
