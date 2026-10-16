# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the CLI: parsing, delegation, rendering, and exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from syncintosh.cli import main as main_module

runner = CliRunner()


def _write(directory: Path, name: str, payload: object) -> str:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _combined(result) -> str:
    return (result.stdout or "") + (getattr(result, "stderr", "") or "")


def test_merge_prints_the_merged_object(tmp_path) -> None:
    # given
    base = _write(tmp_path, "base.json", {"a": "1"})
    local = _write(tmp_path, "local.json", {"a": "2"})
    remote = _write(tmp_path, "remote.json", {"a": "1"})

    # when
    result = runner.invoke(main_module.app, ["merge", base, local, remote])

    # then
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"a": "2"}


def test_merge_exits_1_and_names_the_conflict(tmp_path) -> None:
    # given
    base = _write(tmp_path, "base.json", {"a": "1"})
    local = _write(tmp_path, "local.json", {"a": "2"})
    remote = _write(tmp_path, "remote.json", {"a": "3"})

    # when
    result = runner.invoke(main_module.app, ["merge", base, local, remote])

    # then
    assert result.exit_code == 1
    assert "conflict: a" in _combined(result)


def test_merge_rejects_an_unreadable_file(tmp_path) -> None:
    # given
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    good = _write(tmp_path, "good.json", {})

    # when
    result = runner.invoke(main_module.app, ["merge", str(bad), good, good])

    # then
    assert result.exit_code == 2


def test_merge_accepts_a_top_level_array(tmp_path) -> None:
    # given — a document whose root is an array
    base = _write(tmp_path, "base.json", [1, 2])
    local = _write(tmp_path, "local.json", [1, 2, 3])
    remote = _write(tmp_path, "remote.json", [1, 2])

    # when
    result = runner.invoke(main_module.app, ["merge", base, local, remote])

    # then
    assert result.exit_code == 0
    assert json.loads(result.stdout) == [1, 2, 3]


def test_main_dispatches_to_the_app(monkeypatch) -> None:
    # given
    calls: dict[str, int] = {}
    monkeypatch.setattr(main_module, "app", lambda: calls.setdefault("count", 0))

    # when
    main_module.main()

    # then
    assert calls == {"count": 0}


def test_sync_rejects_an_unknown_adopt_choice() -> None:
    # when
    result = runner.invoke(main_module.app, ["sync", "--adopt", "both"])

    # then
    assert result.exit_code == 2


def test_sync_without_a_configuration_reports_an_error() -> None:
    # when
    result = runner.invoke(main_module.app, ["sync"])

    # then
    assert result.exit_code == 1
    assert "error:" in _combined(result)


def test_importing_the_cli_does_not_import_a_heavy_dependency() -> None:
    # given — a fresh interpreter, so the modules other tests imported are absent
    code = (
        "import sys, syncintosh.cli.main; "
        "print(','.join(m for m in ('cryptography', 'lxml', 'openai') if m in sys.modules))"
    )

    # when
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    # then — CLI startup stays fast (AGENTS.md §4)
    assert result.stdout.strip() == ""


def test_importing_the_cli_does_not_import_the_jsonc_adapter() -> None:
    # given — the jsonc adapter is only needed inside `merge`/`sync`, not at import time
    code = "import sys, syncintosh.cli.main; print('syncintosh.adapters.jsonc' in sys.modules)"

    # when
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    # then
    assert result.stdout.strip() == "False"
