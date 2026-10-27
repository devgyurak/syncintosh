# SPDX-License-Identifier: AGPL-3.0-or-later
"""The keyed tag of a `when` clause: the same text under one machine's key gives one tag, and it never creates a key."""

from __future__ import annotations

from pathlib import Path

from syncintosh.system.scan_allow import keyed_when_tag, new_scan_allow, save_scan_allow


def test_the_same_text_under_one_machine_key_gives_the_same_tag(tmp_path: Path) -> None:
    # given
    allow = tmp_path / "allow.json"
    save_scan_allow(new_scan_allow(), allow)
    tag = keyed_when_tag(allow)

    # when & then
    assert tag is not None
    assert tag("resourceHost == 'build.internal'") == tag("resourceHost == 'build.internal'")


def test_a_different_machine_key_gives_a_different_tag_for_the_same_text(tmp_path: Path) -> None:
    # given — two machines, each with its own key
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    save_scan_allow(new_scan_allow(), first)
    save_scan_allow(new_scan_allow(), second)
    text = "editorTextFocus && !editorReadonly"

    # when & then — a dictionary of likely `when` clauses cannot be checked against one machine's tag without its key
    first_tag = keyed_when_tag(first)
    second_tag = keyed_when_tag(second)
    assert first_tag is not None and second_tag is not None
    assert first_tag(text) != second_tag(text)


def test_without_a_key_there_is_no_tagger_and_the_allowlist_is_not_created(tmp_path: Path) -> None:
    # given — a fresh machine: no allowlist file yet
    allow = tmp_path / "allow.json"

    # when
    tag = keyed_when_tag(allow)

    # then — the caller falls back to the hidden form, and nothing was written
    assert tag is None
    assert not allow.exists()
