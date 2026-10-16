# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manifest: the encrypted index of the repo.

A manifest is a record map keyed by `file_id`, plus the schema version and the key generation
new objects are encrypted with (``docs/design/03-manifest.md``). Both models are frozen and are
built by a single classmethod that validates every field (``AGENTS.md`` §2.7); the record map
is a ``MappingProxyType`` so it cannot be mutated after construction.

Standard library only — this module is the pure shape, the canonical JSON codec lives in
:mod:`syncintosh.core.manifest_codec`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from syncintosh.core.errors import ManifestCorrupt, UnsupportedManifestSchema
from syncintosh.core.ids import is_file_id
from syncintosh.core.models import Scope
from syncintosh.core.ports import FileIdSource

# Bumping this is a migration, not a routine change (docs/design/03-manifest.md).
MANIFEST_SCHEMA = 1

# The adapter key for a record's content, serialized verbatim, so it is a `Literal` and never
# an `Enum` (``AGENTS.md`` §4).
FileFormat = Literal["text", "jsonc", "toml", "yaml", "xml", "plist", "binary"]

FORMATS: tuple[FileFormat, ...] = ("text", "jsonc", "toml", "yaml", "xml", "plist", "binary")
_SCOPES: tuple[Scope, ...] = ("common", "machine")

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _is_plain_int(value: object) -> bool:
    """True for an ``int`` that is not a ``bool`` (``bool`` is an ``int`` subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ManifestCorrupt(f"sha256 must be 64 lowercase hex characters, got {value!r}")
    return value


def _ensure_utf8(value: str, field: str) -> str:
    """Reject a string that cannot be encoded as UTF-8 (an unpaired surrogate).

    Such a string would pass field validation and then blow up as a bare `UnicodeEncodeError`
    when the manifest is serialized, so it is turned into a `ManifestCorrupt` here.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ManifestCorrupt(f"{field} is not valid UTF-8: {value!r}") from exc
    return value


def _validate_path(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ManifestCorrupt(f"path must be a non-empty string, got {value!r}")
    if "\x00" in value:
        raise ManifestCorrupt("path must not contain a NUL byte")
    _ensure_utf8(value, "path")
    # Relative to $HOME so the manifest is portable across Macs. Reject a non-normalized path
    # rather than normalizing silently: `.`, `..`, `//`, or a trailing `/` would otherwise mint a
    # second ID for one file and no dedup rule would catch it. Apply still re-resolves the target
    # (a symlinked parent) as its own trust-boundary check.
    if value.startswith("/"):
        raise ManifestCorrupt(f"path must be relative to $HOME, got {value!r}")
    components = value.split("/")
    if "" in components or "." in components or ".." in components:
        raise ManifestCorrupt(f"path must be normalized (no '.', '..', empty, or trailing '/'): {value!r}")
    return value


def _validate_preset(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ManifestCorrupt(f"preset must be a non-empty string, got {value!r}")
    return _ensure_utf8(value, "preset")


def _validate_scope(value: object) -> Scope:
    if value not in _SCOPES:
        raise ManifestCorrupt(f"scope must be one of {_SCOPES}, got {value!r}")
    return value  # ty: ignore[invalid-return-type]  # narrowed by the membership check above


def _validate_machine(value: object, scope: Scope) -> str | None:
    if scope == "machine":
        if not isinstance(value, str) or value == "":
            raise ManifestCorrupt("a machine-scope record must name its machine")
        return _ensure_utf8(value, "machine")
    if value is not None:
        raise ManifestCorrupt("a common-scope record must not name a machine")
    return None


def _validate_format(value: object) -> FileFormat:
    if value not in FORMATS:
        raise ManifestCorrupt(f"format must be one of {FORMATS}, got {value!r}")
    return value  # ty: ignore[invalid-return-type]  # narrowed by the membership check above


@dataclass(frozen=True)
class ManifestRecord:
    """One tracked file: its stable ID, where it lives, and the digest of its plaintext."""

    file_id: str
    path: str
    preset: str
    scope: Scope
    machine: str | None
    format: FileFormat
    sha256: str

    @classmethod
    def create(
        cls,
        *,
        file_id: str,
        path: str,
        preset: str,
        scope: Scope,
        machine: str | None = None,
        format: FileFormat,
        sha256: str,
    ) -> ManifestRecord:
        """Validate every field and build the frozen record; never assign fields afterwards."""
        if not isinstance(file_id, str) or not is_file_id(file_id):
            raise ManifestCorrupt(f"file_id must be 32 lowercase hex characters, got {file_id!r}")
        validated_scope = _validate_scope(scope)
        return cls(
            file_id=file_id,
            path=_validate_path(path),
            preset=_validate_preset(preset),
            scope=validated_scope,
            machine=_validate_machine(machine, validated_scope),
            format=_validate_format(format),
            sha256=_validate_sha256(sha256),
        )


@dataclass(frozen=True)
class Manifest:
    """The whole index: schema, the generation objects use, and the record map."""

    schema: int
    key_generation: int
    records: Mapping[str, ManifestRecord]

    @classmethod
    def create(
        cls,
        *,
        schema: int,
        key_generation: int,
        records: Mapping[str, ManifestRecord],
    ) -> Manifest:
        """Validate the manifest-wide fields and freeze the record map."""
        if not _is_plain_int(schema):
            raise ManifestCorrupt(f"schema must be an integer, got {type(schema).__name__}")
        if schema != MANIFEST_SCHEMA:
            raise UnsupportedManifestSchema(schema)
        if not _is_plain_int(key_generation) or key_generation < 1:
            raise ManifestCorrupt(f"key_generation must be a positive integer, got {key_generation!r}")
        mismatched = sorted(key for key, record in records.items() if key != record.file_id)
        if mismatched:
            raise ManifestCorrupt(f"record map keys do not match their file_id: {mismatched}")
        return cls(schema=schema, key_generation=key_generation, records=MappingProxyType(dict(records)))

    @classmethod
    def empty(cls, key_generation: int) -> Manifest:
        """A manifest with no records — the state before the first `track`."""
        return cls.create(schema=MANIFEST_SCHEMA, key_generation=key_generation, records={})


def _tracking_key(record: ManifestRecord) -> tuple[Scope, str | None, str]:
    return (record.scope, record.machine, record.path)


def track(
    manifest: Manifest,
    *,
    path: str,
    preset: str,
    scope: Scope,
    machine: str | None = None,
    format: FileFormat,
    sha256: str,
    source: FileIdSource,
) -> Manifest:
    """Return a new manifest with one record added or refreshed.

    The file ID is minted **once** per `(scope, machine, path)` and reused forever: an already
    tracked path keeps its existing ID so the object slot never moves (``docs/design/03-manifest.md``).
    A freshly minted ID is re-checked here — a bad or colliding ID is a source-contract bug, a
    `ValueError`, not a corrupt manifest.
    """
    key = (scope, machine, path)
    existing = next((record for record in manifest.records.values() if _tracking_key(record) == key), None)
    file_id = existing.file_id if existing is not None else _mint_file_id(manifest, source)
    record = ManifestRecord.create(
        file_id=file_id,
        path=path,
        preset=preset,
        scope=scope,
        machine=machine,
        format=format,
        sha256=sha256,
    )
    records = {**manifest.records, record.file_id: record}
    return Manifest.create(schema=manifest.schema, key_generation=manifest.key_generation, records=records)


def _mint_file_id(manifest: Manifest, source: FileIdSource) -> str:
    file_id = source.new_file_id()
    if not isinstance(file_id, str) or not is_file_id(file_id):
        raise ValueError(f"the file ID source returned a malformed id: {file_id!r}")
    if file_id in manifest.records:
        raise ValueError(f"the file ID source returned an id already in the manifest: {file_id!r}")
    return file_id
