# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the machine-local scan allowlist (#25).

The file holds a keyed fingerprint, never the matched text, so these tests check both that the
mechanism works and that the plaintext never lands on disk.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from syncintosh.core.errors import ScanAllowCorrupt
from syncintosh.system.scan_allow import (
    KEY_BYTES,
    ScanAllow,
    ScanAllowEntry,
    fingerprint,
    load_scan_allow,
    new_scan_allow,
    save_scan_allow,
)

KEY = b"k" * KEY_BYTES
MATCHED = b"http://me:s3cr;t@proxy:8080"

MALFORMED = [
    "{not json",
    "[]",
    '{"version": 2, "key": "' + "ab" * 32 + '", "entries": []}',
    '{"version": 1, "entries": []}',
    '{"version": 1, "key": "zz", "entries": []}',
    '{"version": 1, "key": "' + "ab" * 31 + '", "entries": []}',
    '{"version": 1, "key": "' + "ab" * 32 + '", "entries": {}}',
    '{"version": 1, "key": "' + "ab" * 32 + '", "entries": [{"path": ".zshrc"}]}',
    '{"version": 1, "key": "' + "ab" * 32 + '", "entries": [{"path": ".zshrc", "label": "x", "fingerprint": "AAAA"}]}',
    '{"version": 1, "key": "'
    + "ab" * 32
    + '", "entries": [{"path": "", "label": "x", "fingerprint": "'
    + "a" * 32
    + '"}]}',
    '{"version": 1, "key": "'
    + "ab" * 32
    + '", "entries": [{"path": ".zshrc", "label": "x", "fingerprint": "'
    + "a" * 32
    + '", "extra": 1}]}',
]


def test_a_new_allowlist_has_a_full_key_and_no_entries() -> None:
    # when
    allow = new_scan_allow()

    # then
    assert len(allow.key) == KEY_BYTES
    assert allow.entries == frozenset()


def test_a_fresh_load_creates_the_file_with_a_persisted_key(tmp_path) -> None:
    # given
    path = tmp_path / "state" / "scan-allow.json"

    # when
    allow = load_scan_allow(path)

    # then — the key must persist, or a fingerprint taken now would not mean anything next run
    assert path.exists()
    assert load_scan_allow(path).key == allow.key


def test_the_allowlist_is_written_0600(tmp_path) -> None:
    # given — the file holds the HMAC key, so another account must not be able to read it
    path = tmp_path / "scan-allow.json"

    # when
    save_scan_allow(new_scan_allow(), path)

    # then
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_entries_round_trip_through_the_file(tmp_path) -> None:
    # given
    path = tmp_path / "scan-allow.json"
    entry = ScanAllowEntry(path=".zshrc", label="URL with credentials", fingerprint="a" * 32)

    # when
    save_scan_allow(new_scan_allow().with_entry(entry), path)
    reloaded = load_scan_allow(path)

    # then
    assert reloaded.entries == frozenset({entry})


def test_allows_matches_the_whole_triple() -> None:
    # given
    entry = ScanAllowEntry(path=".zshrc", label="URL with credentials", fingerprint="b" * 32)
    allow = ScanAllow(key=KEY, entries=frozenset({entry}))

    # when & then — a different path, label, or fingerprint is not the allowed span
    assert allow.allows(".zshrc", "URL with credentials", "b" * 32)
    assert not allow.allows(".profile", "URL with credentials", "b" * 32)
    assert not allow.allows(".zshrc", "GitHub token", "b" * 32)
    assert not allow.allows(".zshrc", "URL with credentials", "c" * 32)


def test_the_fingerprint_is_keyed_and_stable() -> None:
    # given — the same bytes under two keys must not share a fingerprint (or the key is pointless);
    # the same bytes under one key must stay stable, or `scan allow` could never match again
    # when
    first = fingerprint(KEY, MATCHED)
    again = fingerprint(KEY, MATCHED)
    other = fingerprint(b"o" * KEY_BYTES, MATCHED)

    # then
    assert first == again
    assert first != other
    assert len(first) == 32


def test_the_file_never_holds_the_matched_text(tmp_path) -> None:
    # given — a stored fingerprint must not carry the plaintext it stands for
    path = tmp_path / "scan-allow.json"
    entry = ScanAllowEntry(path=".zshrc", label="URL with credentials", fingerprint=fingerprint(KEY, MATCHED))

    # when
    save_scan_allow(ScanAllow(key=KEY, entries=frozenset({entry})), path)

    # then
    assert MATCHED.decode() not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("payload", MALFORMED)
def test_a_malformed_allowlist_is_refused(payload: str, tmp_path: Path) -> None:
    # given — silently resetting would refuse every allowed span again with no explanation
    path = tmp_path / "scan-allow.json"
    path.write_text(payload, encoding="utf-8")

    # when & then
    with pytest.raises(ScanAllowCorrupt):
        load_scan_allow(path)


@pytest.mark.parametrize(
    ("path", "label", "fingerprint"),
    [
        (".zshrc", "URL with credentials", "NOT-A-FINGERPRINT"),
        (".zshrc", "URL with credentials", "ABCDEF" + "a" * 26),
        (".zshrc", "URL with credentials", "a" * 31),
        ("", "URL with credentials", "a" * 32),
        (".zshrc", "", "a" * 32),
    ],
)
def test_an_invalid_entry_is_refused_when_built(path: str, label: str, fingerprint: str) -> None:
    # given — validating at construction is what stops `scan allow` from writing a shape the
    # loader would reject, which would brick the list until the user deleted it (and lost the key)
    # when & then
    with pytest.raises(ScanAllowCorrupt):
        ScanAllowEntry(path=path, label=label, fingerprint=fingerprint)


def test_the_repr_masks_the_key() -> None:
    # given — a traceback or a debugger dump must not print the HMAC key (``AGENTS.md`` §4)
    allow = ScanAllow(key=b"\x01" * KEY_BYTES, entries=frozenset())

    # when
    shown = repr(allow)

    # then
    assert "\x01" not in shown
    assert "masked" in shown


def test_a_label_whose_match_is_fixed_text_is_not_allowlistable() -> None:
    # given — the PEM header is identical in every private key, so one allowed entry would pass them
    # all; the scan's own label for it must therefore be refused outright
    # when & then
    with pytest.raises(ScanAllowCorrupt):
        ScanAllowEntry(path=".zshrc", label="private key", fingerprint="a" * 32)
