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
from syncintosh.core.paths import collision_key
from syncintosh.core.resolution import CollisionChoice, CollisionKey, RecordChoice

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
    # An explicit `untrack` (a record gone from the base), distinct from a dedup loser.
    deleted: tuple[str, ...]
    # A duplicate ID dropped because an identical-content record for the same path survived.
    deduplicated: tuple[str, ...]

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
    *,
    choices: Mapping[str, RecordChoice] | None = None,
    collision_choices: Mapping[CollisionKey, CollisionChoice] | None = None,
) -> RecordMergeResult:
    """Merge record maps keyed by `file_id` (``base = None`` means an empty base).

    `base`/`local`/`remote` may be a `Manifest.records` map or any record mapping. A record
    whose content changed on both sides while its metadata merged cleanly is returned as a
    `ContentMergeTask`, not as a conflict — only the content merge decides content.

    `choices` settles a record conflict by its `file_id`; `collision_choices` settles a
    `PathCollision` by its `CollisionKey`. A choice for something that is not a conflict or a
    collision is ignored. Settling a record can dissolve a collision, so the collisions are
    recomputed after the record choices, and the collision choices apply to what remains.
    """
    base_records: Mapping[str, ManifestRecord] = base or {}
    merged: dict[str, ManifestRecord] = {}
    conflicts: list[RecordConflict] = []
    content_merges: list[ContentMergeTask] = []
    deleted: list[str] = []

    for file_id in sorted({*base_records, *local, *remote}):
        base_record = base_records.get(file_id)
        local_record = local.get(file_id)
        remote_record = remote.get(file_id)

        if base_record is None:
            present = [record for record in (local_record, remote_record) if record is not None]
            _merge_addition(file_id, present, merged, conflicts)
            continue
        if local_record is None or remote_record is None:
            _merge_deletion(file_id, base_record, local_record, remote_record, merged, conflicts, deleted)
            continue
        if local_record == remote_record:
            merged[file_id] = local_record
        elif local_record == base_record:
            merged[file_id] = remote_record
        elif remote_record == base_record:
            merged[file_id] = local_record
        else:
            _merge_both_changed(file_id, base_record, local_record, remote_record, merged, conflicts, content_merges)

    picks = choices or {}
    unsettled: list[RecordConflict] = []
    for conflict in conflicts:
        if conflict.file_id in picks:
            _settle_conflict(conflict, picks[conflict.file_id], merged, deleted, content_merges)
        else:
            unsettled.append(conflict)
    conflicts = unsettled

    deduplicated: list[str] = []
    collisions = _resolve_collisions(merged, conflicts, content_merges, deduplicated)
    chosen = collision_choices or {}
    settled = [collision for collision in collisions if collision_key_of(collision) in chosen]
    if settled:
        losers = {
            file_id
            for collision in settled
            for file_id in _losers(collision, chosen[collision_key_of(collision)], local, remote)
        }
        for file_id in losers:
            merged.pop(file_id, None)
        content_merges = [task for task in content_merges if task.file_id not in losers]
        conflicts = [conflict for conflict in conflicts if conflict.file_id not in losers]
        collisions = _resolve_collisions(merged, conflicts, content_merges, deduplicated)

    return RecordMergeResult(
        merged=MappingProxyType(merged),
        conflicts=tuple(conflicts),
        collisions=collisions,
        content_merges=tuple(content_merges),
        deleted=tuple(sorted(deleted)),
        deduplicated=tuple(sorted(deduplicated)),
    )


def record_path_of(conflict: RecordConflict) -> str:
    """The path a record conflict is about: the first side that holds a record, or empty."""
    record = conflict.local or conflict.remote or conflict.base
    return "" if record is None else record.path


def _settle_conflict(
    conflict: RecordConflict,
    choice: RecordChoice,
    merged: dict[str, ManifestRecord],
    deleted: list[str],
    content_merges: list[ContentMergeTask],
) -> None:
    """Apply one record choice: the chosen side's record survives, or the path is untracked.

    A modify/modify takes the chosen side's metadata only. The digest is still decided by the
    three-way rule, so a content change the other side made survives the choice; only a content
    change on both sides goes to the content merge (``docs/design/03-manifest.md``). A choice that
    does not fit the conflict's kind is a caller error, not something to guess at.
    """
    if conflict.kind == "delete/modify":
        if choice == "delete":
            deleted.append(conflict.file_id)
            return
        if choice != "keep":
            raise ValueError(f"a delete/modify conflict takes keep or delete, not {choice!r}")
        survivor = conflict.local if conflict.local is not None else conflict.remote
        if survivor is not None:
            merged[conflict.file_id] = survivor
        return
    if choice == "drop" and conflict.kind == "add/add":
        return
    if choice not in ("local", "remote"):
        raise ValueError(f"a {conflict.kind} conflict takes local or remote, not {choice!r}")
    picked = conflict.local if choice == "local" else conflict.remote
    if picked is None:
        return
    if conflict.kind == "modify/modify" and conflict.base is not None and conflict.local and conflict.remote:
        _with_picked_metadata(
            conflict.file_id, conflict.base, conflict.local, conflict.remote, picked, merged, content_merges
        )
        return
    merged[conflict.file_id] = picked


def _with_picked_metadata(
    file_id: str,
    base: ManifestRecord,
    local: ManifestRecord,
    remote: ManifestRecord,
    picked: ManifestRecord,
    merged: dict[str, ManifestRecord],
    content_merges: list[ContentMergeTask],
) -> None:
    """The picked side's metadata, with the digest decided by the three-way rule."""
    sha256, changed_on_both = _pick(base.sha256, local.sha256, remote.sha256)
    if changed_on_both:
        content_merges.append(
            ContentMergeTask(
                file_id=file_id,
                base_sha256=base.sha256,
                local_sha256=local.sha256,
                remote_sha256=remote.sha256,
                path=picked.path,
                preset=picked.preset,
                scope=picked.scope,
                machine=picked.machine,
                format=picked.format,
            )
        )
        return
    merged[file_id] = ManifestRecord.create(
        file_id=file_id,
        path=picked.path,
        preset=picked.preset,
        scope=picked.scope,
        machine=picked.machine,
        format=picked.format,
        sha256=sha256,
    )


def collision_key_of(collision: PathCollision) -> CollisionKey:
    """The key a collision is settled under: the same folded key the record merge groups by."""
    return (collision.scope, collision.machine, collision_key(collision.path))


def _losers(
    collision: PathCollision,
    choice: CollisionChoice,
    local: Mapping[str, ManifestRecord],
    remote: Mapping[str, ManifestRecord],
) -> tuple[str, ...]:
    """The file IDs a collision choice drops; the survivor is the smallest ID the chosen side holds.

    A side that holds none of the colliding IDs leaves the path untracked, the same as `drop`.
    """
    if choice == "drop":
        return collision.file_ids
    side = local if choice == "local" else remote
    survivors = [file_id for file_id in collision.file_ids if file_id in side]
    keep = min(survivors) if survivors else None
    return tuple(file_id for file_id in collision.file_ids if file_id != keep)


def merge_manifests(
    base: Manifest | None,
    local: Manifest,
    remote: Manifest,
    *,
    choices: Mapping[str, RecordChoice] | None = None,
    collision_choices: Mapping[CollisionKey, CollisionChoice] | None = None,
) -> RecordMergeResult:
    """Merge two manifests, refusing to reconcile a key-generation divergence.

    Rotation is fast-forward only and is never merged (``docs/design/05-key-generations.md``),
    so `local` and `remote` must already agree on `key_generation`; differing generations raise
    `GenerationDivergence` rather than producing a record merge.
    """
    if local.key_generation != remote.key_generation:
        raise GenerationDivergence(local.key_generation, remote.key_generation)
    base_records = None if base is None else base.records
    return merge_records(
        base_records, local.records, remote.records, choices=choices, collision_choices=collision_choices
    )


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
    deleted: list[str],
) -> None:
    """At least one side is absent: delete, or flag a delete/modify conflict."""
    if local is None and remote is None:
        deleted.append(file_id)
        return
    if local is None:
        if remote == base:
            deleted.append(file_id)
        else:
            conflicts.append(
                RecordConflict(kind="delete/modify", file_id=file_id, base=base, local=None, remote=remote)
            )
        return
    if local == base:
        deleted.append(file_id)
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


def _secondary_key(record: ManifestRecord) -> tuple[Scope, str | None, str]:
    # Folded the same way `track` folds it, so `.Zshrc` and `.zshrc` collide on APFS.
    return (record.scope, record.machine, collision_key(record.path))


def _resolve_collisions(
    merged: dict[str, ManifestRecord],
    conflicts: list[RecordConflict],
    content_merges: list[ContentMergeTask],
    deduplicated: list[str],
) -> tuple[PathCollision, ...]:
    """Apply the secondary key `(scope, machine, path)` over every candidate survivor.

    Candidates are the merged records **plus** the records still pending a content merge or a
    conflict resolution. Leaving the latter out would let one path end up with two records once
    the task completes — exactly what the secondary key exists to stop
    (``docs/design/02-base-and-merge.md``). A conflict contributes **both** sides and never its
    base (a conflict resolves to local or remote, so base cannot survive), which also keeps the
    grouping symmetric in local and remote so both machines converge. Identical content among
    resolved records is deduplicated silently (smallest ID wins); anything else is a
    `PathCollision`. Machine-scope records on different machines carry different keys.

    The key is folded (NFC + casefold) so two spellings of one APFS path collide, while the
    collision still reports a real path for the user to recognise.
    """
    # `(file_id, record, resolved, display path)`; the display path is the path as written on
    # whichever side contributed the entry, never the folded key.
    groups: dict[tuple[Scope, str | None, str], list[tuple[str, ManifestRecord | None, bool, str]]] = {}
    for file_id, record in merged.items():
        groups.setdefault(_secondary_key(record), []).append((file_id, record, True, record.path))
    for task in content_merges:
        groups.setdefault((task.scope, task.machine, collision_key(task.path)), []).append(
            (task.file_id, None, False, task.path)
        )
    for conflict in conflicts:
        for record in (conflict.local, conflict.remote):
            if record is not None:
                groups.setdefault(_secondary_key(record), []).append((record.file_id, record, False, record.path))

    collisions: list[PathCollision] = []
    for key in sorted(groups, key=lambda item: (item[0], item[1] or "", item[2])):
        entries = groups[key]
        file_ids = sorted({file_id for file_id, _, _, _ in entries})
        if len(file_ids) < 2:
            continue
        resolved_records = [record for _, record, resolved, _ in entries if resolved and record is not None]
        if all(resolved for _, _, resolved, _ in entries) and _all_identical(resolved_records):
            for loser in file_ids[1:]:
                merged.pop(loser, None)
                deduplicated.append(loser)
            continue
        scope, machine, _folded = key
        collisions.append(PathCollision(scope=scope, machine=machine, path=entries[0][3], file_ids=tuple(file_ids)))
    return tuple(collisions)


def _all_identical(records: list[ManifestRecord]) -> bool:
    """True when every record matches the first in all fields but the ID (content + provenance)."""
    first = _content_identity(records[0])
    return all(_content_identity(record) == first for record in records[1:])


def _content_identity(record: ManifestRecord) -> tuple[str, str, Scope, str | None, FileFormat, str]:
    """A record's fields other than `file_id` — what "identical content" means for a dedup."""
    return (record.path, record.preset, record.scope, record.machine, record.format, record.sha256)
