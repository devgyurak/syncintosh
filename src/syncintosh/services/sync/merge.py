# SPDX-License-Identifier: AGPL-3.0-or-later
"""Merge stage of the sync cycle: stale and diverged merges, conflict views, and settling.

`services.sync` is split by stage; see its package docstring for the cycle."""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from syncintosh.core.append import append_merge
from syncintosh.core.config import SyncConfig
from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import ContentDigestMismatch, ContentParseError, GitError
from syncintosh.core.keyed_array import ArrayRule, group_leaves
from syncintosh.core.manifest import MANIFEST_SCHEMA, FileFormat, Manifest, ManifestRecord
from syncintosh.core.models import ConflictAddress
from syncintosh.core.models import PendingConflict as PendingConflictRecord
from syncintosh.core.paths import collision_key
from syncintosh.core.policy import stamp_policies
from syncintosh.core.ports import Cipher, ContentAdapter, GitTransport, MergeDriver, RepoFiles, SecretScanner
from syncintosh.core.record_merge import (
    PathCollision,
    RecordConflict,
    RecordMergeResult,
    collision_key_of,
    merge_manifests,
    record_path_of,
)
from syncintosh.core.repo import object_path, plan_writes
from syncintosh.core.resolution import (
    Choice,
    CollisionChoice,
    CollisionKey,
    ConflictView,
    Decide,
    Picks,
    Proposal,
    RecordChoice,
    Side,
    digest_of,
    fingerprint,
)
from syncintosh.core.sessions import SessionSkipped
from syncintosh.core.tree import ABSENT, Absent, ElementId, KeyPath, Leaf, Node, Tree
from syncintosh.services.policy_gate import PolicyView
from syncintosh.services.repo_writer import write_repo
from syncintosh.services.sync._shared import (
    _Collect,
    _commit,
    _decode,
    _entry_object,
    _Presets,
    _project_merged,
    _read_manifest,
    _replace_sha,
    _sha256,
    _syntax_ok,
    _verified_object,
)
from syncintosh.services.sync.collect import _content_detail

# --------------------------------------------------------------------------- resolve


@dataclass(frozen=True)
class Resolution:
    """How one cycle treats its conflicts. A plain `sync` carries none of this.

    `resolving` marks a cycle that `resolve` runs: it may start from a pending state, and it clears
    that state once a push lands. `decide` is asked, every time a pass finds conflicts, for the ones
    the run has not answered yet. Its answer reruns the cycle with the picks so far; each pick applies
    only to a conflict with the same fingerprint. A pass that finds nothing new to answer records the
    conflicts as pending and stops (``docs/design/01-sync-model.md``, resolve). `rounds` bounds the
    reruns; every rerun answers at least one conflict the run had not answered before.
    """

    # Keyed by fingerprint, which identifies a conflict's values: a repr shows neither the keys nor the picks.
    picks: Picks = field(default=MappingProxyType({}), repr=False)
    decide: Decide | None = None
    resolving: bool = False
    rounds: int = 0


NO_RESOLUTION: Final = Resolution()


MAX_RESOLVE_ROUNDS: Final = 64


@dataclass(frozen=True)
class _Found:
    """A conflict a pass found. `views` is empty when it cannot be chosen, such as a missing object."""

    record: PendingConflictRecord
    views: tuple[ConflictView, ...] = ()


@dataclass(frozen=True)
class _Settled:
    """A content conflict settled to `data`, or left in conflict with its `detail` and `views`."""

    # The merged plaintext: kept out of `repr` so a traceback or a debug log never prints it.
    data: bytes | None = field(repr=False)
    detail: str = ""
    views: tuple[ConflictView, ...] = ()
    # An append-only file that was continued on both sides: `data` is the repository's copy (`core/append.py`).
    forked: bool = False


def _readable(data: bytes) -> str:
    text = _decode(data)
    return "(binary)" if text is None else text


def _subtree(tree: Tree, path: KeyPath) -> dict[KeyPath, Node]:
    """Everything a side holds at `path` or beneath it, keyed relative to `path`.

    A side may hold a whole object at a key where the other holds a scalar; the object is its value,
    so it is read and digested as a whole, not as the leaf at `path` alone.
    """
    return {key[len(path) :]: node for key, node in tree.entries.items() if key[: len(path)] == path}


def _subtree_text(subtree: dict[KeyPath, Node]) -> str:
    """A subtree as a person reads it: its JSON value, or a marker when the side has nothing there."""
    if not subtree:
        return "(absent)"
    return json.dumps(Tree.create(subtree).to_value(), ensure_ascii=False, default=str)


def _subtree_digest(subtree: dict[KeyPath, Node]) -> str:
    return digest_of(tuple(sorted(subtree.items(), key=lambda item: item[0])))


def _whole_fingerprint(file_id: str, base: bytes, local: bytes, remote: bytes) -> str:
    return fingerprint(file_id, None, _sha256(base), _sha256(local), _sha256(remote))


def _is_text(fmt: FileFormat, *sides: bytes) -> bool:
    """True when the record is text and every side decodes as UTF-8.

    The record's format decides first: a `binary` record is never text, however its bytes happen to
    decode. A text record with a side that does not decode is not text either.
    """
    return fmt != "binary" and all(_decode(side) is not None for side in sides)


def _whole_view(file_id: str, path: str, fmt: FileFormat, base: bytes, local: bytes, remote: bytes) -> ConflictView:
    """The whole file as one choice. Its three digests identify it; its text and diff are shown."""
    local_text = _readable(local)
    remote_text = _readable(remote)
    diff = "".join(
        difflib.unified_diff(
            local_text.splitlines(keepends=True), remote_text.splitlines(keepends=True), "local", "remote"
        )
    )
    return ConflictView(
        kind="file",
        file_id=file_id,
        path=path,
        key_path=None,
        readable=_is_text(fmt, base, local, remote),
        fingerprint=_whole_fingerprint(file_id, base, local, remote),
        base_text=_readable(base),
        local_text=local_text,
        remote_text=remote_text,
        diff=diff,
    )


def _parsed(adapter: ContentAdapter, base: bytes, local: bytes, remote: bytes) -> tuple[Tree, Tree, Tree] | None:
    """The three sides as trees, or `None` when one of them does not decode or parse."""
    base_text, local_text, remote_text = _decode(base), _decode(local), _decode(remote)
    if base_text is None or local_text is None or remote_text is None:
        return None
    try:
        return adapter.parse(base_text), adapter.parse(local_text), adapter.parse(remote_text)
    except ContentParseError:
        return None


def _key_view(
    file_id: str, path: str, key_path: KeyPath, base_tree: Tree, local_tree: Tree, remote_tree: Tree
) -> ConflictView:
    """One key path as a choice. The digests of its three leaves identify it."""
    base_part = _subtree(base_tree, key_path)
    local_part = _subtree(local_tree, key_path)
    remote_part = _subtree(remote_tree, key_path)
    return ConflictView(
        kind="key",
        file_id=file_id,
        path=path,
        key_path=key_path,
        fingerprint=fingerprint(
            file_id,
            key_path,
            _subtree_digest(base_part),
            _subtree_digest(local_part),
            _subtree_digest(remote_part),
        ),
        base_text=_subtree_text(base_part),
        local_text=_subtree_text(local_part),
        remote_text=_subtree_text(remote_part),
    )


def _element_step(element: ElementId) -> str:
    """An array element's identity as one key-path step, so its fingerprint can never match a key's."""
    return "\x00element:" + json.dumps([list(pair) for pair in element], ensure_ascii=False)


def _group_text(leaf: Leaf) -> str:
    """A group's bindings as a person reads them: their JSON, or a marker when the side has none."""
    if isinstance(leaf, Absent):
        return "(absent)"
    return json.dumps(Tree.create({(): leaf}).to_value(), ensure_ascii=False, default=str)


def _element_view(
    file_id: str,
    path: str,
    key_path: KeyPath,
    element: ElementId,
    base_part: Leaf,
    local_part: Leaf,
    remote_part: Leaf,
) -> ConflictView:
    """One binding group of an identified array as a choice. Its digests cover the group's bindings only."""
    return ConflictView(
        kind="key",
        file_id=file_id,
        path=path,
        key_path=key_path,
        element=element,
        fingerprint=fingerprint(
            file_id,
            (*key_path, _element_step(element)),
            digest_of(base_part),
            digest_of(local_part),
            digest_of(remote_part),
        ),
        base_text=_group_text(base_part),
        local_text=_group_text(local_part),
        remote_text=_group_text(remote_part),
    )


def _group_index(leaf: Leaf, rule: ArrayRule) -> dict[ElementId, Leaf]:
    """Every binding group of one side's array, indexed once; an unidentifiable array has none."""
    return group_leaves(leaf, rule) or {}


def _content_views(
    file_id: str,
    path: str,
    fmt: FileFormat,
    base: bytes,
    local: bytes,
    remote: bytes,
    adapter: ContentAdapter | None,
    arrays: Mapping[KeyPath, ArrayRule],
    addresses: tuple[ConflictAddress, ...],
) -> tuple[ConflictView, ...]:
    """The choices for one content conflict: one per conflicted key or binding group, or the whole file."""
    trees = _parsed(adapter, base, local, remote) if adapter is not None and addresses else None
    if trees is None:
        return (_whole_view(file_id, path, fmt, base, local, remote),)
    base_tree, local_tree, remote_tree = trees
    # Each array is indexed once per side, not scanned once per binding group it conflicts in.
    indexed: dict[KeyPath, tuple[dict[ElementId, Leaf], dict[ElementId, Leaf], dict[ElementId, Leaf]]] = {}
    views: list[ConflictView] = []
    for key_path, element in dict.fromkeys(addresses):
        if element is None:
            views.append(_key_view(file_id, path, key_path, base_tree, local_tree, remote_tree))
            continue
        if key_path not in indexed:
            rule = arrays[key_path]
            indexed[key_path] = (
                _group_index(base_tree.value(key_path), rule),
                _group_index(local_tree.value(key_path), rule),
                _group_index(remote_tree.value(key_path), rule),
            )
        base_leaves, local_leaves, remote_leaves = indexed[key_path]
        views.append(
            _element_view(
                file_id,
                path,
                key_path,
                element,
                base_leaves.get(element, ABSENT),
                local_leaves.get(element, ABSENT),
                remote_leaves.get(element, ABSENT),
            )
        )
    return tuple(views)


def _content_pick(pick: Choice | None) -> Side | Proposal | None:
    """The side or the proposed value a pick names, or `None` for a pick that names neither."""
    match pick:
        case "local" | "remote" | Proposal():
            return pick
        case _:
            return None


def _checked(
    presets: _Presets, data: bytes, path: str, file_id: str, fmt: FileFormat, base: bytes, local: bytes, remote: bytes
) -> _Settled:
    """A merged result passes its syntax check whoever decided it; a failure is a conflict again."""
    if _syntax_ok(presets, data, path):
        return _Settled(data=data)
    whole = _whole_view(file_id, path, fmt, base, local, remote)
    return _Settled(data=None, detail="the merged result would not pass its syntax check", views=(whole,))


# The rounds that settle a content conflict are a guard: no pick adds a conflict address (see `_settle_content`), so
# the cap is a backstop for a merge that did.
_MAX_SETTLE_ROUNDS = 8


def _settle_content(
    *,
    presets: _Presets,
    driver: MergeDriver,
    file_id: str,
    path: str,
    fmt: FileFormat,
    base: bytes,
    local: bytes,
    remote: bytes,
    picks: Picks,
    reason: str,
) -> _Settled:
    """Merge one content conflict, applying each pick whose fingerprint this conflict still has.

    A whole-file pick settles the file outright; otherwise each key pick settles its key, and what
    the merge still cannot settle is reported with the choices it needs. Every settled result,
    however it was settled, goes through the syntax check.

    Picks are applied in rounds: each merge's views are read, the picks for views not yet settled are
    applied, and the merge runs again, until a round applies no new pick or the cap is reached.

    A pick does not add a conflict address: the addresses after a pick are a subset of those before it
    (`tests/unit/test_pick_addresses.py`). A pick can still make a render refuse, with no address, and then
    the file stays pending with the refusal's fixed reason. The rounds therefore guard against a merge that
    adds an address after a pick, which the core does not do; the cap bounds them whatever the merge does.
    """
    adapter = presets.adapter_for(fmt)
    policy = presets.policy_for(path)
    if policy is not None and policy.append_only:
        # A transcript is only extended: there is no base and no key to merge by (`core/append.py`). A fork is not a
        # conflict to ask about — the repository's copy is kept and the report says so — and a pick has nothing to pick.
        outcome = append_merge(local, remote)
        return _Settled(data=outcome.data, forked=outcome.forked)
    arrays = policy.array_rules if policy is not None else {}
    whole = _content_pick(picks.get(_whole_fingerprint(file_id, base, local, remote)))
    if isinstance(whole, Proposal) and not _is_text(fmt, base, local, remote):
        # A text proposal cannot settle a binary file: it would replace the bytes with a string.
        whole = None
    if whole is not None:
        settled = merge_content(base, local, remote, driver, adapter, choice=whole)
        if not settled.conflicted:
            return _checked(presets, settled.data, path, file_id, fmt, base, local, remote)
    merged = merge_content(base, local, remote, driver, adapter, arrays=arrays)
    key_choices: dict[KeyPath, Side | Proposal] = {}
    element_picks: dict[tuple[KeyPath, ElementId], Side | Proposal] = {}
    applied: set[str] = set()
    # The views are read again from each merge, so a conflict that a merge shows still gets its own pick. A pick adds no
    # conflict address (see the docstring), so the loop is a guard: a round that applies no new pick stops it, and the
    # cap bounds it.
    for _ in range(_MAX_SETTLE_ROUNDS):
        if not merged.conflicted:
            return _checked(presets, merged.data, path, file_id, fmt, base, local, remote)
        new_picks = False
        for view in _content_views(file_id, path, fmt, base, local, remote, adapter, arrays, merged.conflict_addresses):
            pick = _content_pick(picks.get(view.fingerprint))
            if view.key_path is None or pick is None or view.fingerprint in applied:
                continue
            applied.add(view.fingerprint)
            new_picks = True
            if view.element is None:
                key_choices[view.key_path] = pick
            else:
                element_picks[(view.key_path, view.element)] = pick
        if not new_picks:
            break
        merged = merge_content(
            base,
            local,
            remote,
            driver,
            adapter,
            key_choices=key_choices,
            arrays=arrays,
            element_picks=element_picks,
        )
    if not merged.conflicted:
        return _checked(presets, merged.data, path, file_id, fmt, base, local, remote)
    views = _content_views(file_id, path, fmt, base, local, remote, adapter, arrays, merged.conflict_addresses)
    detail = _content_detail(merged.conflict_addresses, reason, merged.reason, presets.when_tag)
    return _Settled(data=None, detail=detail, views=views)


def _record_fingerprint(conflict: RecordConflict) -> str:
    return fingerprint(
        conflict.file_id, None, digest_of(conflict.base), digest_of(conflict.local), digest_of(conflict.remote)
    )


def _collision_id(collision: PathCollision) -> str:
    return f"collision:{collision.scope}:{collision.machine}:{collision_key(collision.path)}"


def _collision_fingerprint(collision: PathCollision, local: Manifest, remote: Manifest) -> str:
    """A collision's identity is its folded path; its digests are the claims each side makes on it."""
    local_claims = tuple(local.records.get(file_id) for file_id in collision.file_ids)
    remote_claims = tuple(remote.records.get(file_id) for file_id in collision.file_ids)
    return fingerprint(
        _collision_id(collision), None, digest_of(None), digest_of(local_claims), digest_of(remote_claims)
    )


def _record_text(record: ManifestRecord | None) -> str:
    """A record's metadata as a person reads it. Its content digest is summarised, not printed."""
    if record is None:
        return "(absent)"
    machine = "-" if record.machine is None else record.machine
    return f"path={record.path} scope={record.scope} machine={machine} format={record.format} preset={record.preset}"


def _content_summary(conflict: RecordConflict) -> str:
    """Which side changed the content, so a choice of metadata can say what it keeps."""
    base, local, remote = conflict.base, conflict.local, conflict.remote
    if base is None or local is None or remote is None:
        return "content: the records differ between the sides"
    changed = [name for name, record in (("local", local), ("remote", remote)) if record.sha256 != base.sha256]
    if not changed:
        return "content: unchanged on both sides"
    if len(changed) == 2:
        return "content: changed on both sides, merged by content"
    return f"content: changed on {changed[0]} only; it is kept whichever side's metadata is picked"


def _record_views(result: RecordMergeResult, local: Manifest, remote: Manifest) -> tuple[ConflictView, ...]:
    """The record and collision conflicts of a merge, as choices a person can make on them."""
    views = [
        ConflictView(
            kind=conflict.kind,
            file_id=conflict.file_id,
            path=record_path_of(conflict),
            key_path=None,
            fingerprint=_record_fingerprint(conflict),
            local_present=conflict.local is not None,
            remote_present=conflict.remote is not None,
            base_text=_record_text(conflict.base),
            local_text=_record_text(conflict.local),
            remote_text=_record_text(conflict.remote),
            diff=_content_summary(conflict),
        )
        for conflict in result.conflicts
    ]
    views.extend(
        ConflictView(
            kind="collision",
            file_id=_collision_id(collision),
            path=collision.path,
            key_path=None,
            fingerprint=_collision_fingerprint(collision, local, remote),
        )
        for collision in result.collisions
    )
    return tuple(views)


def _as_record_choice(pick: Choice) -> RecordChoice:
    match pick:
        case "local" | "remote" | "drop" | "keep" | "delete":
            return pick
        case _:
            raise ValueError(f"{pick!r} is not a record choice")


def _as_collision_choice(pick: Choice) -> CollisionChoice:
    match pick:
        case "local" | "remote" | "drop":
            return pick
        case _:
            raise ValueError(f"{pick!r} is not a collision choice")


def _record_choices(
    first: RecordMergeResult, local: Manifest, remote: Manifest, picks: Picks
) -> tuple[dict[str, RecordChoice], dict[CollisionKey, CollisionChoice]]:
    """The picks that land on this merge's record and collision conflicts, keyed for `merge_manifests`."""
    records: dict[str, RecordChoice] = {}
    for conflict in first.conflicts:
        pick = picks.get(_record_fingerprint(conflict))
        if pick is not None:
            records[conflict.file_id] = _as_record_choice(pick)
    collisions: dict[CollisionKey, CollisionChoice] = {}
    for collision in first.collisions:
        pick = picks.get(_collision_fingerprint(collision, local, remote))
        if pick is not None:
            collisions[collision_key_of(collision)] = _as_collision_choice(pick)
    return records, collisions


def _merge_stale(
    *,
    config: SyncConfig,
    transport: PolicyView,
    cipher: Cipher,
    driver: MergeDriver,
    presets: _Presets,
    head_commit: str | None,
    collected: _Collect,
    resolution: Resolution,
) -> tuple[_Found, ...]:
    """Content-merge every local change whose ledger baseline is older than HEAD.

    The three inputs are the ones the diverged merge uses, with the base taken from the ledger
    rather than from a merge-base commit: the object the user actually edited (`entry.commit`),
    the live file, and HEAD's object. Pushing the live bytes instead would drop HEAD's change
    with no conflict (``docs/design/01-sync-model.md``).
    """
    found: list[_Found] = []
    for change in collected.stale:
        file_id = change.record.file_id
        base = _entry_object(transport, cipher, change.entry.commit, file_id, change.entry.sha256)
        head_bytes = _verified_object(transport, cipher, head_commit, file_id, change.record.sha256)
        if base is None or head_bytes is None:
            found.append(
                _Found(PendingConflictRecord(file_id, change.record.path, "the baseline or HEAD object is missing"))
            )
            continue
        settled = _settle_content(
            presets=presets,
            driver=driver,
            file_id=file_id,
            path=change.record.path,
            fmt=change.record.format,
            base=base,
            local=change.live,
            remote=head_bytes,
            picks=resolution.picks,
            reason="the same region changed here and in the repo since the last apply",
        )
        if settled.data is None:
            record = PendingConflictRecord(file_id, change.record.path, settled.detail, base, change.live, head_bytes)
            found.append(_Found(record, settled.views))
            continue
        projected = _project_merged(
            presets,
            config,
            settled.data,
            scope=change.record.scope,
            machine=change.record.machine,
            path=change.record.path,
            fmt=change.record.format,
        )
        collected.records[file_id] = _replace_sha(change.record, _sha256(projected))
        collected.contents[file_id] = projected
        collected.collected.append(change.record.path)
    return tuple(found)


# --------------------------------------------------------------------------- publish


def _merge_base(transport: GitTransport, local: str, remote: str) -> str | None:
    bases = transport.merge_bases(local, remote)
    if len(bases) > 1:
        raise GitError(f"criss-cross history: {len(bases)} merge bases for {local[:8]} and {remote[:8]}")
    return bases[0] if bases else None


@dataclass(frozen=True)
class _Merged:
    """The outcome of merging a diverged local and remote: a commit, or conflicts with their views."""

    commit: str | None
    conflicts: tuple[PendingConflictRecord, ...]
    base_commit: str | None = None
    views: tuple[ConflictView, ...] = ()
    session_skipped: tuple[SessionSkipped, ...] = ()


def _merge_diverged(
    *,
    config: SyncConfig,
    transport: PolicyView,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    driver: MergeDriver,
    presets: _Presets,
    branch: str,
    local: str,
    remote: str,
    local_manifest: Manifest,
    resolution: Resolution,
) -> _Merged:
    """Steps 8–12 for a diverged pair: decrypt, merge, gate, and build the two-parent commit."""
    base_commit = _merge_base(transport, local, remote)
    base_manifest = _read_manifest(transport, cipher, base_commit)
    remote_manifest = _read_manifest(transport, cipher, remote)
    if remote_manifest is None:
        raise GitError("the remote branch has no manifest; refusing to merge")
    first = merge_manifests(base_manifest, local_manifest, remote_manifest)
    result = first
    if resolution.picks:
        record_choices, collision_choices = _record_choices(first, local_manifest, remote_manifest, resolution.picks)
        result = merge_manifests(
            base_manifest,
            local_manifest,
            remote_manifest,
            choices=record_choices,
            collision_choices=collision_choices,
        )
    conflicts = list(_record_conflicts(result))
    views = list(_record_views(result, local_manifest, remote_manifest))

    contents: dict[str, bytes] = {}
    records = dict(result.merged)
    forked: list[SessionSkipped] = []
    for task in result.content_merges:
        base_bytes = _verified_object(transport, cipher, base_commit, task.file_id, task.base_sha256)
        local_bytes = _verified_object(transport, cipher, local, task.file_id, task.local_sha256)
        remote_bytes = _verified_object(transport, cipher, remote, task.file_id, task.remote_sha256)
        if base_bytes is None:
            # A content-merge task always has a base record, so a missing base object is a
            # corrupt repo — not `base = ∅`, which would merge as if the file were new.
            raise ContentDigestMismatch(task.file_id, task.base_sha256, "the base object is missing")
        if local_bytes is None or remote_bytes is None:
            conflicts.append(PendingConflictRecord(task.file_id, task.path, "a side's object is missing"))
            continue
        settled = _settle_content(
            presets=presets,
            driver=driver,
            file_id=task.file_id,
            path=task.path,
            fmt=task.format,
            base=base_bytes,
            local=local_bytes,
            remote=remote_bytes,
            picks=resolution.picks,
            reason="the same region changed on both sides",
        )
        if settled.data is None:
            conflicts.append(
                PendingConflictRecord(task.file_id, task.path, settled.detail, base_bytes, local_bytes, remote_bytes)
            )
            views.extend(settled.views)
            continue
        if settled.forked:
            forked.append(
                SessionSkipped.create(
                    session_id=presets.sessions.session_of(task.path) or task.path, reason="forked", path=task.path
                )
            )
        projected = _project_merged(
            presets, config, settled.data, scope=task.scope, machine=task.machine, path=task.path, fmt=task.format
        )
        records[task.file_id] = task.complete(_sha256(projected))
        contents[task.file_id] = projected
    if conflicts:
        return _Merged(commit=None, conflicts=tuple(conflicts), base_commit=base_commit, views=tuple(views))

    # Seal the merged result under the newer of the two sides' generations, never the older:
    # a rotation must not be undone by a content merge. A pairwise generation divergence is
    # refused earlier by `merge_manifests` (rotation is fast-forward only), so this floor is
    # defensive; a genuine cross-generation merge would need the full re-seal `key rotate`
    # performs, because `plan_writes` rewrites only objects whose digest changed
    # (``docs/design/05-key-generations.md``).
    merged_manifest = Manifest.create(
        schema=MANIFEST_SCHEMA,
        key_generation=max(local_manifest.key_generation, remote_manifest.key_generation),
        records=records,
        policy=stamp_policies(records.values(), presets.catalog),
    )
    # A migrated HEAD changes its manifest's stamp, so a collect commit exists by the time a merge is planned: the
    # local manifest read here is the stored one, and planning against it cannot skip a migrated object.
    plan = plan_writes(local_manifest, merged_manifest)
    # An object either side already stores under the merged digest is carried as that ciphertext; only a plaintext
    # this merge produced is sealed. `write_repo` opens and re-checks a carried blob, so nothing is trusted here.
    carried: dict[str, bytes] = {}
    for file_id in plan.objects_to_write:
        blob = _object_blob(transport, ((local, local_manifest), (remote, remote_manifest)), file_id, merged_manifest)
        if blob is not None:
            carried[file_id] = blob
            contents.pop(file_id, None)
        elif file_id not in contents:
            raise ContentDigestMismatch(file_id, merged_manifest.records[file_id].sha256, "no matching object")
    write_repo(
        files=files,
        cipher=cipher,
        scanner=scanner,
        head=local_manifest,
        new=merged_manifest,
        contents=contents,
        carried=carried,
    )
    commit = _commit(transport, branch, (local, remote), "syncintosh: merge")
    return _Merged(commit=commit, conflicts=(), base_commit=base_commit, session_skipped=tuple(forked))


def _object_blob(
    transport: GitTransport,
    sides: tuple[tuple[str, Manifest], ...],
    file_id: str,
    merged: Manifest,
) -> bytes | None:
    """The stored ciphertext of a surviving record, from the first side whose record has the merged digest.

    The blob is only read, never opened: `write_repo` is the one place that authenticates it and checks its digest.
    """
    expected = merged.records[file_id].sha256
    for commit, manifest in sides:
        record = manifest.records.get(file_id)
        blob = (
            None if record is None or record.sha256 != expected else transport.read_blob(commit, object_path(file_id))
        )
        if blob is not None:
            return blob
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
