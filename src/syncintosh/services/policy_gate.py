# SPDX-License-Identifier: AGPL-3.0-or-later
"""The policy gate: one place that compares a repo's preset policy with this build's (issue #24).

Two builds can disagree about what a preset strips. The manifest stamps each built-in preset with the
policy its objects were made under (``docs/design/03-manifest.md``, "Policy stamp"), and `PolicyView`
reads the repo through that comparison, so no code path past it has to ask:

* `match` — nothing changes;
* `older` — the objects are re-projected **in memory** under the local policy (the static strip only; a
  carry belongs to a live file, not to a stored object), re-sealed, and served in place of the stored
  ones, with the stamp raised to the local one. The view is what a later step merges, publishes, or
  applies, so the repo converges on the next write;
* `newer` / `unknown` — `PolicyMismatch`: the cycle stops before it merges, publishes, or applies.

It is a `GitTransport` that only changes what `read_blob` returns for `manifest.enc` and for the objects
a migration touched. Every reader — the cycle, the merge, apply, the ledger — therefore sees one
consistent repo, and the digest check that guards each object read keeps working. A write is planned
against the **stored** manifest (`PolicyView.raw`), never the view, or a migrated object would look
unchanged and never be written.

The stamp is a claim by whoever wrote the repo, not a trust boundary. `apply` keeps its own projection check as
defense in depth; it runs only when a live file exists to restore into, and closing that gap is the next PR (C).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from syncintosh.core.errors import ContentDigestMismatch, ManifestCorrupt, PolicyMismatch
from syncintosh.core.manifest import MANIFEST_SCHEMA, FileFormat, Manifest, ManifestRecord
from syncintosh.core.manifest_codec import dumps_manifest, loads_manifest
from syncintosh.core.policy import NEWER, OLDER, UNKNOWN, policy_verdicts, stamp_policies
from syncintosh.core.ports import Cipher, GitTransport, PresetCatalog, PushOutcome, SecretScanner
from syncintosh.core.repo import MANIFEST_PATH, envelope_key_id, object_file_id, object_path

# Reduces stored bytes to the local policy's static strip, or raises `ProjectionError`. The cycle supplies
# it (it owns the adapters), so this module names neither an adapter nor `services.sync`.
Project = Callable[[bytes, str, FileFormat], bytes]


@dataclass(frozen=True)
class Migration:
    """What one commit's older policy became under the local one."""

    manifest_blob: bytes
    # Only the objects whose bytes changed; a record whose projection is the stored bytes is served as stored.
    objects: Mapping[str, bytes] = field(repr=False)
    key_ids: Mapping[str, int]
    steps: tuple[tuple[str, int, int], ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PolicyView:
    """A `GitTransport` that serves the repo as the local policy would have written it."""

    def __init__(
        self,
        inner: GitTransport,
        *,
        cipher: Cipher,
        catalog: PresetCatalog,
        project: Project,
        scanner: SecretScanner | None,
        strict: bool = True,
    ) -> None:
        self.raw = inner
        self._cipher = cipher
        self._catalog = catalog
        self._project = project
        self._scanner = scanner
        self._strict = strict
        self._migrations: dict[str, Migration | None] = {}
        self._mismatches: dict[str, PolicyMismatch] = {}

    # ----------------------------------------------------------------- what the view changes

    def read_blob(self, commit: str, relpath: str) -> bytes | None:
        if relpath == MANIFEST_PATH:
            migration = self._migration(commit)
            return self.raw.read_blob(commit, relpath) if migration is None else migration.manifest_blob
        file_id = object_file_id(relpath)
        if file_id is None:
            return self.raw.read_blob(commit, relpath)
        migration = self._migration(commit)
        if migration is None or file_id not in migration.objects:
            return self.raw.read_blob(commit, relpath)
        return self._cipher.seal(
            migration.objects[file_id], domain="object", file_id=file_id, key_id=migration.key_ids[file_id]
        )

    def migrated_contents(self, commit: str | None) -> Mapping[str, bytes]:
        """The plaintext of each object `commit`'s migration changed, by file ID."""
        migration = None if commit is None else self._migration(commit)
        return {} if migration is None else migration.objects

    def notes(self) -> tuple[str, ...]:
        """One report line per preset migrated, in the order they were first read."""
        seen = dict.fromkeys(
            f"policy migrated: {preset} rev {before}→{after}"
            for migration in self._migrations.values()
            if migration is not None
            for preset, before, after in migration.steps
        )
        return tuple(seen)

    @property
    def mismatches(self) -> tuple[PolicyMismatch, ...]:
        """What a read-only view met instead of raising: the presets this build cannot judge."""
        return tuple(self._mismatches.values())

    # ----------------------------------------------------------------- the comparison

    def _migration(self, commit: str) -> Migration | None:
        if commit not in self._migrations:
            self._migrations[commit] = self._build(commit)
        return self._migrations[commit]

    def _build(self, commit: str) -> Migration | None:
        blob = self.raw.read_blob(commit, MANIFEST_PATH)
        if blob is None:
            return None
        manifest = loads_manifest(self._cipher.open(blob, domain="manifest", file_id=""))
        verdicts = policy_verdicts(manifest.policy, manifest.records.values(), self._catalog)
        blocked = {name: verdict for name, verdict in verdicts.items() if verdict in (NEWER, UNKNOWN)}
        if blocked:
            name, verdict = next(iter(blocked.items()))
            mismatch = PolicyMismatch(name, verdict)
            if self._strict:
                raise mismatch
            self._mismatches[name] = mismatch
            return None
        older = frozenset(name for name, verdict in verdicts.items() if verdict == OLDER)
        if not older:
            return None
        return self._migrate(commit, manifest, envelope_key_id(blob), older)

    def _migrate(self, commit: str, manifest: Manifest, manifest_key: int, older: frozenset[str]) -> Migration:
        records: dict[str, ManifestRecord] = {}
        objects: dict[str, bytes] = {}
        key_ids: dict[str, int] = {}
        for file_id, record in manifest.records.items():
            owner = self._catalog.preset_for(record.path)
            policy = self._catalog.policy_for(record.path)
            stored = self.raw.read_blob(commit, object_path(file_id))
            if owner is None or owner.name not in older:
                records[file_id] = record
                continue
            if policy is None:
                # The preset owns the path but holds no policy for it: nothing can be projected, and stamping the
                # preset as migrated would claim otherwise.
                raise PolicyMismatch(owner.name, UNKNOWN)
            if stored is None:
                raise ManifestCorrupt(f"record {file_id!r} has no stored object")
            data = self._cipher.open(stored, domain="object", file_id=file_id)
            if _sha256(data) != record.sha256:
                raise ContentDigestMismatch(file_id, record.sha256, _sha256(data))
            # A projection that cannot be done stops the cycle (`ProjectionError`): the stored object is the one
            # holding what the new policy strips, so serving it unprojected would defeat the migration.
            projected = self._project(data, record.path, policy.format)
            if self._scanner is not None:
                # A diff passes none: what it serves is shown, never sealed or written.
                self._scanner.scan(projected, subject=record.path, format=policy.format)
            records[file_id] = ManifestRecord.create(
                file_id=file_id,
                path=record.path,
                preset=record.preset,
                scope=record.scope,
                machine=record.machine,
                format=policy.format,
                sha256=_sha256(projected),
            )
            if projected != data:
                objects[file_id] = projected
                key_ids[file_id] = envelope_key_id(stored)
        local = stamp_policies(records.values(), self._catalog)
        migrated = Manifest.create(
            schema=MANIFEST_SCHEMA,
            key_generation=manifest.key_generation,
            records=records,
            policy={**manifest.policy, **{name: stamp for name, stamp in local.items() if name in older}},
        )
        steps = tuple(
            (name, manifest.policy[name].rev if name in manifest.policy else 1, local[name].rev)
            for name in sorted(older)
            if name in local
        )
        sealed = self._cipher.seal(dumps_manifest(migrated), domain="manifest", file_id="", key_id=manifest_key)
        return Migration(manifest_blob=sealed, objects=objects, key_ids=key_ids, steps=steps)

    # ----------------------------------------------------------------- everything else is the stored repo

    def init(self) -> None:
        self.raw.init()

    def clone(self, remote: str) -> None:
        self.raw.clone(remote)

    def fetch(self, remote: str, branch: str) -> None:
        self.raw.fetch(remote, branch)

    def head(self) -> str | None:
        return self.raw.head()

    def branch(self) -> str | None:
        return self.raw.branch()

    def resolve(self, rev: str) -> str | None:
        return self.raw.resolve(rev)

    def merge_bases(self, left: str, right: str) -> tuple[str, ...]:
        return self.raw.merge_bases(left, right)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.raw.is_ancestor(ancestor, descendant)

    def commit(self, parents: tuple[str, ...], message: str, paths: tuple[str, ...]) -> str:
        return self.raw.commit(parents, message, paths)

    def update_ref(self, branch: str, commit: str) -> None:
        self.raw.update_ref(branch, commit)

    def materialize(self, commit: str) -> None:
        self.raw.materialize(commit)

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        return self.raw.push(remote, branch, commit)

    def generations_in_reachable_objects(self, tips: tuple[str, ...]) -> tuple[int, ...]:
        return self.raw.generations_in_reachable_objects(tips)

    def remote_reachable(self, remote: str) -> bool:
        return self.raw.remote_reachable(remote)


def raw_of(transport: GitTransport) -> GitTransport:
    """The stored repo behind `transport`: itself unless it is a `PolicyView`.

    A write is planned against what is stored, and a ledger entry's digest is of the object as it was stored
    when the entry was made, so both read around the view.

    The view must be the transport the cycle holds, not wrapped by another: a wrapper would hide it from this check
    and a write would be planned against the migrated manifest. `tests/unit/test_policy_gate.py` pins the behavior.
    """
    return transport.raw if isinstance(transport, PolicyView) else transport


def migrated_contents(transport: GitTransport, commit: str | None) -> Mapping[str, bytes]:
    """The objects `commit`'s migration changed (empty when `transport` is not a `PolicyView`)."""
    return transport.migrated_contents(commit) if isinstance(transport, PolicyView) else {}


def migration_notes(transport: GitTransport) -> tuple[str, ...]:
    """The report lines for the presets `transport` migrated (empty when it is not a `PolicyView`)."""
    return transport.notes() if isinstance(transport, PolicyView) else ()


__all__ = ["PolicyView", "Project", "migrated_contents", "migration_notes", "raw_of"]
