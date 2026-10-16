# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: the sync cycle for the tracked text files.

The 14 steps of ``docs/design/01-sync-model.md``, in order: lock → collect → scan → stage →
commit → fetch → classify → base → decrypt → merge → gate → publish → retry → apply. The rules
that shape the code:

* **Change detection is against the apply ledger, never `HEAD`**, because apply runs after the
  push (``core/ledger.py``).
* **Git never merges ciphertext.** Sync decrypts both sides, merges plaintext, and builds the
  result with one `commit-tree` holding two parents.
* **Conflicts never touch live files.** A conflict writes `pending/`, leaves the local commit in
  place, and stops without pushing.
* **Publish before apply**, and apply checks every write against its baseline (compare-and-swap).
* **Commit only on change**, so an unchanged sync rewrites nothing.

`services` orchestrates the ports; it never imports `vcs`, `crypto`, `system`, or `adapters`
(``AGENTS.md`` §2.2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalItem, SyncConfig, allows
from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import (
    CommitmentsCorrupt,
    ContentDigestMismatch,
    GitError,
    KeyGenerationMismatch,
    PendingConflict,
)
from syncintosh.core.ledger import AdoptChoice, chosen_digest, classify
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.manifest_codec import dumps_manifest, loads_manifest
from syncintosh.core.models import LedgerEntry, PendingState
from syncintosh.core.models import PendingConflict as PendingConflictRecord
from syncintosh.core.paths import collision_key
from syncintosh.core.ports import (
    Cipher,
    DenyList,
    FileIdSource,
    GitTransport,
    KeyStore,
    LedgerStore,
    LiveFiles,
    MergeDriver,
    PendingStore,
    RepoFiles,
    RingCodec,
    SecretScanner,
    SyncLock,
    remote_ref,
)
from syncintosh.core.record_merge import RecordConflict, RecordMergeResult, merge_manifests
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH, object_path, plan_writes
from syncintosh.services.repo_writer import write_repo

# A push race is expected to settle in one or two tries; the cap stops a pathological fight from
# looping forever (``docs/design/01-sync-model.md`` step 13).
MAX_PUSH_ATTEMPTS = 4

# What apply decided for one record.
_Decision = Literal["write", "skip", "abort", "defer"]
_WRITE: _Decision = "write"
_SKIP: _Decision = "skip"
_ABORT: _Decision = "abort"
_DEFER: _Decision = "defer"


@dataclass(frozen=True)
class SyncReport:
    """What one cycle did, for the CLI to render."""

    published_commit: str | None = None
    created: tuple[str, ...] = ()
    collected: tuple[str, ...] = ()
    applied: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    suppressed: tuple[str, ...] = ()
    symlinks: tuple[str, ...] = ()
    aborted: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    first_sight: tuple[str, ...] = ()
    conflicts: tuple[PendingConflictRecord, ...] = ()
    skipped: tuple[str, ...] = ()
    unchanged: bool = False

    @property
    def has_conflicts(self) -> bool:
        """True when the cycle stopped with unresolved conflicts."""
        return bool(self.conflicts)

    @property
    def needs_adopt(self) -> bool:
        """True when a first-sight record needs `--adopt local|remote`."""
        return bool(self.first_sight)


@dataclass
class _Collect:
    """The state one collect pass produces."""

    records: dict[str, ManifestRecord] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    first_sight: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _join(home: str, relpath: str) -> str:
    return str(Path(home) / relpath)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _replace_sha(record: ManifestRecord, sha256: str) -> ManifestRecord:
    return ManifestRecord.create(
        file_id=record.file_id,
        path=record.path,
        preset=record.preset,
        scope=record.scope,
        machine=record.machine,
        format=record.format,
        sha256=sha256,
    )


def _item_key(scope: str, machine: str | None, path: str) -> tuple[str, str | None, str]:
    """The tracked-file key, folded the same way `track` folds it (APFS case/NFD)."""
    return (scope, machine, collision_key(path))


def _find_item_record(records: Mapping[str, ManifestRecord], item: LocalItem) -> ManifestRecord | None:
    wanted = _item_key(item.scope, item.machine, item.path)
    return next(
        (record for record in records.values() if _item_key(record.scope, record.machine, record.path) == wanted),
        None,
    )


def _mint_id(records: Mapping[str, ManifestRecord], source: FileIdSource) -> str:
    for _ in range(1000):
        candidate = source.new_file_id()
        if candidate not in records:
            return candidate
    raise GitError("the file ID source kept returning an ID already in the manifest")


def _read_manifest(transport: GitTransport, cipher: Cipher, commit: str | None) -> Manifest | None:
    """Decrypt the manifest at `commit`, or `None` when there is no manifest there."""
    if commit is None:
        return None
    blob = transport.read_blob(commit, MANIFEST_PATH)
    if blob is None:
        return None
    return loads_manifest(cipher.open(blob, domain="manifest", file_id=""))


def _read_object(transport: GitTransport, cipher: Cipher, commit: str | None, file_id: str) -> bytes | None:
    if commit is None:
        return None
    blob = transport.read_blob(commit, object_path(file_id))
    if blob is None:
        return None
    return cipher.open(blob, domain="object", file_id=file_id)


def _verified_object(
    transport: GitTransport,
    cipher: Cipher,
    commit: str | None,
    file_id: str,
    expected_sha256: str,
) -> bytes | None:
    """Read and decrypt an object, refusing it when its digest disagrees with its record.

    The record digest is the mix-and-match detector (``docs/design/03-manifest.md``): an object
    replayed into a slot whose record digest does not match must be caught **before it is used**,
    or a stale ciphertext would be applied as if it were current.
    """
    data = _read_object(transport, cipher, commit, file_id)
    if data is None:
        return None
    actual = _sha256(data)
    if actual != expected_sha256:
        raise ContentDigestMismatch(file_id, expected_sha256, actual)
    return data


def _current_generation(store: KeyStore, head: Manifest | None) -> int:
    if head is not None:
        return head.key_generation
    generations = store.generations()
    if not generations:
        raise GitError("this machine holds no key generation; run `syncintosh init` first")
    return max(generations)


def _commit(transport: GitTransport, branch: str, parents: tuple[str, ...], message: str) -> str:
    commit = transport.commit(parents, message)
    transport.update_ref(branch, commit)
    return commit


# --------------------------------------------------------------------------- collect


def _collect(
    *,
    config: SyncConfig,
    head: Manifest | None,
    ledger: Mapping[str, LedgerEntry],
    live: LiveFiles,
    deny: DenyList,
    home: str,
    adopt: AdoptChoice | None,
    source: FileIdSource,
) -> _Collect:
    """Steps 2–4: read the tracked paths, classify each, and stage what changed."""
    state = _Collect(records={} if head is None else dict(head.records))
    for item in config.items:
        if deny.is_denied(home, item.path):
            state.skipped.append(item.path)
            continue
        live_bytes = live.read(_join(home, item.path))
        live_sha = None if live_bytes is None else _sha256(live_bytes)
        record = _find_item_record(state.records, item)
        if record is None:
            _collect_new(state, item, live_bytes, live_sha, source)
        else:
            _collect_existing(state, item, record, live_bytes, live_sha, ledger, adopt)
    return state


def _collect_new(
    state: _Collect,
    item: LocalItem,
    live_bytes: bytes | None,
    live_sha: str | None,
    source: FileIdSource,
) -> None:
    if live_bytes is None or live_sha is None:
        state.deferred.append(item.path)
        return
    file_id = _mint_id(state.records, source)
    state.records[file_id] = ManifestRecord.create(
        file_id=file_id,
        path=item.path,
        preset=item.preset,
        scope=item.scope,
        machine=item.machine,
        format=item.format,
        sha256=live_sha,
    )
    state.contents[file_id] = live_bytes
    state.created.append(item.path)


def _collect_existing(
    state: _Collect,
    item: LocalItem,
    record: ManifestRecord,
    live_bytes: bytes | None,
    live_sha: str | None,
    ledger: Mapping[str, LedgerEntry],
    adopt: AdoptChoice | None,
) -> None:
    verdict = classify(ledger.get(record.file_id), live_sha, record.sha256)
    if verdict == "local-change":
        state.records[record.file_id] = _replace_sha(record, live_sha or record.sha256)
        state.contents[record.file_id] = live_bytes or b""
        state.collected.append(item.path)
    elif verdict == "deferred":
        # Nothing to collect; the apply step decides whether this machine has a file to create
        # (the directory exists) or is genuinely deferred (the app is not installed).
        return
    elif verdict == "suppressed":
        state.suppressed.append(item.path)
    elif verdict == "first-sight" and live_bytes is not None and live_sha is not None:
        _collect_first_sight(state, item, record, live_bytes, live_sha, adopt)
    # `in-sync` and `pending-apply` collect nothing: the first is already settled, and the second
    # is finished by the apply step.


def _collect_first_sight(
    state: _Collect,
    item: LocalItem,
    record: ManifestRecord,
    live_bytes: bytes,
    live_sha: str,
    adopt: AdoptChoice | None,
) -> None:
    if adopt is None:
        state.first_sight.append(item.path)
        return
    if adopt == "remote":
        # Taking the repo's side: keep the record; apply writes the repo content over the file.
        return
    state.records[record.file_id] = _replace_sha(
        record, chosen_digest("local", local_sha256=live_sha, remote_sha256=record.sha256)
    )
    state.contents[record.file_id] = live_bytes
    state.collected.append(item.path)


# --------------------------------------------------------------------------- publish


def _verify_commitments(transport: GitTransport, codec: RingCodec, store: KeyStore, commit: str) -> None:
    """Compare every held generation against the committed map at `commit`.

    The map is required, not optional: a commitment exists for every generation, and treating an
    absent file as "nothing to compare" would let a remote disable the only check that covers an
    out-of-band rotation by deleting one file (``docs/design/05-key-generations.md``).
    """
    blob = transport.read_blob(commit, COMMITMENTS_PATH)
    if blob is None:
        raise CommitmentsCorrupt(f"the repo publishes no {COMMITMENTS_PATH}")
    published = loads_commitments(blob)
    for key_id in store.generations():
        expected = published.get(key_id)
        key = store.get(key_id)
        if expected is not None and key is not None and codec.commitment(key) != expected:
            raise KeyGenerationMismatch(key_id, "the published commitment is not this machine's key for it")


def _merge_base(transport: GitTransport, local: str, remote: str) -> str | None:
    bases = transport.merge_bases(local, remote)
    if len(bases) > 1:
        raise GitError(f"criss-cross history: {len(bases)} merge bases for {local[:8]} and {remote[:8]}")
    return bases[0] if bases else None


@dataclass(frozen=True)
class _Merged:
    """The outcome of merging a diverged local and remote: a commit, or conflicts."""

    commit: str | None
    conflicts: tuple[PendingConflictRecord, ...]
    manifest: Manifest | None = None
    base_commit: str | None = None


def _merge_diverged(
    *,
    transport: GitTransport,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    driver: MergeDriver,
    branch: str,
    local: str,
    remote: str,
    local_manifest: Manifest,
) -> _Merged:
    """Steps 8–12 for a diverged pair: decrypt, merge, gate, and build the two-parent commit."""
    base_commit = _merge_base(transport, local, remote)
    base_manifest = _read_manifest(transport, cipher, base_commit)
    remote_manifest = _read_manifest(transport, cipher, remote)
    if remote_manifest is None:
        raise GitError("the remote branch has no manifest; refusing to merge")
    result = merge_manifests(base_manifest, local_manifest, remote_manifest)
    conflicts = _record_conflicts(result)

    contents: dict[str, bytes] = {}
    records = dict(result.merged)
    for task in result.content_merges:
        base_bytes = _verified_object(transport, cipher, base_commit, task.file_id, task.base_sha256)
        local_bytes = _verified_object(transport, cipher, local, task.file_id, task.local_sha256)
        remote_bytes = _verified_object(transport, cipher, remote, task.file_id, task.remote_sha256)
        if local_bytes is None or remote_bytes is None:
            conflicts = (*conflicts, PendingConflictRecord(task.file_id, task.path, "a side's object is missing"))
            continue
        merged = merge_content(base_bytes or b"", local_bytes, remote_bytes, driver)
        if merged.conflicted:
            conflicts = (
                *conflicts,
                PendingConflictRecord(
                    task.file_id,
                    task.path,
                    "the same region changed on both sides",
                    base_bytes,
                    local_bytes,
                    remote_bytes,
                ),
            )
            continue
        records[task.file_id] = task.complete(_sha256(merged.data))
        contents[task.file_id] = merged.data
    if conflicts:
        return _Merged(commit=None, conflicts=conflicts, base_commit=base_commit)

    merged_manifest = Manifest.create(
        schema=MANIFEST_SCHEMA, key_generation=local_manifest.key_generation, records=records
    )
    plan = plan_writes(local_manifest, merged_manifest)
    for file_id in plan.objects_to_write:
        if file_id in contents:
            continue
        data = _object_content(transport, cipher, local, remote, file_id, merged_manifest.records[file_id].sha256)
        if data is None:
            raise ContentDigestMismatch(file_id, merged_manifest.records[file_id].sha256, "no matching object")
        contents[file_id] = data
    write_repo(files=files, cipher=cipher, scanner=scanner, head=local_manifest, new=merged_manifest, contents=contents)
    commit = _commit(transport, branch, (local, remote), "syncintosh: merge")
    return _Merged(commit=commit, conflicts=(), manifest=merged_manifest, base_commit=base_commit)


def _object_content(
    transport: GitTransport,
    cipher: Cipher,
    local: str,
    remote: str,
    file_id: str,
    expected_sha256: str,
) -> bytes | None:
    """The plaintext of a surviving record, from the side whose object matches its digest."""
    for commit in (local, remote):
        data = _read_object(transport, cipher, commit, file_id)
        if data is not None and _sha256(data) == expected_sha256:
            return data
    return None


def _record_conflicts(result: RecordMergeResult) -> tuple[PendingConflictRecord, ...]:
    conflicts = [_conflict_from_record(conflict) for conflict in result.conflicts]
    conflicts.extend(
        PendingConflictRecord(collision.file_ids[0], collision.path, "two records claim one path")
        for collision in result.collisions
    )
    return tuple(conflicts)


def _conflict_from_record(conflict: RecordConflict) -> PendingConflictRecord:
    record = conflict.local or conflict.remote or conflict.base
    path = "" if record is None else record.path
    return PendingConflictRecord(conflict.file_id, path, f"{conflict.kind} conflict")


@dataclass(frozen=True)
class _Publish:
    """Where a publish attempt ended: a commit, conflicts, or nothing to publish."""

    published: str | None = None
    conflicts: tuple[PendingConflictRecord, ...] = ()
    base_commit: str | None = None
    remote_commit: str | None = None


def _publish(
    *,
    transport: GitTransport,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    driver: MergeDriver,
    codec: RingCodec,
    store: KeyStore,
    config: SyncConfig,
    branch: str,
    local_commit: str | None,
    local_manifest: Manifest,
) -> _Publish:
    """Steps 6–13: fetch, classify, merge, push, retry."""
    for _ in range(MAX_PUSH_ATTEMPTS):
        transport.fetch(config.remote, branch)
        remote_commit = transport.resolve(remote_ref(branch))
        if remote_commit is not None:
            _verify_commitments(transport, codec, store, remote_commit)
        local = transport.head()
        if remote_commit is None:
            outcome = _try_push(transport, config.remote, branch, local_commit)
            if outcome == "ok":
                return _Publish(published=local_commit)
            continue
        if local is None or local == remote_commit:
            if local is None:
                _fast_forward(transport, branch, remote_commit)
                return _Publish(published=remote_commit, remote_commit=remote_commit)
            return _Publish(published=local, remote_commit=remote_commit)
        if transport.is_ancestor(local, remote_commit):
            _fast_forward(transport, branch, remote_commit)
            return _Publish(published=remote_commit, remote_commit=remote_commit)
        if transport.is_ancestor(remote_commit, local):
            outcome = _try_push(transport, config.remote, branch, local)
            if outcome == "ok":
                return _Publish(published=local, remote_commit=remote_commit)
            continue
        merged = _merge_diverged(
            transport=transport,
            files=files,
            cipher=cipher,
            scanner=scanner,
            driver=driver,
            branch=branch,
            local=local,
            remote=remote_commit,
            local_manifest=local_manifest,
        )
        if merged.commit is None:
            return _Publish(conflicts=merged.conflicts, base_commit=merged.base_commit, remote_commit=remote_commit)
        outcome = _try_push(transport, config.remote, branch, merged.commit)
        if outcome == "ok":
            return _Publish(published=merged.commit, base_commit=merged.base_commit, remote_commit=remote_commit)
        # Discard the unpushed merge commit, keep the collect commit, and retry against the fresh
        # remote. Stacking a second merge on an unpushed one is what produces criss-cross history.
        transport.update_ref(branch, local)
        transport.materialize(local)
    raise GitError(f"push retry limit ({MAX_PUSH_ATTEMPTS}) reached without converging")


def _try_push(transport: GitTransport, remote: str, branch: str, commit: str | None) -> str:
    """Push `commit`; `"non-fast-forward"` sends the caller around its retry loop."""
    if commit is None:
        return "ok"
    outcome = transport.push(remote, branch, commit)
    if outcome == "rejected":
        raise GitError("the push was rejected for a reason other than a non-fast-forward race")
    return outcome


def _fast_forward(transport: GitTransport, branch: str, commit: str) -> None:
    transport.update_ref(branch, commit)
    transport.materialize(commit)


# --------------------------------------------------------------------------- apply


def _apply(
    *,
    transport: GitTransport,
    cipher: Cipher,
    target_commit: str,
    target_manifest: Manifest,
    baseline: Mapping[str, bytes],
    ledger: Mapping[str, LedgerEntry],
    live: LiveFiles,
    deny: DenyList,
    config: SyncConfig,
    home: str,
    adopt: AdoptChoice | None,
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Step 14: write the merged records into the live files, compare-and-swap checked.

    The bytes written come from `target_commit`, the published repo state — never from the
    collected local bytes, which after a merge are only one side of the result. `baseline` is
    the other role those collected bytes play: the compare-and-swap reference for the file.

    A record's `path` and `preset` arrive from the repo, so before writing anything this checks
    the target stays under `$HOME` (resolving the parent) and is one this machine's local
    allowlist already tracks (``docs/design/03-manifest.md``). A refusal is reported, never
    silent.
    """
    applied: list[str] = []
    deferred: list[str] = []
    symlinks: list[str] = []
    aborted: list[str] = []
    refused: list[str] = []
    for file_id, record in target_manifest.records.items():
        if record.scope == "machine" and record.machine != config.machine_id:
            continue
        if deny.is_denied(home, record.path) or deny.escapes(home, record.path) or not allows(config, record):
            refused.append(record.path)
            continue
        live_path = _join(home, record.path)
        if live.is_symlink(live_path):
            symlinks.append(record.path)
            continue
        live_bytes = live.read(live_path)
        decision = _decision(record, live_bytes, ledger.get(file_id), baseline.get(file_id), adopt)
        if decision == _ABORT:
            aborted.append(record.path)
            continue
        if decision == _DEFER:
            deferred.append(record.path)
            continue
        if decision == _SKIP:
            continue
        if live_bytes is None and ledger.get(file_id) is None and not live.exists(str(Path(live_path).parent)):
            # The app is not installed: report instead of creating a directory tree.
            deferred.append(record.path)
            continue
        data = _verified_object(transport, cipher, target_commit, file_id, record.sha256)
        if data is None:
            deferred.append(record.path)
            continue
        live.write(live_path, data)
        applied.append(record.path)
    return applied, deferred, symlinks, aborted, refused


def _decision(
    record: ManifestRecord,
    live_bytes: bytes | None,
    entry: LedgerEntry | None,
    collected: bytes | None,
    adopt: AdoptChoice | None,
) -> _Decision:
    """Whether apply may write `record` over the live path, per the conservative rules."""
    if live_bytes is None:
        # A missing file is not a deletion. Recreate a record this machine has never written, and
        # never one it wrote and that disappeared (that is the suppressed rule).
        return _WRITE if entry is None else _SKIP
    in_sync = _sha256(live_bytes) == record.sha256 and entry is not None and not entry.suppressed
    if in_sync:
        return _SKIP
    if entry is None or entry.suppressed:
        # First sight, or a suppressed file that came back: overwrite only on an explicit adopt.
        return _WRITE if adopt == "remote" else _SKIP
    baseline = collected if collected is not None else None
    if baseline is not None:
        return _WRITE if live_bytes == baseline else _ABORT
    return _WRITE if _sha256(live_bytes) == entry.sha256 else _ABORT


def _settle_ledger(
    *,
    target_manifest: Manifest,
    target_commit: str,
    ledger: Mapping[str, LedgerEntry],
    live: LiveFiles,
    home: str,
) -> dict[str, LedgerEntry]:
    """Rewrite the ledger so the next cycle sees in-agreement files as a normal baseline."""
    settled: dict[str, LedgerEntry] = {}
    for file_id, record in target_manifest.records.items():
        live_path = _join(home, record.path)
        if live.is_symlink(live_path):
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = old
            continue
        live_bytes = live.read(live_path)
        if live_bytes is None:
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = LedgerEntry(sha256=old.sha256, commit=old.commit, suppressed=True)
            continue
        digest = _sha256(live_bytes)
        if digest == record.sha256:
            settled[file_id] = LedgerEntry(sha256=digest, commit=target_commit, suppressed=False)
        else:
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = old
    return settled


# --------------------------------------------------------------------------- entry point


def sync(
    *,
    transport: GitTransport,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    store: KeyStore,
    codec: RingCodec,
    driver: MergeDriver,
    live: LiveFiles,
    ledger_store: LedgerStore,
    pending_store: PendingStore,
    lock: SyncLock,
    deny: DenyList,
    source: FileIdSource,
    config: SyncConfig,
    home: str,
    branch: str,
    adopt: AdoptChoice | None = None,
) -> SyncReport:
    """Run one sync cycle. A conflict returns a report; a hard failure raises."""
    lock.acquire()
    try:
        if pending_store.is_pending():
            raise PendingConflict(
                "a previous sync stopped with unresolved conflicts; resolve them before syncing again"
            )
        return _cycle(
            transport=transport,
            files=files,
            cipher=cipher,
            scanner=scanner,
            store=store,
            codec=codec,
            driver=driver,
            live=live,
            ledger_store=ledger_store,
            pending_store=pending_store,
            deny=deny,
            source=source,
            config=config,
            home=home,
            branch=branch,
            adopt=adopt,
        )
    finally:
        lock.release()


def _cycle(
    *,
    transport: GitTransport,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    store: KeyStore,
    codec: RingCodec,
    driver: MergeDriver,
    live: LiveFiles,
    ledger_store: LedgerStore,
    pending_store: PendingStore,
    deny: DenyList,
    source: FileIdSource,
    config: SyncConfig,
    home: str,
    branch: str,
    adopt: AdoptChoice | None,
) -> SyncReport:
    ledger = dict(ledger_store.load())
    head_commit = transport.head()
    head_manifest = _read_manifest(transport, cipher, head_commit)
    collected = _collect(
        config=config,
        head=head_manifest,
        ledger=ledger,
        live=live,
        deny=deny,
        home=home,
        adopt=adopt,
        source=source,
    )
    if collected.first_sight:
        # Present both sides before anything is written; the user picks with --adopt.
        return SyncReport(first_sight=tuple(collected.first_sight), skipped=tuple(collected.skipped))

    generation = _current_generation(store, head_manifest)
    new_manifest = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=generation, records=collected.records)
    changed = (
        bool(collected.contents)
        or head_manifest is None
        or dumps_manifest(head_manifest) != dumps_manifest(new_manifest)
    )
    if changed:
        write_repo(
            files=files,
            cipher=cipher,
            scanner=scanner,
            head=head_manifest,
            new=new_manifest,
            contents=collected.contents,
        )
        local_commit = _commit(transport, branch, () if head_commit is None else (head_commit,), "syncintosh: collect")
    else:
        local_commit = head_commit

    outcome = _publish(
        transport=transport,
        files=files,
        cipher=cipher,
        scanner=scanner,
        driver=driver,
        codec=codec,
        store=store,
        config=config,
        branch=branch,
        local_commit=local_commit,
        local_manifest=new_manifest,
    )
    if outcome.conflicts:
        pending_store.record(
            PendingState.create(
                local_commit=local_commit or "",
                remote_commit=outcome.remote_commit or "",
                base_commit=outcome.base_commit,
                conflicts=outcome.conflicts,
            )
        )
        return SyncReport(
            conflicts=outcome.conflicts,
            created=tuple(collected.created),
            collected=tuple(collected.collected),
            skipped=tuple(collected.skipped),
        )

    target_commit = outcome.published or local_commit or ""
    target_manifest = _read_manifest(transport, cipher, target_commit) or new_manifest
    applied, deferred, symlinks, aborted, refused = _apply(
        transport=transport,
        cipher=cipher,
        target_commit=target_commit,
        target_manifest=target_manifest,
        baseline=collected.contents,
        ledger=ledger,
        live=live,
        deny=deny,
        config=config,
        home=home,
        adopt=adopt,
    )
    ledger_store.save(
        _settle_ledger(
            target_manifest=target_manifest,
            target_commit=target_commit,
            ledger=ledger,
            live=live,
            home=home,
        )
    )
    return SyncReport(
        published_commit=outcome.published,
        created=tuple(collected.created),
        collected=tuple(collected.collected),
        applied=tuple(applied),
        deferred=(*collected.deferred, *deferred),
        suppressed=tuple(collected.suppressed),
        symlinks=tuple(symlinks),
        aborted=tuple(aborted),
        refused=tuple(refused),
        skipped=tuple(collected.skipped),
        unchanged=not changed and outcome.published in (None, local_commit),
    )


__all__ = ["MAX_PUSH_ATTEMPTS", "SyncReport", "sync"]
