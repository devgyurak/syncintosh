# SPDX-License-Identifier: AGPL-3.0-or-later
"""The merge command says why a render refused, in a fixed text that names no value."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from syncintosh.cli import main as main_module
from syncintosh.core.content_merge import RENDER_REFUSED
from syncintosh.presets.vscode import KEYBINDINGS

runner = CliRunner()

BASE = b'[\n  { "key": "a", /* why */ "command": "x" },\n  { "key": "b", "command": "y" }\n]\n'
# The local side edits `b`, so its template still holds `a` as it was; the remote side changes `a`, which carries a
# comment. The clean merge takes `a` from the remote, and rendering it into the local template needs a rewrite of
# `a`'s span, which holds the comment: the render refuses.
LOCAL = b'[\n  { "key": "a", /* why */ "command": "x" },\n  { "key": "b", "command": "y2" }\n]\n'
REMOTE = b'[\n  { "key": "a", /* why */ "command": "x2" },\n  { "key": "b", "command": "y" }\n]\n'


def _write(tmp_path: Path) -> tuple[Path, Path, Path]:
    base, local, remote = tmp_path / "base.jsonc", tmp_path / "local.jsonc", tmp_path / "remote.jsonc"
    base.write_bytes(BASE)
    local.write_bytes(LOCAL)
    remote.write_bytes(REMOTE)
    return base, local, remote


def test_a_refused_render_exits_with_the_fixed_reason_and_no_value(tmp_path: Path) -> None:
    # given — the remote changed a binding that carries a comment inside it
    base, local, remote = _write(tmp_path)

    # when
    result = runner.invoke(main_module.app, ["merge", str(base), str(local), str(remote), "--path", KEYBINDINGS])

    # then — exit 1, the fixed reason is said, and neither side's value is printed in it
    assert result.exit_code == 1
    assert f"not merged: {RENDER_REFUSED}" in result.output
    assert "x2" not in RENDER_REFUSED and "y2" not in RENDER_REFUSED
