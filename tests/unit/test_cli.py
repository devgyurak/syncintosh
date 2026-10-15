# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the CLI: parsing, delegation, rendering, and exit codes."""

from __future__ import annotations

import json
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


def test_merge_rejects_a_non_object_document(tmp_path) -> None:
    # given
    array = _write(tmp_path, "array.json", [1, 2, 3])
    good = _write(tmp_path, "good.json", {})

    # when
    result = runner.invoke(main_module.app, ["merge", array, good, good])

    # then
    assert result.exit_code == 2


def test_main_dispatches_to_the_app(monkeypatch) -> None:
    # given
    calls: dict[str, int] = {}
    monkeypatch.setattr(main_module, "app", lambda: calls.setdefault("count", 0))

    # when
    main_module.main()

    # then
    assert calls == {"count": 0}
