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
* **A preset's policy is applied on the collect side.** The bytes that are hashed, staged,
  merged, and sealed are the *projection* — the file with the preset's noise and excluded keys
  removed — and apply re-attaches the local values from the live file
  (``docs/design/02-base-and-merge.md``, "Projection"). A noise-only change therefore reaches
  neither the digest nor the merge, and a Docker `auths` block is never sealed because it was
  never collected (``AGENTS.md`` §3.4).

`services` orchestrates the ports; it never imports `vcs`, `crypto`, `system`, or `adapters`
(``AGENTS.md`` §2.2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
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
    ProjectionError,
    SyntaxInvalid,
)
from syncintosh.core.ledger import AdoptChoice, chosen_digest, classify
from syncintosh.core.manifest import MANIFEST_SCHEMA, FileFormat, Manifest, ManifestRecord, mint_file_id, tracking_key
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.models import LedgerEntry, PendingState
from syncintosh.core.models import PendingConflict as PendingConflictRecord
from syncintosh.core.ports import (
    Cipher,
    ContentAdapter,
    DenyList,
    FileIdSource,
    GitTransport,
    KeyStore,
    LedgerStore,
    LiveFiles,
    MergeDriver,
    PendingStore,
    PresetCatalog,
    RepoFiles,
    RingCodec,
    SecretScanner,
    SyncLock,
    SyntaxValidator,
    remote_ref,
)
from syncintosh.core.presets import EMPTY_CATALOG, PresetFile, ValidatorName
from syncintosh.core.projection import project_text, restore_text
from syncintosh.core.record_merge import RecordConflict, RecordMergeResult, merge_manifests
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH, TRACKED_PATHS, object_path, plan_writes
from syncintosh.core.tree import KeyPath
from syncintosh.services.repo_writer import write_repo

# A push race is expected to settle in one or two tries; the cap stops a pathological fight from
# looping forever (``docs/design/01-sync-model.md`` step 13).
MAX_PUSH_ATTEMPTS = 4

# The adapter map a caller with no structural adapters passes: an immutable empty mapping, so the
# default argument cannot be mutated (``AGENTS.md`` §4).
NO_ADAPTERS: Mapping[FileFormat, ContentAdapter] = MappingProxyType({})

# The validator map a caller with no validators passes, for the same reason.
NO_VALIDATORS: Mapping[ValidatorName, SyntaxValidator] = MappingProxyType({})

# The catalog a caller with no built-in presets passes: the core null object, so the projection
# is the identity and `knows` makes no claim.
NO_CATALOG: PresetCatalog = EMPTY_CATALOG

# What apply decided for one record.
_Decision = Literal["write", "skip", "abort", "defer"]
_WRITE: _Decision = "write"
_SKIP: _Decision = "skip"
_ABORT: _Decision = "abort"
_DEFER: _Decision = "defer"

# Why a file a preset owns could not be reduced to its synced form. A fixed code, never the
# adapter's message: that message can name a key the file holds (``AGENTS.md`` §4).
ProjectionFailure = Literal["format-mismatch", "no-adapter", "not-utf-8", "unrenderable"]

# The sentence a user sees for each code. Fixed strings, so a report can never carry file content.
_PROJECTION_FAILURE: Mapping[ProjectionFailure, str] = MappingProxyType(
    {
        "format-mismatch": "the record's format is not the format the catalog uses",
        "no-adapter": "this build has no adapter for the file's format",
        "not-utf-8": "the file is not UTF-8",
        "unrenderable": "the file could not be reduced to its synced form",
    }
)


@dataclass(frozen=True)
class Refusal:
    """One path that was not written, with the reason — a fixed phrase, never the file's content.

    `blocking` separates a path this machine *may not* write (another machine's credential, a path
    outside `$HOME`, a built-in preset that does not own it) from one it simply **does not track**:
    the first is a failure the user has to act on, while the second is ordinary — every machine
    carries every record, and a machine that was set up with fewer presets is not broken. Making the
    second one fail the run would leave a script or a launchd job reporting a permanent error for a
    normal configuration.
    """

    path: str
    reason: str
    blocking: bool = True


@dataclass(frozen=True)
class _Presets:
    """The local preset policy the cycle runs under: paths, adapters, and syntax checks.

    Passed as one value so every step projects through the same catalog and adapter map — the
    collect side's digest and the apply side's restore must agree, or the ledger never settles.
    """

    catalog: PresetCatalog
    adapters: Mapping[FileFormat, ContentAdapter]
    validators: Mapping[ValidatorName, SyntaxValidator]

    def policy_for(self, path: str) -> PresetFile | None:
        """The strip/validate policy for `path`, or `None` when no preset owns it."""
        return self.catalog.policy_for(path)

    def adapter_for(self, fmt: FileFormat) -> ContentAdapter | None:
        """The structural adapter for `fmt`, or `None` when this build has none."""
        return self.adapters.get(fmt)


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
    refused: tuple[Refusal, ...] = ()
    unprojectable: tuple[Refusal, ...] = ()
    invalid: tuple[Refusal, ...] = ()
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
    # The projected bytes that would be sealed: the object content, keyed by file_id.
    contents: dict[str, bytes] = field(default_factory=dict)
    # The projected form of each live file that existed at collect, keyed by file_id. Apply reuses
    # it instead of projecting again whenever the raw bytes are unchanged since collect — the
    # compare-and-swap already proves the file is the one this projection was made from.
    views: dict[str, bytes] = field(default_factory=dict)
    # The bytes collect read from each live file, kept so apply can compare-and-swap a record it
    # is about to overwrite on an explicit adopt — even though it collected nothing for it. These
    # are the **raw** bytes, never the projection: an edit between collect and apply must abort
    # the write even when the edit touched only a key the preset strips.
    seen: dict[str, bytes] = field(default_factory=dict)
    # A local change whose ledger baseline is older than HEAD: it must be merged with HEAD's
    # content, not pushed over it.
    stale: list[_StaleChange] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    first_sight: list[str] = field(default_factory=list)
    symlinks: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    unprojectable: list[Refusal] = field(default_factory=list)


@dataclass(frozen=True)
class _StaleChange:
    """A local change against a stale ledger baseline, waiting for a content merge.

    The ledger baseline (`entry`) is the object the user actually edited; HEAD moved on since.
    Pushing the live content would silently drop HEAD's change, so it is merged with that
    baseline as base and HEAD's object as the other side
    (``docs/design/01-sync-model.md`` — "a stale base is ordinary"). `live` is the **projected**
    live content, so the merge runs on the same form the object holds.
    """

    record: ManifestRecord
    entry: LedgerEntry
    live: bytes


def _join(home: str, relpath: str) -> str:
    return str(Path(home) / relpath)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(data: bytes) -> str | None:
    """The file's text, or `None` when it is not UTF-8 (a binary record, a stray byte)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _replace_sha(record: ManifestRecord, sha256: str, *, format: FileFormat | None = None) -> ManifestRecord:
    """The same record with a new digest, and optionally with the format the object is really in.

    Collect passes its own format here: the object it seals was projected with the *local* catalog's
    format for the path, and a record that keeps an older format would describe the object wrongly —
    the format decides how a later scan reads the file (``docs/design/03-manifest.md``).
    """
    return ManifestRecord.create(
        file_id=record.file_id,
        path=record.path,
        preset=record.preset,
        scope=record.scope,
        machine=record.machine,
        format=record.format if format is None else format,
        sha256=sha256,
    )


def _find_item_record(records: Mapping[str, ManifestRecord], item: LocalItem) -> ManifestRecord | None:
    """The record tracking `item`, matched on the same folded key `core.manifest.track` uses."""
    wanted = tracking_key(item.scope, item.machine, item.path)
    return next(
        (record for record in records.values() if tracking_key(record.scope, record.machine, record.path) == wanted),
        None,
    )


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
    commit = transport.commit(parents, message, TRACKED_PATHS)
    transport.update_ref(branch, commit)
    return commit


# --------------------------------------------------------------------------- projection


@dataclass(frozen=True)
class _Projected:
    """One tracked file in both forms: what is on disk, and what would be sealed.

    `failure` is set when a preset owns the path but the file cannot be reduced to its synced
    form — a format with no adapter, a file that is not UTF-8, or a render that would not
    round-trip. That is a refusal, never a fallback to sealing the raw file: the raw file is the
    one holding the keys the preset exists to strip (``AGENTS.md`` §3.4).

    The reason is a fixed code rather than the adapter's message, so nothing content-derived
    (a JSONC duplicate key, say) is carried around in an object that a later change might render
    (``AGENTS.md`` §4, "never log secrets").
    """

    raw: bytes | None
    view: bytes | None
    failure: ProjectionFailure | None = None


def _project_file(presets: _Presets, raw: bytes | None, path: str, fmt: FileFormat) -> _Projected:
    """Reduce a live file to the bytes that would be sealed, or report why it cannot be."""
    policy = presets.policy_for(path)
    if policy is None:
        return _Projected(raw=raw, view=raw)
    if raw is None:
        # Nothing to project, and nothing to report: a missing file is "not applied here", which
        # the apply step already distinguishes from "the app is not installed".
        return _Projected(raw=None, view=None)
    if policy.format != fmt:
        return _Projected(raw=raw, view=None, failure="format-mismatch")
    if not policy.stripped:
        # Nothing to cut, so no adapter is needed and the projection is the file itself. This is
        # the `text` case: a shell script has no keys to strip.
        return _Projected(raw=raw, view=raw)
    adapter = presets.adapter_for(policy.format)
    if adapter is None:
        return _Projected(raw=raw, view=None, failure="no-adapter")
    text = _decode(raw)
    if text is None:
        return _Projected(raw=raw, view=None, failure="not-utf-8")
    try:
        view = project_text(adapter, text, policy).encode("utf-8")
    except ProjectionError:
        return _Projected(raw=raw, view=None, failure="unrenderable")
    return _Projected(raw=raw, view=view)


# Why a record's path may not be written here, as fixed phrases: a report names the location and
# the reason, never anything from the file (``AGENTS.md`` §4).
UNOWNED_PATH = "the path's preset does not own it"
_NOT_ALLOWED = "the path is not in this machine's config"
_DENIED = "a dangerous path this machine never collects"
_ESCAPES = "the target leaves this machine's home directory"
_RESTORE_FAILED = "this machine's own keys could not be re-attached to the file"
_SYNTAX_FAILED = "the file would not pass its syntax check"
_PROJECTION_FAILURE_MERGED = "the merged content could not be reduced to this machine's synced form"


def _refusal(path: str, failure: ProjectionFailure) -> Refusal:
    """The report row for a projection that failed, phrased by its fixed reason code."""
    return Refusal(path, _PROJECTION_FAILURE[failure])


def _record_refusal(
    presets: _Presets, config: SyncConfig, deny: DenyList, home: str, record: ManifestRecord
) -> Refusal | None:
    """Why `record` may not be written here, or `None` when it may.

    The first matching reason wins, most serious first: a record's `path` and `preset` arrive from
    the repo, so each check is a trust boundary (``docs/design/03-manifest.md``), and the user needs
    to know *which* one refused it.
    """
    if deny.is_denied(home, record.path):
        return Refusal(record.path, _DENIED)
    if deny.escapes(home, record.path):
        return Refusal(record.path, _ESCAPES)
    if not presets.catalog.knows(record.preset, record.path):
        return Refusal(record.path, UNOWNED_PATH)
    if not allows(config, record):
        # Not a refusal: this machine does not track the path, so the record is carried, not written.
        return Refusal(record.path, _NOT_ALLOWED, blocking=False)
    return None


def _strips(presets: _Presets, path: str) -> bool:
    """True when this machine's policy for `path` removes any key before sealing."""
    policy = presets.policy_for(path)
    return policy is not None and bool(policy.stripped)


def _project_merged(presets: _Presets, data: bytes, path: str, fmt: FileFormat) -> bytes:
    """A merge result as this machine's policy would seal it.

    The merge combines *stored* objects, and one of them can hold a key this machine's policy strips:
    another build published it, either because that build's preset did not exclude it or because the
    key arrived from the other side as an addition. Sealing the merge as it stands would commit that
    key again — a credential the other machine's build put in the repo — so the result is projected
    exactly as a live file is on the collect side. A projection that cannot be done refuses the seal
    rather than writing the raw bytes (``AGENTS.md`` §3.4).
    """
    if not _strips(presets, path):
        return data
    projected = _project_file(presets, data, path, fmt)
    if projected.failure is not None or projected.view is None:
        raise ProjectionError(f"{path}: {_PROJECTION_FAILURE_MERGED}")
    return projected.view


def _restore_file(presets: _Presets, repo_bytes: bytes, live_raw: bytes, policy: PresetFile) -> bytes | None:
    """`repo_bytes` with the live file's local values re-attached, or `None` on failure."""
    adapter = presets.adapter_for(policy.format)
    repo_text = _decode(repo_bytes)
    live_text = _decode(live_raw)
    if adapter is None or repo_text is None or live_text is None:
        return None
    try:
        return restore_text(adapter, repo_text, live_text, policy).encode("utf-8")
    except ProjectionError:
        return None


def _syntax_ok(presets: _Presets, data: bytes, path: str) -> bool:
    """True when `data` survives the preset's syntax check, or when the path has none.

    A path whose policy names a validator this build did not wire is **refused**, not passed: a
    validator that silently does nothing is worse than none, because the caller believes a check
    happened (``AGENTS.md`` §3.5).
    """
    policy = presets.policy_for(path)
    if policy is None or policy.validator is None:
        return True
    validator = presets.validators.get(policy.validator)
    text = _decode(data)
    if validator is None or text is None:
        return False
    try:
        validator.check(text, subject=path)
    except SyntaxInvalid:
        return False
    return True


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
    presets: _Presets,
) -> _Collect:
    """Steps 2–4: read the tracked paths, classify each, and stage what changed."""
    state = _Collect(records={} if head is None else dict(head.records))
    for item in config.items:
        if deny.is_denied(home, item.path):
            # The dangerous-path check comes first so a private key or a cloud credential is
            # reported as what it is, whatever its preset happens to own.
            state.skipped.append(item.path)
            continue
        if not presets.catalog.knows(item.preset, item.path):
            # The local config names a path the preset it claims does not own — a hand-edited
            # config, or a catalog that changed under it. Publishing it would create a record every
            # machine, this one included, refuses to apply, so the file would never settle and every
            # later sync would ask for `--adopt` again. Refused here, and reported.
            state.refused.append(Refusal(item.path, UNOWNED_PATH))
            continue
        live_path = _join(home, item.path)
        if live.is_symlink(live_path):
            # Collection copies regular files, never follows a link (AGENTS.md §2.4). Following
            # it would also leave the file with no baseline, so every later target edit would
            # read as a first-sight prompt.
            state.symlinks.append(item.path)
            continue
        projected = _project_file(presets, live.read(live_path), item.path, item.format)
        if projected.failure is not None:
            state.unprojectable.append(_refusal(item.path, projected.failure))
            continue
        record = _find_item_record(state.records, item)
        if record is not None and projected.raw is not None:
            # The raw bytes are the compare-and-swap reference apply checks. They are recorded
            # here, before the classification, so even a record that collects nothing (in-sync,
            # suppressed, first-sight) has a baseline to detect an edit made during the cycle.
            state.seen[record.file_id] = projected.raw
        live_sha = None if projected.view is None else _sha256(projected.view)
        if record is None:
            _collect_new(state, item, projected, live_sha, source)
        else:
            _collect_existing(state, item, record, projected, live_sha, ledger, adopt)
    return state


def _collect_new(
    state: _Collect,
    item: LocalItem,
    projected: _Projected,
    view_sha: str | None,
    source: FileIdSource,
) -> None:
    if projected.view is None or view_sha is None:
        state.deferred.append(item.path)
        return
    file_id = mint_file_id(state.records, source)
    state.records[file_id] = ManifestRecord.create(
        file_id=file_id,
        path=item.path,
        preset=item.preset,
        scope=item.scope,
        machine=item.machine,
        format=item.format,
        sha256=view_sha,
    )
    state.contents[file_id] = projected.view
    state.views[file_id] = projected.view
    if projected.raw is not None:
        state.seen[file_id] = projected.raw
    state.created.append(item.path)


def _collect_existing(
    state: _Collect,
    item: LocalItem,
    record: ManifestRecord,
    projected: _Projected,
    view_sha: str | None,
    ledger: Mapping[str, LedgerEntry],
    adopt: AdoptChoice | None,
) -> None:
    entry = ledger.get(record.file_id)
    verdict = classify(entry, view_sha, record.sha256)
    if projected.view is not None:
        # Cached for apply, which reuses it while the raw bytes are unchanged (item 10: one
        # projection per file per cycle, instead of one in each of collect, apply, and settle).
        state.views[record.file_id] = projected.view
    if verdict == "local-change" and projected.view is not None and view_sha is not None:
        if entry is not None and entry.sha256 != record.sha256:
            # The user edited the content the ledger last accounted for, while HEAD holds newer
            # content this machine never applied. Adopting the live bytes would drop HEAD's
            # change with no conflict, so merge instead (step 10, on a stale base).
            state.stale.append(_StaleChange(record=record, entry=entry, live=projected.view))
        else:
            state.records[record.file_id] = _replace_sha(record, view_sha, format=item.format)
            state.contents[record.file_id] = projected.view
            state.collected.append(item.path)
    elif verdict == "deferred":
        # Nothing to collect; the apply step decides whether this machine has a file to create
        # (the directory exists) or is genuinely deferred (the app is not installed).
        return
    elif verdict == "suppressed":
        state.suppressed.append(item.path)
    elif verdict == "first-sight" and projected.view is not None and view_sha is not None:
        _collect_first_sight(state, item, record, projected.view, view_sha, adopt)
    # `in-sync` and `pending-apply` collect nothing: the first is already settled, and the second
    # is finished by the apply step.


def _collect_first_sight(
    state: _Collect,
    item: LocalItem,
    record: ManifestRecord,
    view: bytes,
    view_sha: str,
    adopt: AdoptChoice | None,
) -> None:
    if adopt is None:
        state.first_sight.append(item.path)
        return
    if adopt == "remote":
        # Taking the repo's side: keep the record; apply writes the repo content over the file.
        return
    state.records[record.file_id] = _replace_sha(
        record, chosen_digest("local", local_sha256=view_sha, remote_sha256=record.sha256), format=item.format
    )
    state.contents[record.file_id] = view
    state.collected.append(item.path)


def _content_detail(paths: tuple[KeyPath, ...], reason: str) -> str:
    """A pending-conflict detail that names the colliding key paths, never their values (§4)."""
    if not paths:
        return reason
    return f"{reason}: {', '.join('.'.join(path) for path in paths)}"


def _merge_stale(
    *,
    transport: GitTransport,
    cipher: Cipher,
    driver: MergeDriver,
    presets: _Presets,
    head_commit: str | None,
    collected: _Collect,
) -> tuple[PendingConflictRecord, ...]:
    """Content-merge every local change whose ledger baseline is older than HEAD.

    The three inputs are the ones the diverged merge uses, with the base taken from the ledger
    rather than from a merge-base commit: the object the user actually edited (`entry.commit`),
    the live file, and HEAD's object. Pushing the live bytes instead would drop HEAD's change
    with no conflict (``docs/design/01-sync-model.md``).
    """
    conflicts: list[PendingConflictRecord] = []
    for change in collected.stale:
        file_id = change.record.file_id
        base = _verified_object(transport, cipher, change.entry.commit, file_id, change.entry.sha256)
        head_bytes = _verified_object(transport, cipher, head_commit, file_id, change.record.sha256)
        if base is None or head_bytes is None:
            conflicts.append(
                PendingConflictRecord(file_id, change.record.path, "the baseline or HEAD object is missing")
            )
            continue
        merged = merge_content(base, change.live, head_bytes, driver, presets.adapter_for(change.record.format))
        if not merged.conflicted and not _syntax_ok(presets, merged.data, change.record.path):
            # A structural merge can produce text that is valid for its format but not for its
            # language. Publishing it would break the user's shell, so it is a conflict.
            conflicts.append(
                PendingConflictRecord(
                    file_id,
                    change.record.path,
                    "the merged result would not pass its syntax check",
                    base,
                    change.live,
                    head_bytes,
                )
            )
            continue
        if merged.conflicted:
            conflicts.append(
                PendingConflictRecord(
                    file_id,
                    change.record.path,
                    _content_detail(
                        merged.conflict_paths,
                        "the same region changed here and in the repo since the last apply",
                    ),
                    base,
                    change.live,
                    head_bytes,
                )
            )
            continue
        projected = _project_merged(presets, merged.data, change.record.path, change.record.format)
        collected.records[file_id] = _replace_sha(change.record, _sha256(projected))
        collected.contents[file_id] = projected
        collected.collected.append(change.record.path)
    return tuple(conflicts)


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
        if expected is None:
            # A commitment exists for every generation; a held generation the repo does not
            # commit to is the same disable-the-check lever as deleting the file, one level finer.
            raise CommitmentsCorrupt(f"the repo publishes no commitment for generation {key_id}")
        key = store.get(key_id)
        if key is not None and codec.commitment(key) != expected:
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
    base_commit: str | None = None


def _merge_diverged(
    *,
    transport: GitTransport,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    driver: MergeDriver,
    presets: _Presets,
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
        if base_bytes is None:
            # A content-merge task always has a base record, so a missing base object is a
            # corrupt repo — not `base = ∅`, which would merge as if the file were new.
            raise ContentDigestMismatch(task.file_id, task.base_sha256, "the base object is missing")
        if local_bytes is None or remote_bytes is None:
            conflicts = (*conflicts, PendingConflictRecord(task.file_id, task.path, "a side's object is missing"))
            continue
        merged = merge_content(base_bytes or b"", local_bytes, remote_bytes, driver, presets.adapter_for(task.format))
        if not merged.conflicted and not _syntax_ok(presets, merged.data, task.path):
            conflicts = (
                *conflicts,
                PendingConflictRecord(
                    task.file_id,
                    task.path,
                    "the merged result would not pass its syntax check",
                    base_bytes,
                    local_bytes,
                    remote_bytes,
                ),
            )
            continue
        if merged.conflicted:
            conflicts = (
                *conflicts,
                PendingConflictRecord(
                    task.file_id,
                    task.path,
                    _content_detail(merged.conflict_paths, "the same region changed on both sides"),
                    base_bytes,
                    local_bytes,
                    remote_bytes,
                ),
            )
            continue
        projected = _project_merged(presets, merged.data, task.path, task.format)
        records[task.file_id] = task.complete(_sha256(projected))
        contents[task.file_id] = projected
    if conflicts:
        return _Merged(commit=None, conflicts=conflicts, base_commit=base_commit)

    # Every surviving record whose policy strips keys is read back and projected, even when the
    # merge itself did not touch its content: the merge commit this machine pushes names that object,
    # so a key the policy strips — published by another build — must not travel on in it. A record
    # with no strip list needs no read at all.
    for file_id, record in records.items():
        if file_id in contents or not _strips(presets, record.path):
            continue
        data = _object_content(transport, cipher, local, remote, file_id, record.sha256)
        if data is None:
            raise ContentDigestMismatch(file_id, record.sha256, "no matching object")
        projected = _project_merged(presets, data, record.path, record.format)
        if projected != data:
            records[file_id] = _replace_sha(record, _sha256(projected))
        contents[file_id] = projected

    merged_manifest = Manifest.create(
        schema=MANIFEST_SCHEMA, key_generation=local_manifest.key_generation, records=records
    )
    write_repo(files=files, cipher=cipher, scanner=scanner, head=local_manifest, new=merged_manifest, contents=contents)
    commit = _commit(transport, branch, (local, remote), "syncintosh: merge")
    return _Merged(commit=commit, conflicts=(), base_commit=base_commit)


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
    presets: _Presets,
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
        merged = _merge_diverged(
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
        )
        if merged.commit is None:
            return _Publish(conflicts=merged.conflicts, base_commit=merged.base_commit, remote_commit=remote_commit)
        outcome = transport.push(config.remote, branch, merged.commit)
        if outcome == "ok":
            return _Publish(published=merged.commit, base_commit=merged.base_commit, remote_commit=remote_commit)
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


# --------------------------------------------------------------------------- apply


@dataclass(frozen=True)
class _Applied:
    """What step 14 did with each record, by outcome."""

    applied: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    symlinks: tuple[str, ...] = ()
    aborted: tuple[str, ...] = ()
    refused: tuple[Refusal, ...] = ()
    unprojectable: tuple[Refusal, ...] = ()
    invalid: tuple[Refusal, ...] = ()


def _apply(
    *,
    transport: GitTransport,
    cipher: Cipher,
    presets: _Presets,
    target_commit: str,
    target_manifest: Manifest,
    seen: Mapping[str, bytes],
    views: Mapping[str, bytes],
    ledger: Mapping[str, LedgerEntry],
    live: LiveFiles,
    deny: DenyList,
    config: SyncConfig,
    home: str,
    adopt: AdoptChoice | None,
) -> _Applied:
    """Step 14: write the merged records into the live files, compare-and-swap checked.

    The bytes written come from `target_commit`, the published repo state — never from the
    collected local bytes, which after a merge are only one side of the result. `seen` is the
    other role those collected bytes play: the raw compare-and-swap reference for the file. When
    a preset owns the path the published bytes are first combined with the live file's own values
    at the preset's stripped keys, so an excluded credential the repo never held is left exactly
    as this machine has it (``AGENTS.md`` §3.4) — for a scalar value byte-identical, and for a
    nested object the value itself but re-rendered (``docs/design/02-base-and-merge.md``,
    "What a render is allowed to change").

    A record's `path` and `preset` arrive from the repo, so before writing anything this checks
    the target stays under `$HOME` (resolving the parent), is one this machine's local allowlist
    already tracks, and is one the local catalog lets that preset name
    (``docs/design/03-manifest.md``). A refusal is reported, never silent.
    """
    applied: list[str] = []
    deferred: list[str] = []
    symlinks: list[str] = []
    aborted: list[str] = []
    refused: list[Refusal] = []
    unprojectable: list[Refusal] = []
    invalid: list[Refusal] = []
    for file_id, record in target_manifest.records.items():
        if record.scope == "machine" and record.machine != config.machine_id:
            continue
        refusal = _record_refusal(presets, config, deny, home, record)
        if refusal is not None:
            refused.append(refusal)
            continue
        policy = presets.policy_for(record.path)
        if policy is not None and policy.format != record.format:
            # The record names a format the local catalog does not use for this path. Apply must
            # refuse it here rather than write it, because settle projects the bytes it reads with
            # the *record's* format and would refuse them: two verdicts for one record leaves the
            # file never settled, rewritten on every cycle.
            unprojectable.append(_refusal(record.path, "format-mismatch"))
            continue
        stripped_policy: PresetFile | None = policy if policy is not None and policy.stripped else None
        live_path = _join(home, record.path)
        if live.is_symlink(live_path):
            symlinks.append(record.path)
            continue
        live_bytes = live.read(live_path)
        live_view = live_bytes
        # The compare-and-swap reference is the raw bytes collect read from this file — never the
        # projection, and never the content this cycle will publish, which for a merged record is
        # not what the file holds.
        collect_baseline = seen.get(file_id)
        # Collect's projection stands in for this one only when the file is byte-for-byte the one
        # it was made from **and** the record names the format the policy uses: reusing it across a
        # format mismatch would apply a record on the strength of the file not having been touched,
        # while touching it would refuse it — the same input, two verdicts.
        reusable = (
            live_bytes is not None
            and live_bytes == collect_baseline
            and stripped_policy is not None
            and stripped_policy.format == record.format
        )
        cached = views.get(file_id) if reusable else None
        if cached is not None:
            live_view = cached
        elif live_bytes is not None and stripped_policy is not None:
            view = _project_file(presets, live_bytes, record.path, record.format)
            if view.failure is not None:
                unprojectable.append(_refusal(record.path, view.failure))
                continue
            live_view = view.view
        changed = live_bytes is not None and collect_baseline is not None and live_bytes != collect_baseline
        decision = _decision(
            record,
            live_bytes=live_bytes,
            live_view=live_view,
            changed=changed,
            entry=ledger.get(file_id),
            baseline=collect_baseline,
            adopt=adopt,
        )
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
        if live_bytes is not None and stripped_policy is not None:
            restored = _restore_file(presets, data, live_bytes, stripped_policy)
            if restored is None:
                # Never write a version whose local keys could not be re-attached: the repo's
                # bytes hold the stripped form, and writing them raw would delete a credential
                # or a machine-local preference (``AGENTS.md`` §3.4).
                unprojectable.append(Refusal(record.path, _RESTORE_FAILED))
                continue
            data = restored
        if not _syntax_ok(presets, data, record.path):
            invalid.append(Refusal(record.path, _SYNTAX_FAILED))
            continue
        live.write(live_path, data)
        applied.append(record.path)
    return _Applied(
        applied=tuple(applied),
        deferred=tuple(deferred),
        symlinks=tuple(symlinks),
        aborted=tuple(aborted),
        refused=tuple(refused),
        unprojectable=tuple(unprojectable),
        invalid=tuple(invalid),
    )


def _decision(
    record: ManifestRecord,
    *,
    live_bytes: bytes | None,
    live_view: bytes | None,
    changed: bool,
    entry: LedgerEntry | None,
    baseline: bytes | None,
    adopt: AdoptChoice | None,
) -> _Decision:
    """Whether apply may write `record` over the live path, per the conservative rules.

    `live_view` is the **projection** of the live file — the form whose digest the record and the
    ledger hold — while `baseline` is the raw bytes collect accounted for. `changed` is True when
    the raw file differs from that baseline: an edit made between collect and apply aborts the
    write, adopt included, even when the edit touched only a key the preset strips.
    """
    if live_bytes is None:
        # A missing file is not a deletion. Recreate a record this machine has never written, and
        # never one it wrote and that disappeared (that is the suppressed rule).
        return _WRITE if entry is None else _SKIP
    view = live_bytes if live_view is None else live_view
    in_sync = not changed and _sha256(view) == record.sha256 and entry is not None and not entry.suppressed
    if in_sync:
        return _SKIP
    if entry is None or entry.suppressed:
        # First sight, or a suppressed file that came back: overwrite only on an explicit adopt.
        if adopt != "remote":
            return _SKIP
        return _WRITE if baseline is not None and live_bytes == baseline else _ABORT
    if baseline is None:
        return _WRITE if _sha256(view) == entry.sha256 else _ABORT
    return _WRITE if live_bytes == baseline else _ABORT


def _settle_ledger(
    *,
    target_manifest: Manifest,
    target_commit: str,
    ledger: Mapping[str, LedgerEntry],
    live: LiveFiles,
    deny: DenyList,
    presets: _Presets,
    config: SyncConfig,
    home: str,
) -> dict[str, LedgerEntry]:
    """Rewrite the ledger so the next cycle sees in-agreement files as a normal baseline.

    Gated exactly like apply: this reads live paths, so a record that is denied, that escapes
    `$HOME`, that is not in the local allowlist, or that belongs to another machine is never
    opened — the deny-list refuses before anything is read (``AGENTS.md`` §3.4). The digest is
    the **projection's**, matching what collect and the record compare, so a file whose only
    difference is a stripped key stays settled instead of being re-collected on every cycle.
    """
    settled: dict[str, LedgerEntry] = {}
    for file_id, record in target_manifest.records.items():
        if record.scope == "machine" and record.machine != config.machine_id:
            continue
        if _record_refusal(presets, config, deny, home, record) is not None:
            continue
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
        digest = _ledger_digest(presets, live_bytes, record.path, record.format)
        if digest is not None and digest == record.sha256:
            settled[file_id] = LedgerEntry(sha256=digest, commit=target_commit, suppressed=False)
        else:
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = old
    return settled


def _ledger_digest(presets: _Presets, live_bytes: bytes, path: str, fmt: FileFormat) -> str | None:
    """The digest the ledger should hold for a live file, or `None` when it cannot be projected."""
    projected = _project_file(presets, live_bytes, path, fmt)
    if projected.failure is not None or projected.view is None:
        return None
    return _sha256(projected.view)


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
    adapters: Mapping[FileFormat, ContentAdapter] = NO_ADAPTERS,
    catalog: PresetCatalog = NO_CATALOG,
    validators: Mapping[ValidatorName, SyntaxValidator] = NO_VALIDATORS,
    adopt: AdoptChoice | None = None,
) -> SyncReport:
    """Run one sync cycle. A conflict returns a report; a hard failure raises."""
    presets = _Presets(catalog=catalog, adapters=adapters, validators=validators)
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
            presets=presets,
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
    presets: _Presets,
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
    if head_commit is not None:
        # Discard anything a previous, failed cycle left in the working tree before `git add -A`
        # can stage it — a partially written object or a stale manifest. The commits, not the
        # working tree, are the source of truth.
        transport.materialize(head_commit)
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
        presets=presets,
    )
    if collected.first_sight:
        # Present both sides before anything is written; the user picks with --adopt.
        return SyncReport(
            first_sight=tuple(collected.first_sight),
            symlinks=tuple(collected.symlinks),
            skipped=tuple(collected.skipped),
            refused=tuple(collected.refused),
            unprojectable=tuple(collected.unprojectable),
        )
    if collected.stale:
        stale_conflicts = _merge_stale(
            transport=transport,
            cipher=cipher,
            driver=driver,
            presets=presets,
            head_commit=head_commit,
            collected=collected,
        )
        if stale_conflicts:
            pending_store.record(
                PendingState.create(
                    local_commit=head_commit or "",
                    remote_commit=head_commit or "",
                    base_commit=None,
                    conflicts=stale_conflicts,
                )
            )
            return SyncReport(
                conflicts=stale_conflicts,
                collected=tuple(collected.collected),
                symlinks=tuple(collected.symlinks),
                skipped=tuple(collected.skipped),
                refused=tuple(collected.refused),
                unprojectable=tuple(collected.unprojectable),
            )

    generation = _current_generation(store, head_manifest)
    new_manifest = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=generation, records=collected.records)
    plan = plan_writes(head_manifest, new_manifest)
    # A live file that merely matches HEAD again (a crash between apply and the ledger save) must
    # not produce an empty commit; the write plan, not the collect list, decides (step 4–5).
    changed = plan.write_manifest or bool(plan.objects_to_write) or bool(plan.objects_to_delete)
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
        presets=presets,
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
            refused=tuple(collected.refused),
            unprojectable=tuple(collected.unprojectable),
        )

    target_commit = outcome.published or local_commit or ""
    target_manifest = _read_manifest(transport, cipher, target_commit) or new_manifest
    applied = _apply(
        transport=transport,
        cipher=cipher,
        presets=presets,
        target_commit=target_commit,
        target_manifest=target_manifest,
        seen=collected.seen,
        views=collected.views,
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
            deny=deny,
            presets=presets,
            config=config,
            home=home,
        )
    )
    return SyncReport(
        published_commit=outcome.published,
        created=tuple(collected.created),
        collected=tuple(collected.collected),
        applied=applied.applied,
        deferred=(*collected.deferred, *applied.deferred),
        suppressed=tuple(collected.suppressed),
        symlinks=tuple(dict.fromkeys((*collected.symlinks, *applied.symlinks))),
        aborted=applied.aborted,
        refused=tuple(dict.fromkeys((*collected.refused, *applied.refused))),
        unprojectable=tuple(dict.fromkeys((*collected.unprojectable, *applied.unprojectable))),
        invalid=applied.invalid,
        skipped=tuple(collected.skipped),
        unchanged=not changed and outcome.published in (None, local_commit),
    )


__all__ = ["MAX_PUSH_ATTEMPTS", "NO_CATALOG", "NO_VALIDATORS", "SyncReport", "sync"]
