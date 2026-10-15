# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for canonical manifest serialization.

The no-rewrite rule compares plaintext bytes, so byte stability is a contract: equal logical
manifests must serialize identically whatever the insertion order (``docs/design/03-manifest.md``).
The golden fixture pins the exact output; hypothesis pins order-independence and the round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.errors import ManifestCorrupt, UnsupportedManifestSchema
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.manifest_codec import canonical_json_bytes, dumps_manifest, loads_manifest

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "manifest_golden.json").read_text(encoding="utf-8"))

HEX32 = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)


def _record(file_id: str, sha256: str) -> ManifestRecord:
    return ManifestRecord.create(
        file_id=file_id, path=f"{file_id}.conf", preset="zsh", scope="common", format="text", sha256=sha256
    )


def _manifest(records: dict[str, ManifestRecord], generation: int = 1) -> Manifest:
    return Manifest.create(schema=MANIFEST_SCHEMA, key_generation=generation, records=records)


def _golden_manifest() -> Manifest:
    records = {
        file_id: ManifestRecord.create(file_id=file_id, **fields) for file_id, fields in GOLDEN["records"].items()
    }
    return Manifest.create(schema=MANIFEST_SCHEMA, key_generation=GOLDEN["key_generation"], records=records)


def test_dumps_matches_the_golden_bytes() -> None:
    # given
    manifest = _golden_manifest()

    # when
    encoded = dumps_manifest(manifest)

    # then
    assert encoded == GOLDEN["canonical"].encode("utf-8")
    assert encoded.hex() == GOLDEN["canonical_hex"]


def test_loads_returns_the_golden_manifest() -> None:
    # when
    parsed = loads_manifest(GOLDEN["canonical"].encode("utf-8"))

    # then
    assert parsed == _golden_manifest()


def test_canonical_bytes_have_no_trailing_newline() -> None:
    # when
    encoded = dumps_manifest(_golden_manifest())

    # then
    assert not encoded.endswith(b"\n")


def test_canonical_json_sorts_keys_and_strips_whitespace() -> None:
    # when
    encoded = canonical_json_bytes({"b": 2, "a": 1})

    # then
    assert encoded == b'{"a":1,"b":2}'


def test_canonical_json_allows_none_bool_int_list_and_dict() -> None:
    # when
    encoded = canonical_json_bytes({"nil": None, "flag": True, "count": 0, "list": [1, 2], "map": {"x": "y"}})

    # then
    assert encoded == b'{"count":0,"flag":true,"list":[1,2],"map":{"x":"y"},"nil":null}'


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1.5},
        {"a": -1},
        {"a": [1.5]},
        {1: "non-string key"},
        {"a": object()},
    ],
)
def test_canonical_json_rejects_a_non_canonical_value(value) -> None:
    # when & then
    with pytest.raises(ManifestCorrupt):
        canonical_json_bytes(value)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b"\xff\xfe", ManifestCorrupt),
        (b"{", ManifestCorrupt),
        (b"[]", ManifestCorrupt),
        (b"{}", ManifestCorrupt),
        (b'{"schema":1,"key_generation":1,"records":{},"extra":1}', ManifestCorrupt),
        (b'{"schema":1,"key_generation":1,"records":[]}', ManifestCorrupt),
        (b'{"schema":1,"key_generation":1,"records":{"0f3c4b5a69788796a5b4c3d2e1f0abcd":5}}', ManifestCorrupt),
        (b'{"schema":1,"schema":1,"key_generation":1,"records":{}}', ManifestCorrupt),
        (b'{"schema":1.5,"key_generation":1,"records":{}}', ManifestCorrupt),
        (b'{"schema":NaN,"key_generation":1,"records":{}}', ManifestCorrupt),
        (b'{"schema":"1","key_generation":1,"records":{}}', ManifestCorrupt),
        (b'{"schema":1,"key_generation":0,"records":{}}', ManifestCorrupt),
        (b'{"schema":2,"key_generation":1,"records":{}}', UnsupportedManifestSchema),
    ],
)
def test_loads_rejects_bad_input(raw, error) -> None:
    # when & then
    with pytest.raises(error):
        loads_manifest(raw)


@pytest.mark.parametrize(
    "record_json",
    [
        b'{"path":".zshrc","preset":"zsh","scope":"common","format":"text","sha256":"' + b"a" * 64 + b'","extra":1}',
        b'{"preset":"zsh","scope":"common","format":"text","sha256":"' + b"a" * 64 + b'"}',
        b'{"path":".zshrc","preset":"zsh","scope":"common","format":"text","sha256":"short"}',
    ],
)
def test_loads_rejects_a_bad_record(record_json) -> None:
    # given
    raw = b'{"schema":1,"key_generation":1,"records":{"0f3c4b5a69788796a5b4c3d2e1f0abcd":' + record_json + b"}}"

    # when & then
    with pytest.raises(ManifestCorrupt):
        loads_manifest(raw)


@given(file_ids=st.lists(HEX32, unique=True, max_size=6))
def test_canonical_bytes_are_independent_of_insertion_order(file_ids) -> None:
    # given
    records = {file_id: _record(file_id, file_id * 2) for file_id in file_ids}

    # when
    forward = dumps_manifest(_manifest(records))
    backward = dumps_manifest(_manifest(dict(reversed(list(records.items())))))

    # then
    assert forward == backward


@given(file_ids=st.lists(HEX32, unique=True, max_size=6), generation=st.integers(min_value=1, max_value=2**31))
def test_round_trip_is_identity(file_ids, generation) -> None:
    # given
    manifest = _manifest({file_id: _record(file_id, file_id * 2) for file_id in file_ids}, generation=generation)

    # when
    parsed = loads_manifest(dumps_manifest(manifest))

    # then
    assert parsed == manifest
