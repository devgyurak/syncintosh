# SPDX-License-Identifier: AGPL-3.0-or-later
"""The merge command says why a render refused, in a fixed text that names no value."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from syncintosh.adapters.jsonc import CHANGED_WITH_NOTE_REFUSED, UNRULED_REMOVAL_REFUSED
from syncintosh.cli import main as main_module
from syncintosh.cli.main import WHEN_RUN_KEY_NOTICE
from syncintosh.core.keyed_array import ORDER_CONFLICT_REASON
from syncintosh.presets.vscode import KEYBINDINGS
from syncintosh.system.paths import MachinePaths

runner = CliRunner()

BASE = b'[\n  { "key": "a", /* why */ "command": "x" },\n  { "key": "b", "command": "y" }\n]\n'
# The local side edits `b`, so its template still holds `a` as it was; the remote side changes `a`, which carries a
# comment. The clean merge takes `a` from the remote, and rendering it into the local template needs a rewrite of
# `a`'s span, which holds the comment: the render refuses.
LOCAL = b'[\n  { "key": "a", /* why */ "command": "x" },\n  { "key": "b", "command": "y2" }\n]\n'
REMOTE = b'[\n  { "key": "a", /* why */ "command": "x2" },\n  { "key": "b", "command": "y" }\n]\n'
RULERS_BASE = b'{\n  "editor.rulers": [\n    /* why 80 */\n    80,\n    120\n  ]\n}\n'
RULERS_REMOTE = b'{\n  "editor.rulers": [\n    120\n  ]\n}\n'


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
    assert f"not merged: {CHANGED_WITH_NOTE_REFUSED}" in result.output
    reasons = [line for line in result.output.splitlines() if line.startswith("not merged:")]
    assert reasons == [f"not merged: {CHANGED_WITH_NOTE_REFUSED}"]


# A `when` clause is printed as a keyed tag, never as its text, whatever state the machine's allowlist is in.
WHEN_BASE = b'[\n  { "key": "b", "command": "y", "when": "editorFocus" }\n]\n'
WHEN_LOCAL = b'[\n  { "key": "b", "command": "y1", "when": "editorFocus" }\n]\n'
WHEN_REMOTE = b'[\n  { "key": "b", "command": "y2", "when": "editorFocus" }\n]\n'
UNUSABLE_ALLOWLISTS = {
    "missing": None,
    "not JSON": "{ nope",
    "key too short": '{"version": 1, "key": "ab", "entries": []}',
    "unknown version": '{"version": 99, "key": "%s", "entries": []}' % ("00" * 32),
    "nested too deeply": '{"version": 1, "key": "' + "00" * 32 + '", "entries": ' + "[" * 100_000 + "]" * 100_000 + "}",
}


def _seed_allowlist(text: str | None) -> None:
    """Write the machine's allowlist as `text`, or leave it missing when `text` is None."""
    if text is None:
        return
    allow = MachinePaths.default().scan_allow_file
    allow.parent.mkdir(parents=True, exist_ok=True)
    allow.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("allowlist", list(UNUSABLE_ALLOWLISTS.values()), ids=list(UNUSABLE_ALLOWLISTS))
def test_a_when_clause_conflict_prints_a_tag_and_never_the_clause_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowlist: str | None
) -> None:
    # given — both sides changed the same binding, which has a `when`; the machine's allowlist is missing or unusable
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _seed_allowlist(allowlist)
    base, local, remote = tmp_path / "base.jsonc", tmp_path / "local.jsonc", tmp_path / "remote.jsonc"
    base.write_bytes(WHEN_BASE)
    local.write_bytes(WHEN_LOCAL)
    remote.write_bytes(WHEN_REMOTE)

    # when
    result = runner.invoke(main_module.app, ["merge", str(base), str(local), str(remote), "--path", KEYBINDINGS])

    # then — the conflict is reported with a tag, the run does not crash, and the clause text is not printed
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    conflicts = [line for line in result.output.splitlines() if line.startswith("conflict:")]
    assert len(conflicts) == 1
    assert re.fullmatch(r"conflict: \(document\) \[key=\"b\", when=[0-9a-f]{12}\]", conflicts[0]) is not None
    # the tags come from a key for this run only, so the person is told once, on stderr
    assert WHEN_RUN_KEY_NOTICE in result.output


def test_an_order_conflict_names_the_whole_array_and_why_it_is_one(tmp_path: Path) -> None:
    # given — both sides reorder the shared bindings, to different orders
    def binding(name: str) -> bytes:
        return b'{ "key": "%s", "command": "x" }' % name.encode()

    base = tmp_path / "base.jsonc"
    local = tmp_path / "local.jsonc"
    remote = tmp_path / "remote.jsonc"
    base.write_bytes(b"[\n  " + b",\n  ".join(binding(k) for k in "abc") + b"\n]\n")
    local.write_bytes(b"[\n  " + b",\n  ".join(binding(k) for k in "bac") + b"\n]\n")
    remote.write_bytes(b"[\n  " + b",\n  ".join(binding(k) for k in "acb") + b"\n]\n")

    # when
    result = runner.invoke(main_module.app, ["merge", str(base), str(local), str(remote), "--path", KEYBINDINGS])

    # then — the conflict line names the whole array and the fixed reason; it is not a bare "(document)"
    conflicts = [line for line in result.output.splitlines() if line.startswith("conflict:")]
    assert result.exit_code == 1
    assert conflicts == [f"conflict: (document) ({ORDER_CONFLICT_REASON})"]


WHEN_TWO_BASE = (
    b'[\n  { "key": "b", "command": "y", "when": "editorFocus" },\n'
    b'  { "key": "c", "command": "z", "when": "editorFocus" }\n]\n'
)
WHEN_TWO_LOCAL = (
    b'[\n  { "key": "b", "command": "y1", "when": "editorFocus" },\n'
    b'  { "key": "c", "command": "z1", "when": "editorFocus" }\n]\n'
)
WHEN_TWO_REMOTE = (
    b'[\n  { "key": "b", "command": "y2", "when": "editorFocus" },\n'
    b'  { "key": "c", "command": "z2", "when": "editorFocus" }\n]\n'
)


def test_two_when_tags_in_one_output_print_the_run_key_notice_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # given — two bindings each conflict on their own group, and no allowlist: the tags use a key for this run only
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    base, local, remote = tmp_path / "base.jsonc", tmp_path / "local.jsonc", tmp_path / "remote.jsonc"
    base.write_bytes(WHEN_TWO_BASE)
    local.write_bytes(WHEN_TWO_LOCAL)
    remote.write_bytes(WHEN_TWO_REMOTE)

    # when
    result = runner.invoke(main_module.app, ["merge", str(base), str(local), str(remote), "--path", KEYBINDINGS])

    # then — two conflict lines, each with a tag, and the notice once
    conflicts = [line for line in result.output.splitlines() if line.startswith("conflict:")]
    assert len(conflicts) == 2
    assert result.output.count(WHEN_RUN_KEY_NOTICE) == 1


def test_a_conflict_with_no_tag_prints_no_run_key_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # given — a scalar setting that both sides changed differently: a conflict, but its label carries no `when` tag
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    base, local, remote = tmp_path / "base.jsonc", tmp_path / "local.jsonc", tmp_path / "remote.jsonc"
    base.write_bytes(b'{\n  "editor.fontSize": 13\n}\n')
    local.write_bytes(b'{\n  "editor.fontSize": 14\n}\n')
    remote.write_bytes(b'{\n  "editor.fontSize": 15\n}\n')

    # when
    result = runner.invoke(main_module.app, ["merge", str(base), str(local), str(remote), "--path", "settings.json"])

    # then — the conflict is named, no tag is shown, and so the notice is not printed
    conflicts = [line for line in result.output.splitlines() if line.startswith("conflict:")]
    assert conflicts == ['conflict: "editor.fontSize"']
    assert WHEN_RUN_KEY_NOTICE not in result.output


def test_the_tagger_says_nothing_until_a_tag_is_shown_and_then_says_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # given — a machine with no allowlist, so the tagger uses a key for this run only, and the tagger is made
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    tag = main_module._machine_when_tag()
    before = capsys.readouterr().err

    # when — several tags are shown
    shown = [tag("editorFocus"), tag("resourceHost == 'build.internal'"), tag("editorFocus")]

    # then — no notice before a tag, exactly one notice on the first tag, and the tags still tell two clauses apart
    assert before == ""
    assert capsys.readouterr().err.count(WHEN_RUN_KEY_NOTICE) == 1
    assert shown[0] != shown[1] and shown[0] == shown[2]


def test_a_rule_less_refusal_says_its_fixed_reason(tmp_path: Path) -> None:
    # given — `editor.rulers` has no rule; the remote removes 80 and the note above it, and the local keeps both
    base, local, remote = tmp_path / "base.jsonc", tmp_path / "local.jsonc", tmp_path / "remote.jsonc"
    base.write_bytes(RULERS_BASE)
    local.write_bytes(RULERS_BASE)
    remote.write_bytes(RULERS_REMOTE)

    # when
    result = runner.invoke(main_module.app, ["merge", str(base), str(local), str(remote), "--path", "settings.json"])

    # then — the reason the adapter names is said, and nothing from the array is printed in it
    assert result.exit_code == 1
    assert f"not merged: {UNRULED_REMOVAL_REFUSED}" in result.output
