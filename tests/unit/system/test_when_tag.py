# SPDX-License-Identifier: AGPL-3.0-or-later
"""The keyed tag of a `when` clause: one key gives one tag, a display never writes the allowlist, and a bad one never
fails a display."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncintosh.core.errors import ScanAllowCorrupt
from syncintosh.system.scan_allow import (
    ALLOW_VERSION,
    KEY_BYTES,
    load_scan_allow,
    machine_when_tag,
    new_scan_allow,
    save_scan_allow,
    when_tag_for,
)

# Each way an allowlist can be unusable for a display: the loader refuses every one of them.
_UNUSABLE = {
    "no key": json.dumps({"version": ALLOW_VERSION, "entries": []}),
    "not JSON": "{ nope",
    "key too short": json.dumps({"version": ALLOW_VERSION, "key": "ab", "entries": []}),
    "unknown version": json.dumps({"version": ALLOW_VERSION + 1, "key": "00" * KEY_BYTES, "entries": []}),
}


def test_the_same_text_under_one_machine_key_gives_the_same_tag(tmp_path: Path) -> None:
    # given
    allow = tmp_path / "allow.json"
    save_scan_allow(new_scan_allow(), allow)
    tag = machine_when_tag(allow)

    # when & then
    assert tag("resourceHost == 'build.internal'") == tag("resourceHost == 'build.internal'")


def test_a_different_machine_key_gives_a_different_tag_for_the_same_text(tmp_path: Path) -> None:
    # given — two machines, each with its own key
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    save_scan_allow(new_scan_allow(), first)
    save_scan_allow(new_scan_allow(), second)
    text = "editorTextFocus && !editorReadonly"

    # when & then — a dictionary of likely `when` clauses cannot be checked against one machine's tag without its key
    assert machine_when_tag(first)(text) != machine_when_tag(second)(text)


def test_a_tag_under_the_key_as_loaded_is_the_tag_the_machine_gives(tmp_path: Path) -> None:
    # given — the sync path tags with the key it loaded; a display reads the same stored key
    allow = tmp_path / "allow.json"
    loaded = load_scan_allow(allow)

    # when & then
    assert when_tag_for(loaded.key)("editorTextFocus") == machine_when_tag(allow)("editorTextFocus")


def test_the_key_is_part_of_the_tag(tmp_path: Path) -> None:
    # when & then — the same key gives the same tag, and a different key a different one
    assert when_tag_for(bytes(KEY_BYTES))("x") != when_tag_for(b"\x01" * KEY_BYTES)("x")


def test_without_a_key_the_display_still_tells_clauses_apart_and_creates_no_allowlist(tmp_path: Path) -> None:
    # given — a fresh machine: no allowlist file yet
    allow = tmp_path / "allow.json"

    # when
    tag = machine_when_tag(allow)

    # then — a run key still separates two clauses, and nothing was written
    assert tag("editorTextFocus") != tag("resourceHost == 'build.internal'")
    assert not allow.exists()


@pytest.mark.parametrize("text", list(_UNUSABLE.values()), ids=list(_UNUSABLE))
def test_an_unusable_allowlist_gives_the_display_a_run_key_and_is_left_as_it_was(tmp_path: Path, text: str) -> None:
    # given — an allowlist the loader refuses
    allow = tmp_path / "allow.json"
    allow.write_text(text, encoding="utf-8")

    # when — a display asks for a tagger; it must not raise
    tag = machine_when_tag(allow)

    # then — the display still tells clauses apart, the file is untouched, and the write path still fails loudly
    assert tag("editorTextFocus") != tag("resourceHost == 'build.internal'")
    assert allow.read_text(encoding="utf-8") == text
    with pytest.raises(ScanAllowCorrupt):
        load_scan_allow(allow)
