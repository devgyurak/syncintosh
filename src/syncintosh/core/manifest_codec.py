# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical JSON for the manifest — a byte-stability invariant.

The no-rewrite rule compares plaintext bytes (``docs/design/01-sync-model.md``), so the same
logical manifest must always serialize to the same bytes whatever order the records were
inserted in. Serialization is therefore canonical JSON, UTF-8, with **no trailing newline**
(``docs/design/03-manifest.md``):

* keys sorted by code point, no insignificant whitespace, ``ensure_ascii=False``;
* values restricted to ``str``, non-negative ``int``, ``bool``, ``None``, ``list``, and
  ``dict`` — floats and negative ints are rejected;
* parsing rejects duplicate keys, floats, unknown fields, and an unsupported schema.

Standard library ``json`` only, so this lives in ``core`` (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from syncintosh.core.canonical import canonical_json_bytes
from syncintosh.core.errors import ManifestCorrupt, UnsupportedManifestSchema
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord, validate_preset
from syncintosh.core.policy import STAMP_KEYS, PolicyStamp

# Top-level keys: all required, nothing else allowed.
_MANIFEST_KEYS = frozenset({"schema", "key_generation", "records", "policy"})
# Schema 1 had no `policy`; it is read as schema 2 with none (every preset then reads as revision 1).
_MANIFEST_KEYS_V1 = frozenset({"schema", "key_generation", "records"})
_LEGACY_SCHEMA = 1
# Record keys: `machine` is optional (required only for machine scope, which the model checks).
_RECORD_KEYS = frozenset({"path", "preset", "scope", "machine", "format", "sha256"})


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`object_pairs_hook`: a duplicate JSON key would silently drop an earlier value."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestCorrupt(f"duplicate key {key!r} in manifest JSON")
        result[key] = value
    return result


def _reject_float(token: str) -> Any:
    """`parse_float`: nothing in the manifest is a float, so one is a corrupt manifest."""
    raise ManifestCorrupt(f"manifest JSON may not contain a float, got {token}")


def _reject_constant(token: str) -> Any:
    """`parse_constant`: reject `NaN`, `Infinity`, and `-Infinity`."""
    raise ManifestCorrupt(f"manifest JSON may not contain the constant {token}")


def manifest_to_mapping(manifest: Manifest) -> dict[str, Any]:
    """The plain nested mapping a manifest serializes as (`machine` omitted when absent)."""
    records: dict[str, Any] = {}
    for file_id, record in manifest.records.items():
        entry: dict[str, Any] = {
            "path": record.path,
            "preset": record.preset,
            "scope": record.scope,
            "format": record.format,
            "sha256": record.sha256,
        }
        if record.machine is not None:
            entry["machine"] = record.machine
        records[file_id] = entry
    policy = {name: {"rev": stamp.rev, "digest": stamp.digest} for name, stamp in manifest.policy.items()}
    return {"schema": manifest.schema, "key_generation": manifest.key_generation, "records": records, "policy": policy}


def dumps_manifest(manifest: Manifest) -> bytes:
    """Canonical bytes for `manifest` — the plaintext that is sealed into `manifest.enc`."""
    return canonical_json_bytes(manifest_to_mapping(manifest))


def _require_keys(mapping: Mapping[str, Any], required: frozenset[str], allowed: frozenset[str], where: str) -> None:
    missing = sorted(required - mapping.keys())
    if missing:
        raise ManifestCorrupt(f"{where} is missing required fields: {missing}")
    unknown = sorted(mapping.keys() - allowed)
    if unknown:
        raise ManifestCorrupt(f"{where} has unknown fields: {unknown}")


def _parse_record(file_id: str, raw: object) -> ManifestRecord:
    if not isinstance(raw, dict):
        raise ManifestCorrupt(f"record {file_id!r} must be a JSON object")
    _require_keys(raw, _RECORD_KEYS - {"machine"}, _RECORD_KEYS, f"record {file_id!r}")
    return ManifestRecord.create(
        file_id=file_id,
        path=raw["path"],
        preset=raw["preset"],
        scope=raw["scope"],
        machine=raw.get("machine"),
        format=raw["format"],
        sha256=raw["sha256"],
    )


def _parse_policy(raw: object) -> dict[str, PolicyStamp]:
    if not isinstance(raw, dict):
        raise ManifestCorrupt("manifest `policy` must be a JSON object")
    stamps: dict[str, PolicyStamp] = {}
    for name, entry in raw.items():
        validate_preset(name)
        if not isinstance(entry, dict):
            raise ManifestCorrupt(f"policy {name!r} must be a JSON object")
        _require_keys(entry, STAMP_KEYS, STAMP_KEYS, f"policy {name!r}")
        stamps[name] = PolicyStamp.create(rev=entry["rev"], digest=entry["digest"])
    return stamps


def loads_manifest(data: bytes) -> Manifest:
    """Parse canonical manifest bytes, rejecting anything the model would not have written.

    Rejects a non-UTF-8 payload, a duplicate key, a float, an unknown field, and a schema this
    build does not understand. A failure is a `ManifestError` (subclass), never a bare
    `ValueError`.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestCorrupt("manifest is not valid UTF-8") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ManifestCorrupt(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ManifestCorrupt("manifest must be a JSON object")
    # Check `schema` before any field: a newer schema is an upgrade, not corruption, so it must
    # not be masked by a field a later version added or reshaped.
    schema = parsed.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise ManifestCorrupt(f"manifest schema must be an integer, got {type(schema).__name__}")
    if schema not in (MANIFEST_SCHEMA, _LEGACY_SCHEMA):
        raise UnsupportedManifestSchema(schema)
    legacy = schema == _LEGACY_SCHEMA
    expected = _MANIFEST_KEYS_V1 if legacy else _MANIFEST_KEYS
    _require_keys(parsed, expected, expected, "manifest")
    records_raw = parsed["records"]
    if not isinstance(records_raw, dict):
        raise ManifestCorrupt("manifest `records` must be a JSON object")
    records = {file_id: _parse_record(file_id, raw) for file_id, raw in records_raw.items()}
    # A schema-1 manifest is lifted to the current schema in memory. The next write persists it, so a
    # repo upgrades by being synced, never by a separate migration command.
    policy = {} if legacy else _parse_policy(parsed["policy"])
    return Manifest.create(
        schema=MANIFEST_SCHEMA, key_generation=parsed["key_generation"], records=records, policy=policy
    )


__all__ = [
    "MANIFEST_SCHEMA",
    "canonical_json_bytes",
    "dumps_manifest",
    "loads_manifest",
    "manifest_to_mapping",
]
