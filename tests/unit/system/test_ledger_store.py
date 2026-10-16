# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the on-disk apply ledger."""

from __future__ import annotations

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
