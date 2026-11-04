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

from syncintosh.core.errors import ContentDigestMismatch, ManifestCorrupt, PolicyMismatch, ProjectionError
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
    # The same objects sealed once, so a re-read serves identical bytes (a fresh nonce per read would not).
    sealed: Mapping[str, bytes] = field(repr=False)
    steps: tuple[tuple[str, int, int], ...]
    # Paths left out of the view because they could not be reduced to the local policy; the cycle recollects them.
    skipped: tuple[str, ...] = ()


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
        return migration.sealed[file_id]

    def migrated_contents(self, commit: str | None) -> Mapping[str, bytes]:
        """The plaintext of each object `commit`'s migration changed, by file ID."""
        migration = None if commit is None else self._migration(commit)
        return {} if migration is None else migration.objects

    def left_out(self, commit: str | None) -> tuple[str, ...]:
        """The paths `commit`'s migration could not reduce and so left out of the view."""
        migration = None if commit is None else self._migration(commit)
        return () if migration is None else migration.skipped

    def notes(self) -> tuple[str, ...]:
        """One report line per preset migrated, in the order they were first read."""
        seen = dict.fromkeys(
            f"policy migrated: {preset} rev {before}→{after}"
            for migration in self._migrations.values()
            if migration is not None
            for preset, before, after in migration.steps
        )
        left_out = dict.fromkeys(
            f"policy not migrated: {path} could not be reduced to this build's policy; it will be collected again"
            for migration in self._migrations.values()
            if migration is not None
            for path in migration.skipped
        )
        return (*seen, *left_out)

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
            mismatches = {name: PolicyMismatch(name, verdict) for name, verdict in blocked.items()}
            if self._strict:
                raise next(iter(mismatches.values()))
            self._mismatches.update(mismatches)
            return None
        older = frozenset(name for name, verdict in verdicts.items() if verdict == OLDER)
        if not older:
            return None
        return self._migrate(commit, manifest, envelope_key_id(blob), older)

    def _migrate(self, commit: str, manifest: Manifest, manifest_key: int, older: frozenset[str]) -> Migration:
        records: dict[str, ManifestRecord] = {}
        objects: dict[str, bytes] = {}
        sealed: dict[str, bytes] = {}
        skipped: list[str] = []
        held_back: set[str] = set()
        for file_id, record in manifest.records.items():
            owner = self._catalog.preset_for(record.path)
            if owner is None or owner.name not in older:
                records[file_id] = record
                continue
            policy = self._catalog.policy_for(record.path)
            if policy is None:
                # The preset owns the path but holds no policy for it: a catalog that contradicts itself, not a
                # newer build, so it is not a `PolicyMismatch` ("upgrade") either.
                raise ManifestCorrupt(f"{record.path!r} is owned by {owner.name!r}, which holds no policy for it")
            stored = self.raw.read_blob(commit, object_path(file_id))
            if stored is None:
                raise ManifestCorrupt(f"record {file_id!r} has no stored object")
            data = self._cipher.open(stored, domain="object", file_id=file_id)
            if _sha256(data) != record.sha256:
                raise ContentDigestMismatch(file_id, record.sha256, _sha256(data))
            try:
                if record.format != policy.format:
                    # Collect and apply refuse a record whose format is not the catalog's, one file at a time.
                    raise ProjectionError(record.path)
                projected = self._project(data, record.path, policy.format)
            except ProjectionError:
                # One file that cannot be reduced must not wedge the device: it is neither served (the stored object
                # holds what the new policy strips) nor stamped, and the cycle collects the live file in its place.
                skipped.append(record.path)
                held_back.add(owner.name)
                continue
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
                sealed[file_id] = self._cipher.seal(
                    projected, domain="object", file_id=file_id, key_id=envelope_key_id(stored)
                )
        raised = older - held_back
        local = stamp_policies(records.values(), self._catalog)
        migrated = Manifest.create(
            schema=MANIFEST_SCHEMA,
            key_generation=manifest.key_generation,
            records=records,
            policy={**manifest.policy, **{name: stamp for name, stamp in local.items() if name in raised}},
        )
        steps = tuple(
            (name, manifest.policy[name].rev if name in manifest.policy else 1, local[name].rev)
            for name in sorted(raised)
            if name in local
        )
        manifest_sealed = self._cipher.seal(
            dumps_manifest(migrated), domain="manifest", file_id="", key_id=manifest_key
        )
        return Migration(
            manifest_blob=manifest_sealed, objects=objects, sealed=sealed, steps=steps, skipped=tuple(skipped)
        )

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


__all__ = ["PolicyView", "Project"]
