# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the immutable manifest models and stable tracking.

Field rules, the scope/machine rule, the frozen record map, and the "mint an ID once" rule of
``track`` are the boundary behaviour #3 ships (``AGENTS.md`` §5.7).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from syncintosh.core.errors import ManifestCorrupt, UnsupportedManifestSchema
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord, track

ID1 = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
ID2 = "00112233445566778899aabbccddeeff"
SHA1 = "a" * 64
SHA2 = "b" * 64

VALID: dict[str, Any] = {
    "file_id": ID1,
    "path": ".zshrc",
    "preset": "zsh",
    "scope": "common",
    "machine": None,
    "format": "text",
    "sha256": SHA1,
}


def _record(**overrides: Any) -> ManifestRecord:
    """A valid record with the given fields overridden."""
    return ManifestRecord.create(**{**VALID, **overrides})


def _manifest(records: dict[str, ManifestRecord] | None = None, generation: int = 1) -> Manifest:
    return Manifest.create(schema=MANIFEST_SCHEMA, key_generation=generation, records=records or {})


class StubIdSource:
    """A `FileIdSource` returning a fixed script of ids, so a test can force a bad one."""

    def __init__(self, *ids: str) -> None:
        self._ids = list(ids)

    def new_file_id(self) -> str:
        return self._ids.pop(0)


def test_record_create_accepts_a_common_scope() -> None:
    # when
    record = _record()

    # then
    assert record.file_id == ID1
    assert record.machine is None
    assert record.format == "text"


def test_record_create_accepts_a_machine_scope_with_a_machine() -> None:
    # when
    record = _record(scope="machine", machine="mac-studio")

    # then
    assert record.scope == "machine"
    assert record.machine == "mac-studio"


@pytest.mark.parametrize(
    "overrides",
    [
        {"file_id": "short"},
        {"file_id": "F" * 32},
        {"file_id": "0f3c4b5a69788796a5b4c3d2e1f0abc"},
        {"path": ""},
        {"path": "/etc/zshrc"},
        {"path": "has\x00nul"},
        {"path": "./.zshrc"},
        {"path": "."},
        {"path": "a//b"},
        {"path": "a/"},
        {"path": "a/../b"},
        {"path": ".."},
        {"path": "\ud800"},
        {"preset": ""},
        {"preset": "\ud800"},
        {"scope": "shared"},
        {"format": "ini"},
        {"sha256": "a" * 63},
        {"sha256": "A" * 64},
        {"scope": "machine", "machine": None},
        {"scope": "machine", "machine": "\ud800"},
        {"scope": "common", "machine": "mac-studio"},
    ],
)
def test_record_create_rejects_a_bad_field(overrides) -> None:
    # when & then
    with pytest.raises(ManifestCorrupt):
        _record(**overrides)


def test_manifest_create_rejects_a_newer_schema() -> None:
    # when & then — a schema bump is a migration, distinct from a corrupt manifest
    with pytest.raises(UnsupportedManifestSchema):
        Manifest.create(schema=2, key_generation=1, records={})


@pytest.mark.parametrize(
    ("schema", "generation"),
    [(True, 1), ("1", 1), (1, 0), (1, -1), (1, True), (1, "1")],
)
def test_manifest_create_rejects_bad_wide_fields(schema, generation) -> None:
    # when & then
    with pytest.raises(ManifestCorrupt):
        Manifest.create(schema=schema, key_generation=generation, records={})


def test_manifest_create_rejects_a_key_that_does_not_match_its_record() -> None:
    # given — the map keyed by file_id must agree with the record's own file_id
    records = {ID2: _record()}

    # when & then
    with pytest.raises(ManifestCorrupt):
        _manifest(records)


def test_empty_manifest_has_no_records() -> None:
    # when
    manifest = Manifest.empty(key_generation=3)

    # then
    assert manifest.schema == MANIFEST_SCHEMA
    assert manifest.key_generation == 3
    assert dict(manifest.records) == {}


def test_manifest_records_cannot_be_mutated() -> None:
    # given
    manifest = _manifest({ID1: _record()})

    # when & then
    with pytest.raises(TypeError):
        manifest.records[ID2] = _record(file_id=ID2)  # ty: ignore[invalid-assignment]  # MappingProxyType


def test_record_is_frozen() -> None:
    # given
    record = _record()

    # when & then
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.path = "/tmp/other"  # ty: ignore[invalid-assignment]  # frozen dataclass


def test_track_mints_and_reuses_one_id_for_a_path() -> None:
    # given
    source = StubIdSource(ID2)

    # when — the same (scope, machine, path) is tracked twice with different content
    first = track(
        Manifest.empty(1), path=".zshrc", preset="zsh", scope="common", format="text", sha256=SHA1, source=source
    )
    second = track(first, path=".zshrc", preset="zsh", scope="common", format="text", sha256=SHA2, source=source)

    # then — one minted id, reused on the second call, with the refreshed digest
    assert list(first.records) == [ID2]
    assert list(second.records) == [ID2]
    assert second.records[ID2].sha256 == SHA2


def test_track_keeps_the_manifest_sibling_records() -> None:
    # given
    base = _manifest({ID1: _record()})

    # when
    tracked = track(
        base, path=".config/x", preset="zed", scope="common", format="jsonc", sha256=SHA2, source=StubIdSource(ID2)
    )

    # then
    assert sorted(tracked.records) == sorted([ID1, ID2])


def test_track_rejects_a_malformed_id_from_the_source() -> None:
    # given — the port contract is 32 lowercase hex characters
    source = StubIdSource("not-hex")

    # when & then
    with pytest.raises(ValueError):
        track(Manifest.empty(1), path=".zshrc", preset="zsh", scope="common", format="text", sha256=SHA1, source=source)


def test_track_rejects_an_id_the_source_repeats() -> None:
    # given — the source hands back an id the manifest already holds
    base = _manifest({ID1: _record()})

    # when & then
    with pytest.raises(ValueError):
        track(
            base, path=".config/x", preset="zed", scope="common", format="jsonc", sha256=SHA2, source=StubIdSource(ID1)
        )
