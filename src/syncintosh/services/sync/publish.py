# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish stage of the sync cycle: the push, its retries, and abandoning an unpublished rotation.

`services.sync` is split by stage; see its package docstring for the cycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from syncintosh.core.config import SyncConfig
from syncintosh.core.errors import GenerationDivergence, GitError
from syncintosh.core.manifest import Manifest
from syncintosh.core.models import PendingConflict as PendingConflictRecord
from syncintosh.core.models import PendingPush
from syncintosh.core.ports import (
    Cipher,
    GenerationWrap,
    GitTransport,
    KeyStore,
    MergeDriver,
    RepoFiles,
    RingCodec,
    RotationMarkerStore,
    SecretScanner,
    remote_ref,
)
from syncintosh.core.repo import wrap_path
from syncintosh.core.resolution import ConflictView
from syncintosh.core.sessions import SessionSkipped
from syncintosh.services.keys import absorb_wraps, discard_stray_generations, read_commitments, verify_commitments
from syncintosh.services.policy_gate import PolicyView
from syncintosh.services.sync._shared import MAX_PUSH_ATTEMPTS, _Presets, _read_manifest
from syncintosh.services.sync.merge import Resolution, _merge_diverged


@dataclass(frozen=True)
class _Publish:
    """Where a publish attempt ended: a commit, conflicts, or nothing to publish."""

    published: str | None = None
    conflicts: tuple[PendingConflictRecord, ...] = ()
    base_commit: str | None = None
    remote_commit: str | None = None
    views: tuple[ConflictView, ...] = ()
    # Set when the remote had rotated ahead of our collect commit: the branch was moved to the
    # remote tip and the caller must re-collect on the new generation
    # (``docs/design/05-key-generations.md``).
    restart: bool = False
    # A fixed sentence the report must carry across the restart (an unpublished rotation was discarded).
    note: str | None = None
    # Sessions a diverged merge kept the repository's copy of (`_Settled.forked`).
    session_skipped: tuple[SessionSkipped, ...] = ()


# Shown when a hygiene rotation that never reached the remote is dropped; the rotation can simply be repeated.
ABANDONED_ROTATION_NOTE = (
    "Discarded the unpublished key rotation to generation {generation}, because the remote moved on without it. "
    "Run `syncintosh key rotate --hygiene` again if you still want to rotate."
)


def _landed(codec: RingCodec, store: KeyStore, marker: PendingPush, published: Mapping[int, str]) -> bool:
    """True when the remote publishes the marked generation under the key this machine holds for it."""
    expected = published.get(marker.generation)
    key = store.get(marker.generation)
    return expected is not None and key is not None and codec.commitment(key) == expected


def _abandon_unpublished_rotation(
    *,
    transport: PolicyView,
    files: RepoFiles,
    store: KeyStore,
    rotation_marker: RotationMarkerStore,
    branch: str,
    remote_commit: str,
    published: Mapping[int, str],
    marker: PendingPush | None,
    local_generation: int,
) -> _Publish | None:
    """Drop a local-only rotation the remote never saw; `None` when this is not that case.

    It applies only when `key rotate` left a pending-push marker for exactly the generation this branch
    holds (its push outcome was never learned), the marker's commit is in the local history, the remote does
    not publish that generation, and the local branch is not simply behind the remote: either it diverged
    from the remote, or it is ahead of it (the rotation, perhaps with collect commits on top, was never
    pushed). Without the marker the held generation stays a hard stop in `verify_commitments`, so a remote
    that omits or forges a commitment cannot talk this machine out of a key it really published, and every
    other held generation must be published, so the sweep can only ever drop the marked one. It runs
    **before** that check, which would otherwise stop the cycle on the held generation.

    * **Hygiene** (its `keys/<n>.wrap` is in the local tree): the remote tip is adopted, the stray generation
      leaves the ring, the marker is cleared and the cycle restarts. Nothing is lost: the apply ledger is
      saved only on apply, so the live edits are collected again.
    * **Out-of-band** (no wrap): refused. A removal or a compromise may be the reason for the rotation, and
      silently reverting it would leave the removed device with access.

    A generation the remote does publish is a real disagreement and is left to the normal checks.
    """
    if not published or marker is None or marker.generation != local_generation:
        return None
    local = transport.head()
    if local is None or local == remote_commit or transport.is_ancestor(local, remote_commit):
        return None
    if not transport.is_ancestor(marker.commit, local):
        return None
    remote_generation = max(published)
    if local_generation <= remote_generation or local_generation in published:
        return None
    if any(held != marker.generation and held not in published for held in store.generations()):
        # Another held generation the remote does not publish is not the marked rotation's doing: the sweep
        # below would drop it too on the remote's say-so, so leave it to `verify_commitments`.
        return None
    if transport.read_blob(local, wrap_path(local_generation)) is None:
        raise GenerationDivergence(local_generation, remote_generation, out_of_band=True)
    transport.update_ref(branch, remote_commit)
    transport.materialize(remote_commit)
    discard_stray_generations(files=files, store=store, above=remote_generation)
    rotation_marker.clear()
    return _Publish(
        restart=True,
        remote_commit=remote_commit,
        note=ABANDONED_ROTATION_NOTE.format(generation=local_generation),
    )


def _publish(
    *,
    transport: PolicyView,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    driver: MergeDriver,
    presets: _Presets,
    codec: RingCodec,
    wrapper: GenerationWrap,
    store: KeyStore,
    rotation_marker: RotationMarkerStore,
    config: SyncConfig,
    branch: str,
    local_commit: str | None,
    local_manifest: Manifest,
    resolution: Resolution,
) -> _Publish:
    """Steps 6–13: fetch, classify, merge, push, retry."""
    for _ in range(MAX_PUSH_ATTEMPTS):
        transport.fetch(config.remote, branch)
        remote_commit = transport.resolve(remote_ref(branch))
        if remote_commit is not None:
            # Absorb a distributed generation first, then check every held generation against the
            # published commitments — so a generation this fetch delivered is covered by the same
            # check as one this machine already held.
            published = read_commitments(transport, remote_commit)
            absorb_wraps(
                transport=transport,
                wrapper=wrapper,
                codec=codec,
                store=store,
                commit=remote_commit,
                published=published,
            )
            marker = rotation_marker.load()
            if marker is not None and _landed(codec, store, marker, published):
                # The rotation whose outcome was unknown did land, so the marker has done its job.
                rotation_marker.clear()
                marker = None
            abandoned = _abandon_unpublished_rotation(
                transport=transport,
                files=files,
                store=store,
                rotation_marker=rotation_marker,
                branch=branch,
                remote_commit=remote_commit,
                published=published,
                marker=marker,
                local_generation=local_manifest.key_generation,
            )
            if abandoned is not None:
                return abandoned
            verify_commitments(codec, store, published)
            # The policy gate, before any classification: a fast-forward would apply what a newer build wrote
            # just as a merge would. After the wraps are absorbed, because reading needs the remote's generation
            # (`services/policy_gate.py`).
            _read_manifest(transport, cipher, remote_commit)
        local = transport.head()
        if remote_commit is None:
            outcome = _push_or_raise(transport, config.remote, branch, local_commit)
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
            outcome = _push_or_raise(transport, config.remote, branch, local)
            if outcome == "ok":
                return _Publish(published=local, remote_commit=remote_commit)
            continue
        remote_manifest = _read_manifest(transport, cipher, remote_commit)
        if remote_manifest is not None and local_manifest.key_generation < remote_manifest.key_generation:
            # The remote rotated ahead of our collect commit. Rotation is never merged, and the
            # collect commit's objects are bound to the retired generation, so adopt the remote tip
            # and let the caller re-collect on the new generation — the live edits are still here,
            # and change detection is against the ledger, not HEAD (``core/ledger.py``).
            transport.update_ref(branch, remote_commit)
            transport.materialize(remote_commit)
            return _Publish(restart=True, remote_commit=remote_commit)
        merged = _merge_diverged(
            config=config,
            transport=transport,
            files=files,
            cipher=cipher,
            scanner=scanner,
            driver=driver,
            presets=presets,
            branch=branch,
            local=local,
            remote=remote_commit,
            local_manifest=local_manifest,
            resolution=resolution,
        )
        if merged.commit is None:
            return _Publish(
                conflicts=merged.conflicts,
                views=merged.views,
                base_commit=merged.base_commit,
                remote_commit=remote_commit,
            )
        outcome = transport.push(config.remote, branch, merged.commit)
        if outcome == "ok":
            return _Publish(
                published=merged.commit,
                base_commit=merged.base_commit,
                remote_commit=remote_commit,
                session_skipped=merged.session_skipped,
            )
        # Either way the merge commit did not land. Discard it and keep the collect commit, so a
        # stuck branch never holds an unpushed merge — stacking a second merge on one is what
        # produces criss-cross history.
        transport.update_ref(branch, local)
        transport.materialize(local)
        if outcome == "non-fast-forward":
            continue
        raise GitError("the push was rejected for a reason other than a non-fast-forward race")
    raise GitError(f"push retry limit ({MAX_PUSH_ATTEMPTS}) reached without converging")


def _push_or_raise(transport: GitTransport, remote: str, branch: str, commit: str | None) -> str:
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
