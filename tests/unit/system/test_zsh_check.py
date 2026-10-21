# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `zsh -fn` syntax validator behind the `SyntaxValidator` port."""

from __future__ import annotations

import subprocess

import pytest

from syncintosh.core.errors import SyntaxInvalid
from syncintosh.system.zsh_check import ZshSyntaxValidator

VALID = "if true; then\n  echo hi\nfi\n"
BROKEN = "if true; then\n  echo hi\n"


def test_a_valid_shell_file_passes() -> None:
    # when & then
    ZshSyntaxValidator().check(VALID, subject=".zshrc")


def test_a_broken_shell_file_is_refused() -> None:
    # when & then
    with pytest.raises(SyntaxInvalid):
        ZshSyntaxValidator().check(BROKEN, subject=".zshrc")


def test_the_validator_never_runs_the_file(tmp_path) -> None:
    # given — a file that would leave a marker behind and then fails to parse
    marker = tmp_path / "ran"
    text = f"touch {marker}\nif true; then\n"

    # when
    with pytest.raises(SyntaxInvalid):
        ZshSyntaxValidator().check(text, subject=".zshrc")

    # then — `-n` parses only (docs/design/presets.md, the zsh row)
    assert marker.exists() is False


def test_a_refusal_names_the_subject_and_never_the_text() -> None:
    # given
    text = "echo MARKER-abc123\nif true; then\n"

    # when
    with pytest.raises(SyntaxInvalid) as refusal:
        ZshSyntaxValidator().check(text, subject=".zshrc")

    # then — a log line is where a recovered secret would leak (AGENTS.md §4)
    assert ".zshrc" in str(refusal.value)
    assert "MARKER-abc123" not in str(refusal.value)


def test_a_missing_zsh_is_a_refusal_not_a_pass(monkeypatch) -> None:
    # given — no `zsh` on this machine
    def _missing(*args, **kwargs):
        raise FileNotFoundError("zsh")

    monkeypatch.setattr("syncintosh.system.zsh_check.subprocess.run", _missing)

    # when & then — a validator that failed open would publish the very file it guards
    with pytest.raises(SyntaxInvalid):
        ZshSyntaxValidator().check(VALID, subject=".zshrc")


def test_a_check_that_cannot_run_in_time_is_a_refusal(monkeypatch) -> None:
    # given — a check that hangs
    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="zsh", timeout=1)

    monkeypatch.setattr("syncintosh.system.zsh_check.subprocess.run", _timeout)

    # when & then
    with pytest.raises(SyntaxInvalid):
        ZshSyntaxValidator().check(VALID, subject=".zshrc")
