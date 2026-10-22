# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-cases: create a repo's first key generation, import a ring, and rotate generations.

The master key is unrecoverable if lost (``AGENTS.md`` §6), so `init_new_repo` treats the
recovery export as part of creation, not a follow-up: it writes the export, reads it back, and
unwraps it, and only then touches the ring and records the commitment. A failed export leaves
the keyring exactly as it was, so a retry starts clean instead of colliding with an abandoned
generation 1.

`import_ring` verifies **every** generation — against this machine's ring and against the
published commitment — before it mutates the store, so a partial or mismatched import never
lands half a ring (``docs/design/05-key-generations.md``).

`rotate_repo` is the rotation itself: mint a generation, re-encrypt **every** object and the
manifest under it, and publish one fast-forward-only commit (a hygiene rotation also publishes
`keys/<new_id>.wrap`). A push that cannot fast-forward discards the rotation and drops the new
generation (``docs/design/05-key-generations.md``). `export_ring` writes the whole ring for an
out-of-band hand-off.

`services` orchestrates the `KeyStore`, `RingCodec`, `GenerationWrap`, `Cipher`, `RepoFiles`, and
`LiveFiles` ports; it never imports `crypto` (``AGENTS.md`` §2.2).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from typing import Literal

from syncintosh.core.commitments import dumps_commitments, loads_commitments
from syncintosh.core.errors import (
    CommitmentsCorrupt,
    ContentDigestMismatch,
    GitError,
    InitRefused,
    KeyGenerationMismatch,
    KeyStoreCorrupt,
    MasterKeyUnwrapError,
    PendingConflict,
    PruneRefused,
)
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.ports import (
    GENERATION_KEY_SIZE,
    Cipher,
    GenerationWrap,
    GitTransport,
    KeyStore,
    LiveFiles,
    PendingStore,
    PushOutcome,
    RepoFiles,
    Ring,
    RingCodec,
    SecretScanner,
    SyncLock,
    remote_ref,
)
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH, TRACKED_PATHS, object_path, wrap_path
from syncintosh.services.repo_writer import write_repo


def published_commitments(transport: GitTransport) -> Mapping[int, str]:
    """The repo's published commitment map at HEAD, or an empty map when there is no HEAD yet.

    A `use-case`-shaped read that lives here, not in `cli/`: the command only wires and renders
    (``AGENTS.md`` §2.3).
    """
    head = transport.head()
    if head is None:
        return {}
    blob = transport.read_blob(head, COMMITMENTS_PATH)
    return {} if blob is None else loads_commitments(blob)


def init_new_repo(
    *,
    store: KeyStore,
    codec: RingCodec,
    files: RepoFiles,
    live: LiveFiles,
    recovery_path: str,
    passphrase: str,
) -> None:
    """Create generation 1, force a verified recovery export, and record its commitment.

    `recovery_path` is where the wrapped ring is written for the user to keep. Nothing is written
    until both a free keyring slot and a free export path are confirmed: a re-run must never
    overwrite a good export with a key the keyring then refuses, which would leave the user with
    a backup they cannot use and a master key that exists nowhere else.

    The export is read back and unwrapped before the ring is stored, and a failure **before the
    key is stored** discards it, so the check above does not turn a failed run into one that can
    never be retried (``AGENTS.md`` §6 — the master key is unrecoverable if lost).
    """
    if store.get(1) is not None:
        if 1 not in store.generations():
            # Generation 1 is readable but the index does not list it: the keychain lost the index
            # entry, not a second key. Point at the import that rebuilds the index — `init` must not
            # mint a fresh master key over the one the recovery export protects.
            raise InitRefused(
                "this machine holds a generation 1 key its index no longer lists; restore the ring "
                "with `syncintosh key import <recovery export>`"
            )
        raise InitRefused("this machine already holds a key for generation 1")
    if live.exists(recovery_path):
        raise InitRefused(f"a recovery export already exists at {recovery_path}")

    generation_key = os.urandom(GENERATION_KEY_SIZE)
    ring: Ring = ((1, generation_key),)
    blob = codec.wrap(ring, passphrase)
    live.write(recovery_path, blob)
    try:
        written = live.read(recovery_path)
        if written is None:
            raise MasterKeyUnwrapError("the recovery export was not written; init is not complete")
        if codec.unwrap(written, passphrase) != ring:
            raise MasterKeyUnwrapError("the recovery export did not verify; init is not complete")
    except BaseException:
        # The export names a key the ring does not hold yet, so it is worth nothing to the user
        # and would only make the next attempt fail as "already exists". Remove it and let the
        # failure surface.
        _discard_export(live, recovery_path)
        raise

    # Only now touch the store: a store mutation before the export verifies would leave a key
    # stored without a backup, because the next attempt mints a fresh generation 1 and `put`
    # refuses a second key.
    store.put(1, generation_key)
    files.write(COMMITMENTS_PATH, dumps_commitments({1: codec.commitment(generation_key)}))


def _discard_export(live: LiveFiles, recovery_path: str) -> None:
    """Remove a failed init's export, without masking the failure that led here."""
    try:
        live.remove(recovery_path)
    except OSError:
        return


def import_ring(
    *,
    store: KeyStore,
    codec: RingCodec,
    blob: bytes,
    passphrase: str,
    published: Mapping[int, str],
    lock: SyncLock | None = None,
) -> None:
    """Unwrap `blob` and add its generations, after checking every one of them.

    `published` is the repo's `keys/commitments.json` map. A generation this machine already
    holds differently, a generation the repo does not commit to, or one whose commitment
    disagrees, is a hard error — the store is left untouched (``docs/design/05-key-generations.md``).

    `lock`, when given, is held across the store mutation: the keyring index is a read-modify-write,
    so it is only safe under the same lock a concurrent sync or rotation holds
    (``crypto/keyring_store.py``).
    """
    if lock is not None:
        lock.acquire()
    try:
        _import_ring(store=store, codec=codec, blob=blob, passphrase=passphrase, published=published)
    finally:
        if lock is not None:
            lock.release()


def _import_ring(
    *,
    store: KeyStore,
    codec: RingCodec,
    blob: bytes,
    passphrase: str,
    published: Mapping[int, str],
) -> None:
    ring = codec.unwrap(blob, passphrase)
    for key_id, generation_key in ring:
        held = store.get(key_id)
        if held is not None and held != generation_key:
            raise KeyGenerationMismatch(key_id, "this machine already holds a different key for it")
        expected = published.get(key_id)
        if expected is None:
            raise CommitmentsCorrupt(f"the repo publishes no commitment for generation {key_id}")
        if codec.commitment(generation_key) != expected:
            raise KeyGenerationMismatch(key_id, "the published commitment is not this machine's key for it")
    for key_id, generation_key in ring:
        store.put(key_id, generation_key)


def remote_commitments(transport: GitTransport, remote: str, branch: str) -> Mapping[int, str]:
    """The published commitment map at the remote's branch tip, after a fetch.

    A use-case-shaped read for `key import`: fetching first means an import sees a generation a
    rotation published just before it, and the map is what `import_ring` checks against.
    """
    transport.fetch(remote, branch)
    commit = transport.resolve(remote_ref(branch))
    if commit is None:
        return published_commitments(transport)
    return read_commitments(transport, commit)


def read_commitments(transport: GitTransport, commit: str) -> dict[int, str]:
    """The published commitment map at `commit`, refusing a repo that publishes none.

    The map is required, not optional: a commitment exists for every generation, and treating an
    absent file as "nothing to compare" would let a remote disable the only check that covers an
    out-of-band rotation by deleting one file (``docs/design/05-key-generations.md``).
    """
    blob = transport.read_blob(commit, COMMITMENTS_PATH)
    if blob is None:
        raise CommitmentsCorrupt(f"the repo publishes no {COMMITMENTS_PATH}")
    return loads_commitments(blob)


def verify_commitments(codec: RingCodec, store: KeyStore, published: Mapping[int, str]) -> None:
    """Compare every held generation against the committed map.

    A held generation the repo does not commit to is the same disable-the-check lever as deleting
    the file, one level finer, so it is refused too.
    """
    for key_id in store.generations():
        expected = published.get(key_id)
        if expected is None:
            raise CommitmentsCorrupt(f"the repo publishes no commitment for generation {key_id}")
        key = store.get(key_id)
        if key is not None and codec.commitment(key) != expected:
            raise KeyGenerationMismatch(key_id, "the published commitment is not this machine's key for it")


def absorb_wraps(
    *,
    transport: GitTransport,
    wrapper: GenerationWrap,
    codec: RingCodec,
    store: KeyStore,
    commit: str,
    published: Mapping[int, str],
) -> None:
    """Add every generation the remote distributes and this machine does not yet hold.

    A hygiene rotation publishes `keys/<new_id>.wrap`: `KG_{n+1}` sealed under `KG_n`. The
    commitment map lists every generation, so it is the index of which wraps to try; a generation
    this machine already holds is never overwritten (a second key for one id is a hard error, and
    `verify_commitments` reports it). The unwrapped key is checked against the published commitment
    **before** it reaches the ring (``docs/design/05-key-generations.md``).
    """
    for key_id in sorted(published):
        if store.get(key_id) is not None:
            continue
        blob = transport.read_blob(commit, wrap_path(key_id))
        if blob is None:
            # An out-of-band rotation publishes no wrap; the commitment still exists for it.
            continue
        key = wrapper.unwrap(blob, key_id=key_id)
        if codec.commitment(key) != published[key_id]:
            raise KeyGenerationMismatch(key_id, "the published commitment is not the key the wrap carries for it")
        store.put(key_id, key)


def _held_ring(store: KeyStore) -> Ring:
    """Every generation this machine holds, ascending, for an export or a commitment map."""
    ring: list[tuple[int, bytes]] = []
    for key_id in store.generations():
        key = store.get(key_id)
        if key is None:
            # The index named a generation the store cannot read; refuse rather than write a
            # ring (or a commitment map) that silently omits it.
            raise KeyStoreCorrupt(f"the ring lists generation {key_id} but its key is unreadable")
        ring.append((key_id, key))
    return tuple(ring)


def _commitments_for(store: KeyStore, codec: RingCodec, published: Mapping[int, str]) -> bytes:
    """The canonical commitment bytes for every held generation, plus any the repo already publishes.

    Merging preserves a commitment for a generation this machine does **not** hold — one another
    machine imported out of band — so a rotation never drops the corroboration that machine relies
    on.
    """
    held = {key_id: codec.commitment(key) for key_id, key in _held_ring(store)}
    return dumps_commitments({**published, **held})


def export_ring(
    *,
    store: KeyStore,
    codec: RingCodec,
    live: LiveFiles,
    path: str,
    passphrase: str,
) -> None:
    """Wrap the whole ring under `passphrase` and write it to `path`.

    Used for an **out-of-band** hand-off: a removal or a compromise rotation must not publish
    `keys/<n>.wrap`, because the machine being removed still holds the wrapper generation
    (``docs/design/05-key-generations.md``). Nothing is written when a file already exists at
    `path`, and the export is read back and unwrapped before it is trusted, so a backup that
    cannot be opened is never left for the user to rely on.
    """
    if live.exists(path):
        raise InitRefused(f"an export already exists at {path}")
    ring = _held_ring(store)
    if not ring:
        raise InitRefused(
            "this machine holds no key generation to export; if the keychain lost its index, "
            "restore it with `syncintosh key import <recovery export>`"
        )
    blob = codec.wrap(ring, passphrase)
    live.write(path, blob)
    try:
        written = live.read(path)
        if written is None or codec.unwrap(written, passphrase) != ring:
            raise MasterKeyUnwrapError("the export did not verify; it was not written correctly")
    except BaseException:
        _discard_export(live, path)
        raise


RotationMode = Literal["hygiene", "out-of-band"]


def rotate_repo(
    *,
    transport: GitTransport,
    files: RepoFiles,
    cipher: Cipher,
    wrapper: GenerationWrap,
    scanner: SecretScanner,
    store: KeyStore,
    codec: RingCodec,
    lock: SyncLock,
    pending: PendingStore,
    remote: str,
    branch: str,
    mode: RotationMode,
) -> int:
    """Rotate to a new generation under the lock; return the new generation id.

    `mode` is `hygiene` — publish `keys/<new_id>.wrap` in the same commit, so a machine that still
    holds the old key absorbs the new one — or `out-of-band`, which publishes no wrap and hands the
    key over with `export_ring`. It is explicit (no default) because the two answer different
    situations. The rotation is fast-forward only: a push that provably did not land restores the
    branch and drops the not-yet-published generation (``docs/design/05-key-generations.md``).

    The return is the new generation id — a result value, not a success flag (``sync`` likewise
    returns a report); a failure raises.
    """
    lock.acquire()
    try:
        return _rotate(
            transport=transport,
            files=files,
            cipher=cipher,
            wrapper=wrapper,
            scanner=scanner,
            store=store,
            codec=codec,
            pending=pending,
            remote=remote,
            branch=branch,
            mode=mode,
        )
    finally:
        lock.release()


def _rotate(
    *,
    transport: GitTransport,
    files: RepoFiles,
    cipher: Cipher,
    wrapper: GenerationWrap,
    scanner: SecretScanner,
    store: KeyStore,
    codec: RingCodec,
    pending: PendingStore,
    remote: str,
    branch: str,
    mode: RotationMode,
) -> int:
    if pending.is_pending():
        # Only `resolve` may run while a conflict is unresolved; a rotation would rewind the
        # collect commit the pending state points at (``docs/design/01-sync-model.md``).
        raise PendingConflict("a sync conflict is unresolved; run `syncintosh resolve` before rotating")
    transport.fetch(remote, branch)
    remote_commit = transport.resolve(remote_ref(branch))
    published: Mapping[int, str] = {}
    if remote_commit is not None:
        published = read_commitments(transport, remote_commit)
        # Gain a distributed generation first, so a rotation this machine was handed is not treated
        # as one it cannot read.
        absorb_wraps(
            transport=transport,
            wrapper=wrapper,
            codec=codec,
            store=store,
            commit=remote_commit,
            published=published,
        )
        # Report a genuine key disagreement **before** decrypting anything: a held key whose
        # commitment disagrees with the published one is `KeyGenerationMismatch`, but decrypting a
        # manifest under it would surface a misleading authentication failure instead.
        _reject_mismatched_held_keys(codec=codec, store=store, published=published)
        remote_manifest = _read_manifest(transport, cipher, remote_commit)
        local = transport.head()
        if local is not None and local != remote_commit:
            local_manifest = _read_manifest(transport, cipher, local)
            ahead = transport.is_ancestor(remote_commit, local)
            behind = transport.is_ancestor(local, remote_commit)
            if ahead and local_manifest.key_generation <= remote_manifest.key_generation:
                # An unpushed collect commit: `sync` publishes it, a rotation would rewind it.
                raise GitError(
                    "the local branch is ahead of the remote with unpushed changes; run `syncintosh sync` first"
                )
            if not ahead and not behind and local_manifest.key_generation == remote_manifest.key_generation:
                # A content divergence at one generation: `sync` merges it, a rotation cannot.
                raise GitError("the local branch and the remote have diverged; run `syncintosh sync` first")
            # Otherwise the local branch is behind, or ahead only by a generation the remote does
            # not have — a rotation that never landed. Either way, adopt the published tip.
        # Move to the published tip: a fast-forward when behind, a discard of a stale collect or an
        # unlanded rotation when ahead (the stray-generation sweep below removes its generation).
        transport.update_ref(branch, remote_commit)
        transport.materialize(remote_commit)
    head = transport.head()
    if head is None:
        raise GitError("there is nothing to rotate; run `syncintosh init` first")
    manifest = _read_manifest(transport, cipher, head)
    # Any held generation newer than the published one can only be a rotation that never landed.
    # Its wrap must go with it: leaving it in the working tree would publish an abandoned
    # generation on the next commit (``docs/design/05-key-generations.md``).
    for stray in [generation for generation in store.generations() if generation > manifest.key_generation]:
        files.delete(wrap_path(stray))
        store.drop(stray)
    if remote_commit is not None:
        # Checked after the sweep, so a stray generation the discard removed is not reported as a
        # held generation the repo does not commit to.
        verify_commitments(codec, store, published)
    new_id = manifest.key_generation + 1
    try:
        generation_key = os.urandom(GENERATION_KEY_SIZE)
        store.put(new_id, generation_key)
        contents = _object_plaintexts(transport, cipher, head, manifest)
        rotated = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=new_id, records=manifest.records)
        write_repo(
            files=files,
            cipher=cipher,
            scanner=scanner,
            head=manifest,
            new=rotated,
            contents=contents,
            force_reseal=True,
        )
        if mode == "out-of-band":
            # A removal or a compromise must not hand the removed machine the new key through the repo.
            files.delete(wrap_path(new_id))
        else:
            files.write(
                wrap_path(new_id), wrapper.wrap(generation_key, key_id=new_id, under_id=manifest.key_generation)
            )
        files.write(COMMITMENTS_PATH, _commitments_for(store, codec, published))
        commit = transport.commit((head,), f"syncintosh: rotate key generation {new_id}", TRACKED_PATHS)
        transport.update_ref(branch, commit)
    except BaseException:
        # A rotation that never landed must leave nothing behind: the not-yet-published generation
        # goes back out of the ring, so it cannot answer `get(n)` for another machine's generation
        # and cannot be handed out by `key export`.
        _best_effort_discard(transport=transport, files=files, store=store, branch=branch, head=head, new_id=new_id)
        raise
    # The push is outside the discard guard: it can fail **after** the server applied it (a lost
    # response, or an interrupt), so an exception here is not proof the commit is absent. An
    # unresolved outcome keeps the generation; only a proven absence discards it.
    try:
        outcome: PushOutcome | None = transport.push(remote, branch, commit)
    except Exception:
        outcome = None
    if outcome != "ok":
        landed = _rotation_landed(transport, remote, branch, commit)
        if landed is True:
            return new_id
        if landed is None:
            raise GitError(
                "the push outcome could not be determined; the new generation was kept. Check the "
                "remote first (`syncintosh doctor` / `syncintosh sync`), and re-run "
                "`syncintosh key rotate` only if the rotation did not land"
            )
        _best_effort_discard(transport=transport, files=files, store=store, branch=branch, head=head, new_id=new_id)
        raise GitError("the push did not land; the rotation was discarded — re-run `syncintosh key rotate`")
    return new_id


def _reject_mismatched_held_keys(*, codec: RingCodec, store: KeyStore, published: Mapping[int, str]) -> None:
    """Refuse a held key whose commitment disagrees with the published one, before any decrypt.

    A held generation the repo does not commit to is an unlanded rotation, removed by the stray
    sweep, so it is skipped here rather than reported as a disagreement.
    """
    for key_id in store.generations():
        expected = published.get(key_id)
        if expected is None:
            continue
        key = store.get(key_id)
        if key is not None and codec.commitment(key) != expected:
            raise KeyGenerationMismatch(key_id, "the published commitment is not this machine's key for it")


def _rotation_landed(transport: GitTransport, remote: str, branch: str, commit: str) -> bool | None:
    """Whether a rejected push actually landed: `True`/`False`, or `None` when it cannot be known."""
    try:
        transport.fetch(remote, branch)
        tip = transport.resolve(remote_ref(branch))
        if tip is None:
            return False
        return tip == commit or transport.is_ancestor(commit, tip)
    except GitError:
        # The remote could not be reached to check; the outcome stays unknown.
        return None


def _best_effort_discard(
    *,
    transport: GitTransport,
    files: RepoFiles,
    store: KeyStore,
    branch: str,
    head: str,
    new_id: int,
) -> None:
    """Discard a failed rotation without masking the failure that led here.

    A leftover is repaired by the stray-generation sweep on the next `key rotate`, so a cleanup
    failure must not hide the original error — the same reasoning as `_discard_export`.
    """
    try:
        _discard_rotation(transport=transport, files=files, store=store, branch=branch, head=head, new_id=new_id)
    except Exception:
        return


def _discard_rotation(
    *,
    transport: GitTransport,
    files: RepoFiles,
    store: KeyStore,
    branch: str,
    head: str,
    new_id: int,
) -> None:
    """Undo a rotation that did not land: restore the branch and work tree, drop the generation."""
    transport.update_ref(branch, head)
    transport.materialize(head)
    files.delete(wrap_path(new_id))
    store.drop(new_id)


def _read_manifest(transport: GitTransport, cipher: Cipher, commit: str) -> Manifest:
    blob = transport.read_blob(commit, MANIFEST_PATH)
    if blob is None:
        raise GitError("the repo has no manifest to rotate")
    return loads_manifest(cipher.open(blob, domain="manifest", file_id=""))


def prune_generation(
    *,
    transport: GitTransport,
    store: KeyStore,
    lock: SyncLock,
    remote: str,
    branch: str,
    generation: int,
) -> None:
    """Drop a generation no reachable commit uses; refuse one that is still needed.

    A generation is needed when a reachable commit — this machine's HEAD or the remote's branch
    tip — holds an envelope encrypted under it, or when it is the manifest's current generation.
    The `key_id` is cleartext, so this is decided **without any key**
    (``docs/design/05-key-generations.md``). Reachability frees almost nothing: without rewriting
    history every commit stays reachable, so the only generation this can drop is one that was
    published but never used — an aborted rotation.
    """
    if generation == 1:
        # The master generation: a ring without it cannot be exported for recovery.
        raise PruneRefused("generation 1 is the master generation and cannot be pruned")
    lock.acquire()
    try:
        transport.fetch(remote, branch)
        tips = tuple(tip for tip in (transport.head(), transport.resolve(remote_ref(branch))) if tip is not None)
        if not tips:
            # No reachable history could be read (an unborn branch, or a branch name the remote does
            # not use): with nothing to compare against, "unreferenced" cannot be established, so
            # nothing may be dropped.
            raise PruneRefused("no reachable history to check against; nothing can be pruned safely")
        if generation in transport.generations_in_reachable_objects(tips):
            raise PruneRefused(f"generation {generation} is still referenced by reachable history")
        if store.get(generation) is None:
            # Nothing to drop: this machine never held it, or already dropped it.
            return
        store.drop(generation)
    finally:
        lock.release()


def _object_plaintexts(transport: GitTransport, cipher: Cipher, commit: str, manifest: Manifest) -> dict[str, bytes]:
    """Every record's plaintext, read and digest-checked, so a rotation re-seals the real content."""
    contents: dict[str, bytes] = {}
    for file_id, record in manifest.records.items():
        blob = transport.read_blob(commit, object_path(file_id))
        if blob is None:
            raise ContentDigestMismatch(file_id, record.sha256, "the object is missing")
        plaintext = cipher.open(blob, domain="object", file_id=file_id)
        actual = hashlib.sha256(plaintext).hexdigest()
        if actual != record.sha256:
            raise ContentDigestMismatch(file_id, record.sha256, actual)
        contents[file_id] = plaintext
    return contents


__all__ = [
    "export_ring",
    "import_ring",
    "init_new_repo",
    "prune_generation",
    "published_commitments",
    "remote_commitments",
    "rotate_repo",
]
