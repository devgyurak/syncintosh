# SPDX-License-Identifier: AGPL-3.0-or-later
"""Record-level manifest merge, keyed by `file_id`.

The first of the two merges (``docs/design/03-manifest.md``): it decides which records survive
and which are in conflict, and **routes content changes to the content merge rather than
deciding them**. Because `sha256` is part of the record, a content-only change still counts as
a change, so a side that edits a file and a side that deletes it collide as delete/modify.

`base` may be `None`, meaning `base = ∅` — the first-connect flow (``docs/design/02-base-and-merge.md``).
Pure: it takes and returns plain values (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from syncintosh.core.errors import GenerationDivergence
from syncintosh.core.manifest import FileFormat, Manifest, ManifestRecord
from syncintosh.core.models import Scope

# The three record-level conflicts; the kind decides the user's choice
# (docs/design/03-manifest.md).
RecordConflictKind = Literal["add/add", "delete/modify", "modify/modify"]

# Metadata fields compared one at a time when both sides changed. `sha256` is deliberately not
# in this list: only the content merge may decide a both-sides content change.
_METADATA_FIELDS = ("path", "preset", "scope", "machine", "format")


@dataclass(frozen=True)
class RecordConflict:
    """A record that needs a human (or LLM) choice; `base` is `None` for an add/add."""

    kind: RecordConflictKind
    file_id: str
    base: ManifestRecord | None
    local: ManifestRecord | None
    remote: ManifestRecord | None


@dataclass(frozen=True)
class PathCollision:
    """Two or more surviving records share `(scope, machine, path)` with different content.

    File IDs are random, so two machines that tracked one path before sharing history mint two
    IDs for it; the secondary key catches that (``docs/design/02-base-and-merge.md``).
    """

    scope: Scope
    machine: str | None
    path: str
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class ContentMergeTask:
    """A record whose metadata merged but whose content changed on both sides.

    It is **not** placed in the merged result: its digest is unknown until the adapter runs
    `merge(base_plain, local_plain, remote_plain)`. Call `complete(resolved_sha256)` to finish
    the record once the adapter has produced the merged content.
    """

    file_id: str
    base_sha256: str
    local_sha256: str
    remote_sha256: str
    path: str
    preset: str
    scope: Scope
    machine: str | None
    format: FileFormat

    def complete(self, sha256: str) -> ManifestRecord:
        """Build the surviving record from the adapter's merged-content digest."""
        return ManifestRecord.create(
            file_id=self.file_id,
            path=self.path,
            preset=self.preset,
            scope=self.scope,
            machine=self.machine,
            format=self.format,
            sha256=sha256,
        )


@dataclass(frozen=True)
class RecordMergeResult:
    """The surviving records, the conflicts, the collisions, and the pending content merges."""

    merged: Mapping[str, ManifestRecord]
    conflicts: tuple[RecordConflict, ...]
    collisions: tuple[PathCollision, ...]
    content_merges: tuple[ContentMergeTask, ...]
    deletions: tuple[str, ...]

    @property
    def has_conflicts(self) -> bool:
        """True when at least one record or path needs a decision, so `apply` must not run."""
        return bool(self.conflicts or self.collisions)


def _pick[T](base_value: T, local_value: T, remote_value: T) -> tuple[T, bool]:
    """Three-way pick for one field: `(value, changed_on_both)`.

    `changed_on_both` is True only when both sides differ from base **and** from each other —
    the one case a single path cannot resolve automatically.
    """
    if local_value == remote_value:
        return local_value, False
    if local_value == base_value:
        return remote_value, False
    if remote_value == base_value:
        return local_value, False
    return local_value, True


def merge_records(
    base: Mapping[str, ManifestRecord] | None,
    local: Mapping[str, ManifestRecord],
    remote: Mapping[str, ManifestRecord],
) -> RecordMergeResult:
    """Merge record maps keyed by `file_id` (``base = None`` means an empty base).

    `base`/`local`/`remote` may be a `Manifest.records` map or any record mapping. A record
    whose content changed on both sides while its metadata merged cleanly is returned as a
    `ContentMergeTask`, not as a conflict — only the content merge decides content.
    """
    base_records: Mapping[str, ManifestRecord] = base or {}
    merged: dict[str, ManifestRecord] = {}
    conflicts: list[RecordConflict] = []
    content_merges: list[ContentMergeTask] = []
    deletions: list[str] = []

    for file_id in sorted({*base_records, *local, *remote}):
        base_record = base_records.get(file_id)
        local_record = local.get(file_id)
        remote_record = remote.get(file_id)

        if base_record is None:
            present = [record for record in (local_record, remote_record) if record is not None]
            _merge_addition(file_id, present, merged, conflicts)
            continue
        if local_record is None or remote_record is None:
            _merge_deletion(file_id, base_record, local_record, remote_record, merged, conflicts, deletions)
            continue
        if local_record == remote_record:
            merged[file_id] = local_record
        elif local_record == base_record:
            merged[file_id] = remote_record
        elif remote_record == base_record:
            merged[file_id] = local_record
        else:
            _merge_both_changed(file_id, base_record, local_record, remote_record, merged, conflicts, content_merges)

    collisions = _resolve_collisions(merged, deletions)
    return RecordMergeResult(
        merged=MappingProxyType(merged),
        conflicts=tuple(conflicts),
        collisions=collisions,
        content_merges=tuple(content_merges),
        deletions=tuple(sorted(deletions)),
    )


def merge_manifests(base: Manifest | None, local: Manifest, remote: Manifest) -> RecordMergeResult:
    """Merge two manifests, refusing to reconcile a key-generation divergence.

    Rotation is fast-forward only and is never merged (``docs/design/05-key-generations.md``),
    so `local` and `remote` must already agree on `key_generation`; differing generations raise
    `GenerationDivergence` rather than producing a record merge.
    """
    if local.key_generation != remote.key_generation:
        raise GenerationDivergence(local.key_generation, remote.key_generation)
    base_records = None if base is None else base.records
    return merge_records(base_records, local.records, remote.records)


def _merge_addition(
    file_id: str,
    present: list[ManifestRecord],
    merged: dict[str, ManifestRecord],
    conflicts: list[RecordConflict],
) -> None:
    """`base = ∅` for this id: take the present side(s), or flag an add/add conflict.

    `present` holds the local and/or remote record (the id is in the union, so it is never
    empty). Both sides present with the same record is a one-record result; different records
    are an `add/add` conflict — with no base there is nothing to merge.
    """
    if len(present) == 2 and present[0] != present[1]:
        conflicts.append(
            RecordConflict(kind="add/add", file_id=file_id, base=None, local=present[0], remote=present[1])
        )
        return
    merged[file_id] = present[0]


def _merge_deletion(
    file_id: str,
    base: ManifestRecord,
    local: ManifestRecord | None,
    remote: ManifestRecord | None,
    merged: dict[str, ManifestRecord],
    conflicts: list[RecordConflict],
    deletions: list[str],
) -> None:
    """At least one side is absent: delete, or flag a delete/modify conflict."""
    if local is None and remote is None:
        deletions.append(file_id)
        return
    if local is None:
        if remote == base:
            deletions.append(file_id)
        else:
            conflicts.append(
                RecordConflict(kind="delete/modify", file_id=file_id, base=base, local=None, remote=remote)
            )
        return
    if local == base:
        deletions.append(file_id)
    else:
        conflicts.append(RecordConflict(kind="delete/modify", file_id=file_id, base=base, local=local, remote=None))


def _merge_both_changed(
    file_id: str,
    base: ManifestRecord,
    local: ManifestRecord,
    remote: ManifestRecord,
    merged: dict[str, ManifestRecord],
    conflicts: list[RecordConflict],
    content_merges: list[ContentMergeTask],
) -> None:
    """Both sides changed differently: 3-way the metadata, then route the digest.

    A metadata field both sides changed differently is a `modify/modify` conflict. A digest
    both sides changed differently is a content merge, never a record decision.
    """
    resolved = {
        field: _pick(getattr(base, field), getattr(local, field), getattr(remote, field)) for field in _METADATA_FIELDS
    }
    if any(changed for _, changed in resolved.values()):
        conflicts.append(RecordConflict(kind="modify/modify", file_id=file_id, base=base, local=local, remote=remote))
        return

    sha256, sha_changed_on_both = _pick(base.sha256, local.sha256, remote.sha256)
    if sha_changed_on_both:
        content_merges.append(
            ContentMergeTask(
                file_id=file_id,
                base_sha256=base.sha256,
                local_sha256=local.sha256,
                remote_sha256=remote.sha256,
                path=resolved["path"][0],
                preset=resolved["preset"][0],
                scope=resolved["scope"][0],
                machine=resolved["machine"][0],
                format=resolved["format"][0],
            )
        )
        return

    merged[file_id] = ManifestRecord.create(
        file_id=file_id,
        path=resolved["path"][0],
        preset=resolved["preset"][0],
        scope=resolved["scope"][0],
        machine=resolved["machine"][0],
        format=resolved["format"][0],
        sha256=sha256,
    )


def _resolve_collisions(merged: dict[str, ManifestRecord], deletions: list[str]) -> tuple[PathCollision, ...]:
    """Apply the secondary key `(scope, machine, path)` over the merged records.

    Identical content is deduplicated silently, keeping the lexicographically smallest ID and
    dropping the rest; differing content is a `PathCollision`. Machine-scope records on
    different machines carry different keys, so they never collide.
    """
    groups: dict[tuple[Scope, str | None, str], list[str]] = {}
    for file_id, record in merged.items():
        groups.setdefault((record.scope, record.machine, record.path), []).append(file_id)

    collisions: list[PathCollision] = []
    for key in sorted(groups, key=lambda item: (item[0], item[1] or "", item[2])):
        file_ids = sorted(groups[key])
        if len(file_ids) < 2:
            continue
        if _all_identical(merged, file_ids):
            deletions.extend(file_ids[1:])
            for loser in file_ids[1:]:
                del merged[loser]
        else:
            scope, machine, path = key
            collisions.append(PathCollision(scope=scope, machine=machine, path=path, file_ids=tuple(file_ids)))
    return tuple(collisions)


def _all_identical(merged: Mapping[str, ManifestRecord], file_ids: list[str]) -> bool:
    """True when every record matches the first in all fields but the ID (content + provenance)."""
    first = _content_identity(merged[file_ids[0]])
    return all(_content_identity(merged[file_id]) == first for file_id in file_ids[1:])


def _content_identity(record: ManifestRecord) -> tuple[str, str, Scope, str | None, FileFormat, str]:
    """A record's fields other than `file_id` — what "identical content" means for a dedup."""
    return (record.path, record.preset, record.scope, record.machine, record.format, record.sha256)
