# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the on-disk apply ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncintosh.core.errors import LedgerCorrupt
from syncintosh.core.models import LedgerEntry
from syncintosh.system.ledger import JsonLedgerStore

FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
ENTRY = LedgerEntry(sha256="a" * 64, commit="b" * 40, suppressed=False)


def test_a_missing_ledger_reads_as_empty(tmp_path: Path) -> None:
    # given
    store = JsonLedgerStore(tmp_path / "applied.json")

    # when & then
    assert store.load() == {}


def test_a_ledger_round_trips(tmp_path: Path) -> None:
    # given
    store = JsonLedgerStore(tmp_path / "applied.json")

    # when
    store.save({FILE_ID: ENTRY})

    # then
    assert store.load() == {FILE_ID: ENTRY}


def test_a_suppressed_entry_round_trips(tmp_path: Path) -> None:
    # given
    store = JsonLedgerStore(tmp_path / "applied.json")
    suppressed = LedgerEntry(sha256="a" * 64, commit="b" * 40, suppressed=True)

    # when
    store.save({FILE_ID: suppressed})

    # then
    assert store.load()[FILE_ID].suppressed is True


def test_a_release_round_trips(tmp_path: Path) -> None:
    # given
    store = JsonLedgerStore(tmp_path / "applied.json")
    versioned = LedgerEntry(sha256="a" * 64, commit="b" * 40, suppressed=False, release="DataGrip2026.2")

    # when
    store.save({FILE_ID: versioned})

    # then
    assert store.load() == {FILE_ID: versioned}


def test_an_entry_without_a_release_is_saved_without_the_key(tmp_path: Path) -> None:
    # given
    path = tmp_path / "applied.json"

    # when
    JsonLedgerStore(path).save({FILE_ID: ENTRY})

    # then — a ledger with no versioned dirs stays readable by a build that predates the field
    assert "release" not in json.loads(path.read_text(encoding="utf-8"))["files"][FILE_ID]


def test_a_legacy_ledger_without_a_release_loads_with_it_empty(tmp_path: Path) -> None:
    # given
    path = tmp_path / "applied.json"
    legacy = {"version": 1, "files": {FILE_ID: {"sha256": "a" * 64, "commit": "b" * 40, "suppressed": False}}}
    path.write_text(json.dumps(legacy), encoding="utf-8")

    # when
    loaded = JsonLedgerStore(path).load()

    # then
    assert loaded == {FILE_ID: ENTRY}
    assert loaded[FILE_ID].release == ""


def test_a_non_string_release_is_a_domain_error(tmp_path: Path) -> None:
    # given
    path = tmp_path / "applied.json"
    bad = {"files": {FILE_ID: {"sha256": "a", "commit": "b", "suppressed": False, "release": 7}}}
    path.write_text(json.dumps(bad), encoding="utf-8")

    # when & then
    with pytest.raises(LedgerCorrupt):
        JsonLedgerStore(path).load()


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"files": []}',
        '{"files": {"' + FILE_ID + '": 7}}',
        '{"files": {"' + FILE_ID + '": {"sha256":"a","commit":"b"}}}',
    ],
)
def test_a_malformed_ledger_is_a_domain_error(tmp_path: Path, payload: str) -> None:
    # given
    path = tmp_path / "applied.json"
    path.write_text(payload, encoding="utf-8")
    store = JsonLedgerStore(path)

    # when & then
    with pytest.raises(LedgerCorrupt):
        store.load()


def test_a_profile_anchor_round_trips(tmp_path: Path) -> None:
    # given
    store = JsonLedgerStore(tmp_path / "applied.json")
    anchored = LedgerEntry(
        sha256="a" * 64, commit="b" * 40, suppressed=False, profile_dir="7c91e0aa", profile_key="d" * 64
    )

    # when
    store.save({FILE_ID: anchored})

    # then
    assert store.load() == {FILE_ID: anchored}


@pytest.mark.parametrize(
    "partial",
    [
        pytest.param({"profile_dir": "7c91e0aa"}, id="directory-only"),
        pytest.param({"profile_key": "d" * 64}, id="key-only"),
        pytest.param({}, id="neither"),
    ],
)
def test_a_half_anchor_is_not_written(tmp_path: Path, partial: dict[str, str]) -> None:
    # given
    path = tmp_path / "applied.json"
    entry = LedgerEntry(sha256="a" * 64, commit="b" * 40, suppressed=False, **partial)

    # when
    JsonLedgerStore(path).save({FILE_ID: entry})

    # then — a lone half would never match a rename, and an anchor-free ledger stays readable by an older build
    written = json.loads(path.read_text(encoding="utf-8"))["files"][FILE_ID]
    assert {"profile_dir", "profile_key"} & written.keys() == set()


@pytest.mark.parametrize("field", ["profile_dir", "profile_key"])
def test_a_non_string_profile_anchor_is_a_domain_error(tmp_path: Path, field: str) -> None:
    # given
    path = tmp_path / "applied.json"
    bad = {"files": {FILE_ID: {"sha256": "a", "commit": "b", "suppressed": False, field: 7}}}
    path.write_text(json.dumps(bad), encoding="utf-8")

    # when & then
    with pytest.raises(LedgerCorrupt):
        JsonLedgerStore(path).load()
