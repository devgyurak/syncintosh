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

import difflib
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from syncintosh.core.append import append_merge
from syncintosh.core.config import LocalItem, SyncConfig, allows, allows_path
from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import (
    ContentDigestMismatch,
    ContentParseError,
    DocumentRefused,
    DocumentStale,
    DocumentWrittenUnverified,
    GenerationDivergence,
    GitError,
    IgnoreListMalformed,
    PendingConflict,
    ProjectionError,
    SecretScanError,
    SyntaxInvalid,
)
from syncintosh.core.ignore import NO_IGNORES, IgnoreSpec
from syncintosh.core.keyed_array import ArrayRule, group_leaves
from syncintosh.core.ledger import AdoptChoice, chosen_digest, classify
from syncintosh.core.manifest import MANIFEST_SCHEMA, FileFormat, Manifest, ManifestRecord, mint_file_id, tracking_key
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.models import ConflictAddress, LedgerEntry, PendingPush, PendingState, Scope
from syncintosh.core.models import PendingConflict as PendingConflictRecord
from syncintosh.core.paths import collision_key
from syncintosh.core.policy import stamp_policies
from syncintosh.core.ports import (
    AgentProjectIndex,
    AppConfigDir,
    AppDetector,
    Cipher,
    ContentAdapter,
    DenyList,
    FileIdSource,
    GenerationWrap,
    GitTransport,
    KeyStore,
    LedgerStore,
    LiveFiles,
    MergeDriver,
    PendingStore,
    PresetCatalog,
    ProfileIndex,
    RepoFiles,
    RingCodec,
    RotationMarkerStore,
    RunningApps,
    SecretScanner,
    SessionActivity,
    SessionStore,
    SyncLock,
    SyntaxValidator,
    WhenTag,
    remote_ref,
)
from syncintosh.core.presets import EMPTY_CATALOG, MAX_GLOB_EXPANSION, Preset, PresetFile, PresetGlob, ValidatorName
from syncintosh.core.projection import Carry, DroppedItem, project_text, restore_with_drops
from syncintosh.core.record_merge import (
    PathCollision,
    RecordConflict,
    RecordMergeResult,
    collision_key_of,
    merge_manifests,
    record_path_of,
)
from syncintosh.core.repo import MANIFEST_PATH, TRACKED_PATHS, object_path, plan_writes, wrap_path
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
from syncintosh.core.sessions import (
    MAX_FILE_BYTES,
    SessionListing,
    SessionSkipped,
    SessionUnit,
    eligibility,
    within_project_cap,
)
from syncintosh.core.tree import ABSENT, Absent, ElementId, KeyPath, Leaf, Node, Tree, path_label
from syncintosh.services.keys import (
    NO_ROTATION_MARKER,
    absorb_wraps,
    discard_stray_generations,
    read_commitments,
    verify_commitments,
)
from syncintosh.services.policy_gate import PolicyView
from syncintosh.services.profile_paths import LivePaths, Located, Unresolved, open_live_paths, profile_directory
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


class _UnknownApps:
    """An `AppDetector` that answers `None` for everything.

    The detector a caller with none passes, so `apply` keeps its directory heuristic and never
    defers a record on a guess (``AGENTS.md`` §2.1, ``docs/design/01-sync-model.md`` step 14).
    """

    def installed(self, bundle_id: str) -> bool | None:
        return None


NO_APP_INFO: AppDetector = _UnknownApps()


class _UnknownRunning:
    """A `RunningApps` that answers `None` for everything.

    The port a caller with none passes. A preset that defers while its app runs treats `None` as running, so a
    caller that cannot tell never writes under an open app (``docs/design/presets.md``).
    """

    def running(self, bundle_id: str) -> bool | None:
        return None


NO_RUNNING_INFO: RunningApps = _UnknownRunning()


class _NoSessions:
    """A `SessionStore` that lists nothing and owns no path: the store a caller with no agent sessions passes."""

    def sessions(self) -> SessionListing:
        return SessionListing()

    def session_of(self, path: str) -> str | None:
        return None

    def note_for(self, path: str, data: bytes) -> str | None:
        return None


class _UnknownSessionActivity:
    """A `SessionActivity` that cannot tell: every session counts as open, so none is sealed or overwritten."""

    def is_open(self, session_id: str) -> bool | None:
        return None


NO_SESSION_STORE: SessionStore = _NoSessions()
NO_SESSION_ACTIVITY: SessionActivity = _UnknownSessionActivity()

# What apply decided for one record.
_Decision = Literal["write", "skip", "abort", "defer"]
_WRITE: _Decision = "write"
_SKIP: _Decision = "skip"
_ABORT: _Decision = "abort"
_DEFER: _Decision = "defer"

# Why a file a preset owns could not be reduced to its synced form. A fixed code, never the
# adapter's message: that message can name a key the file holds (``AGENTS.md`` §4).
ProjectionFailure = Literal["format-mismatch", "no-adapter", "not-utf-8", "unrenderable", "ignore-list"]

# The sentence a user sees for each code. Fixed strings, so a report can never carry file content.
_PROJECTION_FAILURE: Mapping[ProjectionFailure, str] = MappingProxyType(
    {
        "format-mismatch": "the record's format is not the format the catalog uses",
        "no-adapter": "this build has no adapter for the file's format",
        "not-utf-8": "the file is not UTF-8",
        "unrenderable": "the file could not be reduced to its synced form",
        "ignore-list": "the settings ignore list could not be read, so no file that obeys it is synced this run",
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
class Deferral:
    """A record this machine did not write, with why — and, for a missing app, how to get it.

    `download_url` and `cask` come from the **local built-in catalog**, matched by path — never
    from the record, whose preset name is untrusted. So a URL here is one this project shipped
    (``AGENTS.md`` §3.6), and the CLI may show it without the untrusted-domain warning.
    """

    path: str
    reason: str
    download_url: str | None = None
    cask: str | None = None


@dataclass(frozen=True)
class _Presets:
    """The local preset policy the cycle runs under: paths, adapters, and syntax checks.

    Passed as one value so every step projects through the same catalog and adapter map — the
    collect side's digest and the apply side's restore must agree, or the ledger never settles.
    """

    catalog: PresetCatalog
    adapters: Mapping[FileFormat, ContentAdapter]
    validators: Mapping[ValidatorName, SyntaxValidator]
    # The keyed tag a conflict names a `when` clause by; `None` shows it as hidden (`core/tree.py`).
    when_tag: WhenTag | None = None
    # The user's ignore list, read once for the cycle by `_read_ignores`, by the path of the file that holds it:
    # the spec, or the fixed reason the list could not be used. Empty until a cycle entry point fills it, so
    # building a `_Presets` reads nothing (``AGENTS.md`` §2.6).
    ignores: Mapping[str, IgnoreSpec | str] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    # The agent sessions this machine has and whether the agent has one open, and the cycle's clock reading (taken once,
    # so every session is judged idle against the same instant). The defaults list nothing and cannot tell, so a
    # cycle built without them seals no session.
    sessions: SessionStore = NO_SESSION_STORE
    activity: SessionActivity = NO_SESSION_ACTIVITY
    now: float = 0.0

    def append_only(self, path: str) -> bool:
        """True when `path` is a file that is only ever extended (`PresetFile.append_only`)."""
        policy = self.catalog.policy_for(path)
        return policy is not None and policy.append_only

    def policy_for(self, path: str) -> PresetFile | None:
        """The strip/validate policy for `path`, or `None` when no preset owns it."""
        return self.catalog.policy_for(path)

    def carried(self, policy: PresetFile) -> frozenset[KeyPath]:
        """The top-level keys this machine carries for `policy`'s file: the ignore list minus the preset's strip."""
        state = self.ignores.get(policy.path) if policy.ignore_list else None
        return state.paths_for(policy) if isinstance(state, IgnoreSpec) else frozenset()

    def ignore_failure(self, policy: PresetFile) -> str | None:
        """Why the ignore list `policy`'s file obeys cannot be used this cycle, or `None` when it can."""
        state = self.ignores.get(policy.path) if policy.ignore_list else None
        return state if isinstance(state, str) else None

    def projects(self, policy: PresetFile) -> bool:
        """True when the file is projected at all: it strips a key, or carries one."""
        return policy.strips or bool(self.carried(policy))

    def notes(self) -> tuple[str, ...]:
        """Fixed sentences about the ignore list that are not failures, for the report."""
        unmatched = sum(
            len(state.unmatched_cancellations) for state in self.ignores.values() if isinstance(state, IgnoreSpec)
        )
        if not unmatched:
            return ()
        return (_IGNORE_CANCEL_NOTE.format(count=unmatched),)

    def adapter_for(self, fmt: FileFormat) -> ContentAdapter | None:
        """The structural adapter for `fmt`, or `None` when this build has none."""
        return self.adapters.get(fmt)


# Reads a commit's manifest records, or `None` when that commit has no manifest. Injected so `Backup` does
# no I/O of its own (``AGENTS.md`` §2.6); the caller memoizes it, since every overwritten file asks.
RecordReader = Callable[[str], Mapping[str, ManifestRecord] | None]


@dataclass(frozen=True)
class Backup:
    """The commit this sync's apply works from: the collect commit when the cycle wrote one, else HEAD.

    `_apply` takes a `Backup` as a required keyword, so no write can be requested without naming
    this commit. The commit alone does not hold every overwritten file's previous bytes: a file an earlier
    cycle published but never applied has its bytes only in the ledger's commit. `_apply` therefore finds a
    restore point **per file** (`_restore_point`) and refuses an overwrite that no commit holds.
    `commit` is `None` only when nothing has been committed yet; `_apply` then refuses to overwrite a file
    that exists. `records_at` reads the manifest records of any commit it is asked about.
    """

    commit: str | None
    records_at: RecordReader | None = field(default=None, repr=False)

    @classmethod
    def create(cls, commit: str | None, records_at: RecordReader | None = None) -> Backup:
        """Name the restore point. Called only once the cycle has its collect commit, or its HEAD."""
        return cls(commit=commit, records_at=records_at)


@dataclass(frozen=True)
class DroppedLocal:
    """Local values an apply removed because the synced settings do not have the item they belonged to.

    `item` is the item's label (`core.tree.path_label`), `keys` how many local entries went with it. No value is
    held: an excluded value is in no commit, so a restore point cannot bring it back, and this report is the only
    account of the loss.
    """

    path: str
    item: str
    keys: int


@dataclass(frozen=True)
class RestorePoint:
    """The commit one overwritten file's previous bytes can be restored from."""

    path: str
    commit: str


@dataclass(frozen=True)
class SyncReport:
    """What one cycle did, for the CLI to render."""

    published_commit: str | None = None
    # The commit this cycle's apply worked from: the collect commit, or the HEAD it kept.
    backup_commit: str | None = None
    # Per overwritten file, the commit its previous bytes are in. Usually `backup_commit`; a file published
    # earlier but never applied is restored from the ledger's commit instead.
    restore_points: tuple[RestorePoint, ...] = ()
    # Local values an apply removed with an item the synced settings do not have; written files only
    # (see `DroppedLocal`).
    dropped: tuple[DroppedLocal, ...] = ()
    created: tuple[str, ...] = ()
    collected: tuple[str, ...] = ()
    applied: tuple[str, ...] = ()
    deferred: tuple[Deferral, ...] = ()
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
    # Fixed sentences about the cycle that are neither failures nor file content (the ignore list, today).
    notes: tuple[str, ...] = ()
    # Agent sessions left out of this cycle, each with a fixed reason (`core/sessions.py`). Never blocking.
    session_skipped: tuple[SessionSkipped, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        """True when the cycle stopped with unresolved conflicts."""
        return bool(self.conflicts)

    @property
    def needs_adopt(self) -> bool:
        """True when a first-sight record needs `--adopt local|remote`."""
        return bool(self.first_sight)

    @property
    def restore_groups(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """`(commit, paths)` for what to restore from, in first-seen order; `paths` is empty for the one-commit case.

        A run whose overwrites all share one commit (or that overwrote nothing, only created files) reports that
        commit alone, as before; only a run with several commits names the files under each.
        """
        if not self.applied:
            return ()
        by_commit: dict[str, list[str]] = {}
        for point in self.restore_points:
            by_commit.setdefault(point.commit, []).append(point.path)
        if len(by_commit) > 1:
            return tuple((commit, tuple(paths)) for commit, paths in by_commit.items())
        only = next(iter(by_commit), self.backup_commit)
        return () if only is None else ((only, ()),)


@dataclass
class _Collect:
    """The state one collect pass produces."""

    records: dict[str, ManifestRecord] = field(default_factory=dict)
    # The projected bytes that would be sealed: the object content, keyed by file_id.
    contents: dict[str, bytes] = field(default_factory=dict, repr=False)
    # The projected form of each live file that existed at collect, keyed by file_id. Apply reuses
    # it instead of projecting again whenever the raw bytes are unchanged since collect — the
    # compare-and-swap already proves the file is the one this projection was made from.
    views: dict[str, bytes] = field(default_factory=dict, repr=False)
    # The bytes collect read from each live file, kept so apply can compare-and-swap a record it
    # is about to overwrite on an explicit adopt — even though it collected nothing for it. These
    # are the **raw** bytes, never the projection: an edit between collect and apply must abort
    # the write even when the edit touched only a key the preset strips.
    seen: dict[str, bytes] = field(default_factory=dict, repr=False)
    # A local change whose ledger baseline is older than HEAD: it must be merged with HEAD's
    # content, not pushed over it.
    stale: list[_StaleChange] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)
    deferred: list[Deferral] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    first_sight: list[str] = field(default_factory=list)
    symlinks: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    unprojectable: list[Refusal] = field(default_factory=list)
    session_skipped: list[SessionSkipped] = field(default_factory=list)


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
    live: bytes = field(repr=False)


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


def _entry_object(transport: PolicyView, cipher: Cipher, commit: str | None, file_id: str, sha256: str) -> bytes | None:
    """The object a ledger entry accounts for: as the view serves it, else as it was stored.

    An entry's digest is of the object when the entry was made. After a policy migration the view serves that
    commit's object re-projected, so the stored object is what still matches an entry made before the
    migration (`services/policy_gate.py`).
    """
    try:
        return _verified_object(transport, cipher, commit, file_id, sha256)
    except ContentDigestMismatch:
        if file_id not in transport.migrated_contents(commit):
            raise
        return _verified_object(transport.raw, cipher, commit, file_id, sha256)


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


# --------------------------------------------------------------------------- ignore list


def _read_ignores(
    catalog: PresetCatalog,
    adapters: Mapping[FileFormat, ContentAdapter],
    live: LiveFiles,
    deny: DenyList,
    home: str,
) -> Mapping[str, IgnoreSpec | str]:
    """The user's ignore list for each file that holds one, read from the live file into memory.

    Once per cycle, and from the **default** profile's file only: a profile's own `settings.json` obeys the
    list its default counterpart holds. Nothing is collected or copied, and a change to the list is seen
    from the next cycle. A file that is **not there** has no list (VS Code has none either). A file that is
    there but cannot be used — a symlink, a denied path, one outside `$HOME`, or text that is not a list of
    strings — is a fixed reason string, which `_project_file` turns into a refusal for every file that obeys
    it (``AGENTS.md`` §4).
    """
    found: dict[str, IgnoreSpec | str] = {}
    for policy in catalog.ignore_list_files():
        full = _join(home, policy.path)
        if deny.is_denied(home, policy.path) or deny.escapes(home, policy.path) or live.is_symlink(full):
            # The list cannot be read, which is not the same as there being none: the default file is not
            # collected, but a profile's file obeys its list, and an empty one would seal the keys the user
            # meant to keep local and overwrite the other Macs' values with them.
            found[policy.path] = _IGNORE_LIST_UNREADABLE
            continue
        raw = live.read(full)
        if raw is None:
            found[policy.path] = NO_IGNORES
            continue
        found[policy.path] = _ignore_spec(adapters.get(policy.format), raw)
    return MappingProxyType(found)


def _ignore_spec(adapter: ContentAdapter | None, raw: bytes) -> IgnoreSpec | str:
    """The spec a settings file's text yields, or the fixed reason it does not."""
    text = _decode(raw)
    if adapter is None or text is None:
        return _IGNORE_LIST_UNREADABLE
    try:
        settings = adapter.parse(text).to_value()
    except ContentParseError:
        return _IGNORE_LIST_UNREADABLE
    if not isinstance(settings, dict):
        return NO_IGNORES
    try:
        return IgnoreSpec.create(settings)
    except IgnoreListMalformed:
        return _IGNORE_LIST_UNREADABLE


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

    raw: bytes | None = field(repr=False)
    view: bytes | None = field(repr=False)
    failure: ProjectionFailure | None = None


# Where a carried key's value comes from, read only when the file has one: the object this projection is
# measured against (`core.projection.Carry`). `None` from the reader means this machine holds no such object.
_Source = Callable[[], bytes | None]


# The reader a caller with no object to carry from passes: the carried keys are then left out of the seal.
def _no_source() -> bytes | None:
    return None


def _object_reader(transport: GitTransport, cipher: Cipher, commit: str | None, record: ManifestRecord) -> _Source:
    """A reader for `record`'s object at `commit`, verified against the record's digest when it is called."""

    def read() -> bytes | None:
        return _verified_object(transport, cipher, commit, record.file_id, record.sha256)

    return read


def _entry_reader(transport: PolicyView, cipher: Cipher, file_id: str, entry: LedgerEntry) -> _Source:
    """A reader for the object a ledger entry accounts for, verified against the entry's digest."""

    def read() -> bytes | None:
        return _entry_object(transport, cipher, entry.commit, file_id, entry.sha256)

    return read


def _baseline_reader(
    transport: PolicyView, cipher: Cipher, ledger: Mapping[str, LedgerEntry], head_commit: str | None
) -> Callable[[ManifestRecord], _Source]:
    """Readers for the object each carried key's value is taken from.

    The ledger's object when this machine has one: it is what change detection compares the live file with, so a
    key carried from it never reads as a local edit (`core.projection.Carry`). Otherwise the object at `HEAD`, for
    a file the repo holds and this machine has not accounted for yet; with neither, the keys are left out.
    """

    def of(record: ManifestRecord) -> _Source:
        def read() -> bytes | None:
            entry = ledger.get(record.file_id)
            if entry is not None:
                accounted = _entry_object(transport, cipher, entry.commit, record.file_id, entry.sha256)
                if accounted is not None:
                    return accounted
            return _verified_object(transport, cipher, head_commit, record.file_id, record.sha256)

        return read

    return of


_NO_SOURCE: _Source = _no_source


def _project_file(
    presets: _Presets,
    raw: bytes | None,
    path: str,
    fmt: FileFormat,
    *,
    source: _Source = _NO_SOURCE,
    carry: bool = True,
) -> _Projected:
    """Reduce a live file to the bytes that would be sealed, or report why it cannot be.

    `carry` False projects with the preset's static strip only: a merge result already holds the carried
    values of the objects it merged (``docs/design/02-base-and-merge.md``), so carrying again would replace
    them with an older object's. Otherwise `source` reads the object each carried key's value comes from.
    """
    policy = presets.policy_for(path)
    if policy is None:
        return _Projected(raw=raw, view=raw)
    if raw is None:
        # Nothing to project, and nothing to report: a missing file is "not applied here", which
        # the apply step already distinguishes from "the app is not installed".
        return _Projected(raw=None, view=None)
    if policy.format != fmt:
        return _Projected(raw=raw, view=None, failure="format-mismatch")
    if carry and presets.ignore_failure(policy) is not None:
        # The list that says which keys stay local cannot be read, so which keys to seal is unknown. Sealing
        # the file anyway would put a key the user meant to keep local into the repo (``AGENTS.md`` §3.4).
        return _Projected(raw=raw, view=None, failure="ignore-list")
    carried = presets.carried(policy) if carry else frozenset()
    if not policy.strips and not carried:
        # Nothing to cut, so no adapter is needed and the projection is the file itself. This is
        # the `text` case: a shell script has no keys to strip.
        return _Projected(raw=raw, view=raw)
    adapter = presets.adapter_for(policy.format)
    if adapter is None:
        return _Projected(raw=raw, view=None, failure="no-adapter")
    text = _decode(raw)
    if text is None:
        return _Projected(raw=raw, view=None, failure="not-utf-8")
    source_bytes = source() if carried else None
    try:
        view = project_text(
            adapter, text, policy, Carry(carried, None if source_bytes is None else _decode(source_bytes))
        )
    except ProjectionError:
        return _Projected(raw=raw, view=None, failure="unrenderable")
    return _Projected(raw=raw, view=view.encode("utf-8"))


# Why a record's path may not be written here, as fixed phrases: a report names the location and
# the reason, never anything from the file (``AGENTS.md`` §4).
UNOWNED_PATH = "the path's preset does not own it"
_NOT_ALLOWED = "the path is not in this machine's config"
_DENIED = "a dangerous path this machine never collects"
_ESCAPES = "the target leaves this machine's home directory"
_RESTORE_FAILED = "this machine's own keys could not be re-attached to the file"
_IGNORE_LIST_UNREADABLE = _PROJECTION_FAILURE["ignore-list"]
_SYNTAX_FAILED = "the file would not pass its syntax check"
_PROJECTION_FAILURE_MIGRATED = "the stored content could not be reduced to this machine's synced form"
_PROJECTION_FAILURE_MERGED = "the merged content could not be reduced to this machine's synced form"

# What the report says about an ignore list that is usable but has entries that do nothing. A count, never the
# entries: they are file content (``AGENTS.md`` §4). VS Code uses a leading "-" to subtract from its own default
# set, which this tool does not have; here it can only cancel what the user listed.
_IGNORE_CANCEL_NOTE = (
    '{count} entries of the settings ignore list start with "-" but cancel nothing you listed; '
    "a setting the preset manages cannot be switched back on this way"
)

# Why a record was not written, as fixed phrases (§4). A file this machine has never had is one
# thing, an app that is not installed is another, and the two call for different reactions — hence
# the distinct reasons and, for the second, the app's own download page.
_FILE_NOT_HERE = "the file is not on this machine yet"
_APP_NOT_INSTALLED = "the app is not installed on this machine"
_DIRECTORY_MISSING = "the file's directory is not on this machine yet"
_OBJECT_MISSING = "the stored content is missing from the repo"
_NO_RESTORE_POINT = "nothing is committed yet, so the file this would overwrite has no restore point"
_NOT_IN_RESTORE_POINT = "the file's current content is in no commit it could be restored from"
# Fixed: the scan names a location, never the matched bytes. `scan allow` is how a person accepts a false positive.
_RESTORE_SCAN_REFUSED = (
    "the file's current content failed the secret scan, so it could not be saved for restore; "
    "review it, or accept it with `syncintosh scan allow`"
)
_RESTORE_MESSAGE = "syncintosh: restore point before an overwrite"

# Why a new file under a glob was not published: the glob already covers the maximum this machine
# publishes, so a file that is not already a record under it is held back. A file that *is* already
# published keeps being collected — the guard is soft, so a concurrent sync that pushed the total over
# the bound cannot leave the glob permanently refused (``core.presets.MAX_GLOB_EXPANSION``).
_GLOB_FULL = "the glob is full, so this new file is not published"

# Why a glob's directory was refused: it is a symlink (which would list another tree under this
# preset's policy), a dangerous path, or outside `$HOME`. Checked before the directory is listed.
_GLOB_DIRECTORY_UNSAFE = "the glob's directory is a symbolic link or a dangerous path"
_PROFILE_DIRECTORY_UNSAFE = "the profile's directory is a symbolic link or a dangerous path"
_PROFILE_FOLDER_MISSING = "the profile has no folder on this machine yet; open the profile in the app once"
_AGENT_ROOT_MISSING = "the project is not on this machine yet; clone it at the mapped path to sync its data"
_CONFIG_FOLDER_MISSING = "the app's settings folder is not on this machine yet; open the app once to create it"
_APP_RUNNING = "the app is open (or cannot be told to be closed); quit it to apply this file"
# A document kept in a database row (`core/documents.py`) whose store refused to read or write it. The store's message
# is a fixed sentence (never the payload), so it is said as it is. The record and the ledger baseline are left as they
# were: a document the store would not open is not a document that was deleted.
_DOCUMENT_REFUSED = "the app's stored document was left as it is"
_DOCUMENT_UNVERIFIED = (
    "the app's stored document was written, but reading it back did not confirm it; check it before the next sync"
)
_DOCUMENT_MOVED = "the app saved this document while it was being written, so it was not changed; the next sync retries"


def _document_refusal(path: str, failure: DocumentRefused) -> Refusal:
    """The report row for a document the store would not read or write.

    A refusal is blocking: an unsupported database is something to act on. A row that moved between the read and the
    write is not — the next cycle starts from the app's new state — so it is reported without failing the run.
    """
    if isinstance(failure, DocumentStale):
        return Refusal(path, _DOCUMENT_MOVED, blocking=False)
    if isinstance(failure, DocumentWrittenUnverified):
        return Refusal(path, _DOCUMENT_UNVERIFIED)
    return Refusal(path, f"{_DOCUMENT_REFUSED}: {failure}")


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
    return policy is not None and policy.strips


def _project_merged(
    presets: _Presets,
    config: SyncConfig,
    data: bytes,
    *,
    scope: Scope,
    machine: str | None,
    path: str,
    fmt: FileFormat,
) -> bytes:
    """A merge result as this machine's policy would seal it.

    The merge combines *stored* objects, and one of them can hold a key this machine's policy strips:
    another build published it, either because that build's preset did not exclude it or because the
    key arrived from the other side as an addition. Sealing that *result* would commit the key again,
    so the content this machine creates is projected exactly as a live file is on the collect side,
    and a projection that cannot be done refuses the seal rather than writing the raw bytes
    (``AGENTS.md`` §3.4).

    **Only content this machine creates is projected, and only for a path it tracks.** An object the merge
    merely re-uses is served by the policy gate (`services/policy_gate.py`), which has already brought it to
    this build's policy or stopped the cycle, so it needs no second projection here. This one stays as the check
    on what the merge itself produces, because a stamp is a claim, not a proof. A path this machine does not
    track is not governed by this machine's policy either: the machine that does track it decides what its preset
    strips, and projecting it here would rewrite that machine's file — and abort the whole sync when
    that machine's record names a format the local catalog does not use.
    """
    if not allows_path(config, scope=scope, machine=machine, path=path) or not _strips(presets, path):
        return data
    projected = _project_file(presets, data, path, fmt, carry=False)
    if projected.failure is not None or projected.view is None:
        raise ProjectionError(f"{path}: {_PROJECTION_FAILURE_MERGED}")
    return projected.view


def static_projector(presets: _Presets) -> Callable[[bytes, str, FileFormat], bytes]:
    """The reducer a policy migration uses: `data` cut by the local static strip only, or `ProjectionError`.

    A migration re-projects a *stored* object, which already holds the carried values of the live files it came
    from, so the carry is not applied (``docs/design/02-base-and-merge.md``, "Projection"), and a projection that
    cannot be done raises rather than serving the unprojected bytes (``AGENTS.md`` §3.4).
    """

    def project(data: bytes, path: str, fmt: FileFormat) -> bytes:
        projected = _project_file(presets, data, path, fmt, carry=False)
        if projected.failure is not None or projected.view is None:
            raise ProjectionError(f"{path}: {_PROJECTION_FAILURE_MIGRATED}")
        return projected.view

    return project


def _restore_file(
    presets: _Presets, repo_bytes: bytes, live_raw: bytes, policy: PresetFile
) -> tuple[bytes, tuple[DroppedItem, ...]] | None:
    """`repo_bytes` with the live file's local values re-attached, and what that left out, or `None` on failure.

    The second element is the items the synced settings do not have, whose local values are not re-attached.

    The live file's ignored keys come back along with the preset's noise: they are this machine's own, and
    the object being applied is the carry source of the round-trip check (`core.projection.restore_text`).
    """
    adapter = presets.adapter_for(policy.format)
    repo_text = _decode(repo_bytes)
    live_text = _decode(live_raw)
    if adapter is None or repo_text is None or live_text is None:
        return None
    try:
        text, dropped = restore_with_drops(adapter, repo_text, live_text, policy, Carry(presets.carried(policy)))
        return text.encode("utf-8"), dropped
    except ProjectionError:
        return None


def _reduce_for_apply(
    presets: _Presets, data: bytes, live_bytes: bytes | None, policy: PresetFile, record: ManifestRecord
) -> tuple[bytes, tuple[DroppedItem, ...]] | str:
    """What apply writes for a stored object under a policy that projects, or the reason it must not be written.

    The bytes come with the items whose local values the write removes (none without a live file).

    The one place apply checks that an object is the local policy's projection, because the stamp that says so is
    a claim by whoever wrote the repo (``docs/design/03-manifest.md``). With a live file, this machine's own keys
    are re-attached and the round trip must give the object back; with none, the object must be its own static
    strip, since nothing else could tell a key this policy removes from one it keeps. Either way an object that
    cannot be reduced is never written raw (``AGENTS.md`` §3.4).
    """
    if live_bytes is not None:
        restored = _restore_file(presets, data, live_bytes, policy)
        return _RESTORE_FAILED if restored is None else restored
    reduced = _project_file(presets, data, record.path, record.format, carry=False)
    if reduced.failure is not None or reduced.view != data:
        return _PROJECTION_FAILURE_MIGRATED
    return data, ()


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


@dataclass(frozen=True)
class _Expanded:
    """The concrete paths to collect: the exact items plus each allowed glob expanded in place."""

    items: tuple[LocalItem, ...]
    refusals: tuple[Refusal, ...]
    deferrals: tuple[Deferral, ...] = ()


def _unsafe_glob_directories(
    config: SyncConfig, live: LiveFiles, deny: DenyList, home: str, paths: LivePaths
) -> tuple[PresetGlob, ...]:
    """The globs whose **directory** may not be used here.

    A glob whose directory is a symlink (say `snippets` → `~/.docker`) would list — or write into —
    another tree under *this* preset's policy, and for VS Code that policy strips nothing, so a
    credential the real owner excludes would be sealed (``AGENTS.md`` §2.4, §3.4). A denied directory
    or one outside `$HOME` is refused for the same reason. Checked on **both** ends: collect must not
    list it, and apply must not write through it.

    A logical directory is judged at the **real** directory it resolves to: the logical text names nothing on
    disk. One that does not resolve here is not unsafe — it lists nothing and is reported as deferred.
    """
    unsafe: list[PresetGlob] = []
    for entry in config.globs:
        directory = paths.resolve_directory(entry.preset, entry.glob.directory)
        if isinstance(directory, Unresolved):
            continue
        if deny.is_denied(home, directory) or deny.escapes(home, directory):
            unsafe.append(entry.glob)
            continue
        if directory and live.is_symlink(_join(home, directory)):
            unsafe.append(entry.glob)
    return tuple(unsafe)


def _bounded(
    glob: PresetGlob, matched: tuple[str, ...], head_records: tuple[ManifestRecord, ...]
) -> tuple[tuple[str, ...], tuple[Refusal, ...]]:
    """The files of a glob that may be published, and a non-blocking refusal for each one held back.

    A **soft** guard: a file already published keeps being collected, so an edit to it is never
    held back. New files fill the slots that are left, and only the ones past that are held — each
    reported by name, and only when there are any. A hard "refuse the whole glob" was a dead end: two
    machines syncing at once published 300 between them, and after that every cycle refused the glob
    with its local edits never leaving the machine.
    """
    published = {collision_key(record.path) for record in head_records if glob.matches(record.path)}
    known = tuple(path for path in matched if collision_key(path) in published)
    fresh = tuple(path for path in matched if collision_key(path) not in published)
    room = MAX_GLOB_EXPANSION - len(published)
    held = fresh[room:] if room >= 0 else fresh
    return (*known, *fresh[: max(room, 0)]), tuple(Refusal(path, _GLOB_FULL, blocking=False) for path in held)


def _expand_profiles(
    config: SyncConfig, head: Manifest | None, live: LiveFiles, deny: DenyList, home: str, paths: LivePaths
) -> _Expanded:
    """The logical items of every allowed profile this machine really has.

    One item per profile per file (decision D4), with the logical path a record carries; the live path
    is `paths.resolve`'s; the directory a glob lists is joined by `profile_directory`, the one place that
    does. A file whose resource the profile borrows from the default profile is **not** collected
    (decision D6) and is reported. A snippet glob lists the profile's own
    `snippets` directory, after that directory passes the same symlink/deny/escape checks a glob
    directory gets. Problems the profile index reported (a collision, a malformed entry, an unreadable
    list) are surfaced as non-blocking rows, so a person can see why a profile is missing.
    """
    head_records = () if head is None else tuple(head.records.values())
    items: list[LocalItem] = []
    refusals: list[Refusal] = []
    deferrals: list[Deferral] = []
    for entry in config.profiles:
        view = next((candidate for candidate in paths.views if candidate.spec == entry.spec), None)
        if view is None:
            continue
        spec = view.spec
        refusals.extend(Refusal(spec.index, problem, blocking=False) for problem in view.profiles.problems)
        for profile in view.profiles.entries:
            if not entry.allows(profile.name):
                continue
            for file in spec.files:
                logical = spec.logical_path(profile.name, file.path)
                # The default profile's own entry for this file: the pattern text of a glob is not itself a
                # path the catalog can match, so the preset's file is looked up by its declared path.
                owner = paths.catalog.get(entry.preset)
                policy = (
                    None
                    if owner is None
                    else next(
                        (candidate for candidate in owner.files if candidate.path == spec.default_path(file.path)), None
                    )
                )
                if policy is None:
                    continue
                if file.resource in profile.borrowed:
                    reason = f'the profile "{profile.name}" uses the default profile\'s {file.resource}'
                    deferrals.append(Deferral(logical, reason))
                    continue
                if policy.glob is None:
                    items.append(
                        LocalItem.create(
                            path=logical,
                            preset=entry.preset,
                            scope=entry.scope,
                            machine=entry.machine,
                            format=policy.format,
                        )
                    )
                    continue
                directory = profile_directory(spec, profile.directory) + (
                    "/" + file.path.rpartition("/")[0] if "/" in file.path else ""
                )
                if (
                    deny.is_denied(home, directory)
                    or deny.escapes(home, directory)
                    or live.is_symlink(_join(home, directory))
                ):
                    refusals.append(Refusal(logical, _PROFILE_DIRECTORY_UNSAFE))
                    continue
                pattern = PresetGlob.create(logical)
                matched, held = _bounded(pattern, pattern.expand(live.list_dir(_join(home, directory))), head_records)
                refusals.extend(held)
                items.extend(
                    LocalItem.create(
                        path=path, preset=entry.preset, scope=entry.scope, machine=entry.machine, format=policy.format
                    )
                    for path in matched
                )
    return _Expanded(items=tuple(items), refusals=tuple(refusals), deferrals=tuple(deferrals))


def _expand_agent_projects(
    config: SyncConfig, head: Manifest | None, live: LiveFiles, deny: DenyList, home: str, paths: LivePaths
) -> _Expanded:
    """The logical items of every project this machine mapped, for each preset it enabled for that project.

    One item per project per preset file, with the logical path a record carries; the live path is `paths.resolve`'s.
    A project with no usable folder here (a missing root, an environment override) is a non-blocking deferral, and a
    file under a relocated memory directory is left out the same way (decision D3). A glob lists the project's own
    directory after it passes the symlink/deny/escape checks a glob directory gets. Nothing is collected for a project
    or a preset the config does not name (decision D5).
    """
    head_records = () if head is None else tuple(head.records.values())
    items: list[LocalItem] = []
    refusals: list[Refusal] = []
    deferrals: list[Deferral] = []
    for project in config.agent_projects:
        for layout in project.layouts:
            owner = paths.catalog.get(layout.preset)
            view = next((candidate for candidate in paths.agent_views if candidate.spec == layout.spec), None)
            if owner is None or owner.agent_projects != layout.spec or view is None:
                continue
            for file in owner.files:
                relative = layout.spec.relative_of_template(file.path)
                if relative is None or file.append_only:
                    # An append-only file is a session's, enumerated per session by the store (`_collect_sessions`).
                    continue
                logical = layout.spec.logical_path(project.name, relative)
                located = paths.resolve_project(layout.spec, project.name, relative)
                if isinstance(located, Unresolved):
                    deferrals.append(Deferral(logical, located.reason))
                    continue
                if file.glob is None:
                    items.append(
                        LocalItem.create(path=logical, preset=layout.preset, scope="common", format=file.format)
                    )
                    continue
                directory = located.path.rpartition("/")[0]
                if (
                    deny.is_denied(home, directory)
                    or deny.escapes(home, directory)
                    or live.is_symlink(_join(home, directory))
                ):
                    refusals.append(Refusal(logical, _PROFILE_DIRECTORY_UNSAFE))
                    continue
                pattern = PresetGlob.create(logical)
                matched, held = _bounded(pattern, pattern.expand(live.list_dir(_join(home, directory))), head_records)
                refusals.extend(held)
                items.extend(
                    LocalItem.create(path=path, preset=layout.preset, scope="common", format=file.format)
                    for path in matched
                )
    return _Expanded(items=tuple(items), refusals=tuple(refusals), deferrals=tuple(deferrals))


def _real_path_refusal(live: LiveFiles, deny: DenyList, home: str, logical: str, located: Located) -> Refusal | None:
    """Why a profile or config-directory file's **real** path may not be used, or `None`.

    Checked on the resolved path at every read and write, not only when the index (or the app's metadata) was
    read: that is a snapshot, and a directory can become a symlink or a denied path between it and a write. The
    deny-list sees the real path here, which is the one a wildcard such as `JetBrains/*/ssl` is written for (the
    logical `@` element matches it too, but only the real folder is ever read). A path that is not a resolved
    logical one has no owner directory, and the ordinary checks already cover it.
    """
    owner = located.owner_directory
    if owner is None:
        return None
    if deny.is_denied(home, located.path):
        return Refusal(logical, _DENIED)
    if deny.escapes(home, located.path):
        return Refusal(logical, _ESCAPES)
    parent = located.path.rpartition("/")[0]
    # Every directory from the owner down to the file's parent, not only the two ends: a symlinked middle element
    # (`Release/a/` -> elsewhere in `$HOME`) passes the deny and escape checks and would still be written through.
    between = [owner]
    for part in parent[len(owner) :].split("/") if parent != owner else ():
        if part:
            between.append(f"{between[-1]}/{part}")
    if any(live.is_symlink(_join(home, each)) for each in between):
        return Refusal(logical, _PROFILE_DIRECTORY_UNSAFE)
    return None


def _expand_globs(
    config: SyncConfig, head: Manifest | None, live: LiveFiles, deny: DenyList, home: str, paths: LivePaths
) -> _Expanded:
    """Turn the config's exact items and its globs into one list of concrete items.

    A glob is one directory listing, never a walk (`core.presets.PresetGlob`): only the last element
    may be a pattern, so the expansion lists the fixed directory and matches the names.

    `MAX_GLOB_EXPANSION` bounds the files a glob **publishes**, as a soft guard: a file that is
    already a record under the glob keeps being collected, so an edit to it is never held back; new
    files fill the slots that are left, and only the ones past that are held — each reported by name,
    and only when there are any. Counting it here, rather than per record at apply time, is what keeps
    the bound from becoming a permanent failure: apply refusing each of 300 records would exit 1 with
    300 lines on every cycle, and the user would have no way out.

    The **directory itself** is checked before it is listed, not only the files inside it: a glob
    whose directory is a symlink (say `snippets` → `~/.docker`) would otherwise list another tree's
    files and collect them under *this* preset's policy — for VS Code, a policy that strips nothing —
    so a credential the real owner excludes would be sealed (``AGENTS.md`` §2.4, §3.4). A denied
    directory is refused for the same reason. The expanded paths are then handed back as ordinary
    `LocalItem`s, so `_collect` runs them through the **same** deny → ownership → symlink →
    projection checks an exact item gets: the directory is listed, but every file inside is judged on
    its own.

    A glob inside a versioned config directory is listed at the **resolved** real directory, never at its logical
    text; the paths it yields stay logical, because those are what a record carries. One whose directory does not
    resolve here lists nothing and is reported as deferred.
    """
    head_records = () if head is None else tuple(head.records.values())
    items = list(config.items)
    refusals: list[Refusal] = []
    deferrals: list[Deferral] = []
    unsafe = {glob.path for glob in _unsafe_glob_directories(config, live, deny, home, paths)}
    for entry in config.globs:
        real = paths.resolve_directory(entry.preset, entry.glob.directory)
        if isinstance(real, Unresolved):
            deferrals.append(Deferral(entry.glob.path, real.reason))
            continue
        if entry.glob.path in unsafe:
            refusals.append(Refusal(entry.glob.path, _GLOB_DIRECTORY_UNSAFE))
            continue
        matched, held = _bounded(entry.glob, entry.glob.expand(live.list_dir(_join(home, real))), head_records)
        refusals.extend(held)
        items.extend(
            LocalItem.create(
                path=path,
                preset=entry.preset,
                scope=entry.scope,
                machine=entry.machine,
                format=entry.format,
            )
            for path in matched
        )
    return _Expanded(items=tuple(items), refusals=tuple(refusals), deferrals=tuple(deferrals))


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
    paths: LivePaths | None = None,
    baseline: Callable[[ManifestRecord], _Source] = lambda record: _NO_SOURCE,
    scanner: SecretScanner | None = None,
    head_object: Callable[[ManifestRecord], bytes | None] = lambda record: None,
) -> _Collect:
    """Steps 2–4: read the tracked paths, classify each, and stage what changed.

    `baseline` reads, for a record, the object a carried key's value is taken from: the one the apply ledger
    last accounted for (`core.projection.Carry`). `head_object` reads HEAD's object for a record, which an append-only
    file is compared with (`core/append.py`); `scanner` pre-scans an agent session before anything of it is sealed.
    """
    paths = paths or LivePaths(catalog=presets.catalog)
    state = _Collect(records={} if head is None else dict(head.records))
    expanded = _expand_globs(config, head, live, deny, home, paths)
    profiled = _expand_profiles(config, head, live, deny, home, paths)
    projected = _expand_agent_projects(config, head, live, deny, home, paths)
    state.refused.extend((*expanded.refusals, *profiled.refusals, *projected.refusals))
    state.deferred.extend((*expanded.deferrals, *profiled.deferrals, *projected.deferrals))
    for item in (*expanded.items, *profiled.items, *projected.items):
        if presets.sessions.session_of(item.path) is not None:
            # A session file is collected with the rest of its session, below: alone it would skip the idle check and
            # the per-session refusal.
            continue
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
        located = paths.resolve(item.path)
        if isinstance(located, Unresolved):
            # A logical path with no real one here is never read: it ends as a report, and its record
            # (if any) stays in the manifest untouched.
            state.deferred.append(Deferral(item.path, located.reason))
            continue
        unsafe_profile = _real_path_refusal(live, deny, home, item.path, located)
        if unsafe_profile is not None:
            state.refused.append(unsafe_profile)
            continue
        live_path = _join(home, located.path)
        if live.is_symlink(live_path):
            # Collection copies regular files, never follows a link (AGENTS.md §2.4). Following
            # it would also leave the file with no baseline, so every later target edit would
            # read as a first-sight prompt.
            state.symlinks.append(item.path)
            continue
        record = _find_item_record(state.records, item)
        try:
            raw = live.read(live_path)
        except DocumentRefused as exc:
            # Neither "absent" nor "empty": the record stays in the manifest as it was, and nothing is collected.
            state.refused.append(_document_refusal(item.path, exc))
            continue
        projected = _project_file(
            presets,
            raw,
            item.path,
            item.format,
            source=_NO_SOURCE if record is None else baseline(record),
        )
        if projected.failure is not None:
            state.unprojectable.append(_refusal(item.path, projected.failure))
            continue
        if record is not None and projected.raw is not None:
            # The raw bytes are the compare-and-swap reference apply checks. They are recorded
            # here, before the classification, so even a record that collects nothing (in-sync,
            # suppressed, first-sight) has a baseline to detect an edit made during the cycle.
            state.seen[record.file_id] = projected.raw
        live_sha = None if projected.view is None else _sha256(projected.view)
        if record is None:
            _collect_new(state, item, projected, live_sha, source)
        else:
            _collect_existing(state, item, record, projected, live_sha, ledger, adopt, presets, head_object)
    if scanner is not None:
        _collect_sessions(
            state,
            head=head,
            ledger=ledger,
            live=live,
            deny=deny,
            home=home,
            adopt=adopt,
            source=source,
            presets=presets,
            scanner=scanner,
            head_object=head_object,
            paths=paths,
        )
    return state


def _collect_new(
    state: _Collect,
    item: LocalItem,
    projected: _Projected,
    view_sha: str | None,
    source: FileIdSource,
) -> None:
    if projected.view is None or view_sha is None:
        state.deferred.append(Deferral(item.path, _FILE_NOT_HERE))
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
    presets: _Presets,
    head_object: Callable[[ManifestRecord], bytes | None],
) -> None:
    if projected.view is not None and view_sha is not None and presets.append_only(item.path):
        # No ledger, no stale base: an append-only file is compared with HEAD's copy directly (`core/append.py`).
        state.views[record.file_id] = projected.view
        _collect_append_only(state, item, record, projected.view, view_sha, presets, head_object)
        return
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
            # The record this machine republishes carries the catalog's format for the path, the
            # one the object is really in — the same normalization the other collect paths do, so the
            # merge below projects a format it agrees with instead of aborting the sync. It is also how a
            # record the policy gate kept as stored (a format the catalog does not use) is replaced, under
            # its own file ID, by the machine that holds the file.
            state.stale.append(
                _StaleChange(
                    record=_replace_sha(record, record.sha256, format=item.format), entry=entry, live=projected.view
                )
            )
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


def _collect_append_only(
    state: _Collect,
    item: LocalItem,
    record: ManifestRecord,
    view: bytes,
    view_sha: str,
    presets: _Presets,
    head_object: Callable[[ManifestRecord], bytes | None],
) -> None:
    """Collect an append-only file only when it extends HEAD's copy (decision D2).

    Equal: nothing to do. HEAD's copy extends the local one: this machine is behind, and apply writes the extension.
    The local one extends HEAD's: it is collected. Neither: the session was continued on both Macs, so the
    repository's copy stays, the local one is **not** collected, and the report says so without blocking the cycle.
    """
    if view_sha == record.sha256:
        return
    session = presets.sessions.session_of(item.path) or item.path
    stored = head_object(record)
    if stored is None:
        state.session_skipped.append(SessionSkipped.create(session_id=session, reason="unknown", path=item.path))
        return
    outcome = append_merge(view, stored)
    if outcome.forked:
        state.session_skipped.append(SessionSkipped.create(session_id=session, reason="forked", path=item.path))
        return
    if outcome.longer == "local":
        state.records[record.file_id] = _replace_sha(record, view_sha, format=item.format)
        state.contents[record.file_id] = view
        state.collected.append(item.path)


def _collect_sessions(
    state: _Collect,
    *,
    head: Manifest | None,
    ledger: Mapping[str, LedgerEntry],
    live: LiveFiles,
    deny: DenyList,
    home: str,
    adopt: AdoptChoice | None,
    source: FileIdSource,
    presets: _Presets,
    scanner: SecretScanner,
    head_object: Callable[[ManifestRecord], bytes | None],
    paths: LivePaths,
) -> None:
    """Collect the agent sessions the store lists, one whole session at a time.

    A session is sealed only when it is idle and within the caps (`core/sessions.eligibility`); every file of it is
    read and scanned **before** any is staged, so one secret-scan hit leaves the whole session out and reports it by
    session, file and the scanner's own location text (decision D6) — the rest of the cycle goes on, and nothing of
    the session reaches `write_repo`, whose scan stays as the second wall (``AGENTS.md`` §3.4).
    """
    listing = presets.sessions.sessions()
    state.session_skipped.extend(listing.skipped)
    published = frozenset(
        found
        for record in (() if head is None else head.records.values())
        if (found := presets.sessions.session_of(record.path)) is not None
    )
    idle: list[SessionUnit] = []
    for unit in listing.units:
        why = eligibility(unit, now=presets.now, is_open=presets.activity.is_open(unit.session_id))
        if why is None:
            idle.append(unit)
        else:
            state.session_skipped.append(SessionSkipped.create(session_id=unit.session_id, reason=why))
    fitting, over = within_project_cap(idle, published=published)
    state.session_skipped.extend(SessionSkipped.create(session_id=unit.session_id, reason="cap") for unit in over)
    for unit in fitting:
        staged = _stage_session(
            state, unit, live=live, deny=deny, home=home, presets=presets, scanner=scanner, paths=paths
        )
        if staged is None:
            continue
        for item, raw in staged:
            record = _find_item_record(state.records, item)
            projected = _Projected(raw=raw, view=raw)
            if record is None:
                _collect_new(state, item, projected, _sha256(raw), source)
                continue
            state.seen[record.file_id] = raw
            _collect_existing(state, item, record, projected, _sha256(raw), ledger, adopt, presets, head_object)


def _stage_session(
    state: _Collect,
    unit: SessionUnit,
    *,
    live: LiveFiles,
    deny: DenyList,
    home: str,
    presets: _Presets,
    scanner: SecretScanner,
    paths: LivePaths,
) -> list[tuple[LocalItem, bytes]] | None:
    """Every file of `unit` as an item with its bytes, or `None` (reported) when any file may not be collected."""
    staged: list[tuple[LocalItem, bytes]] = []
    for file in unit.files:
        owner = presets.catalog.preset_for(file.path)
        if (
            owner is None
            or not presets.append_only(file.path)
            or not presets.catalog.knows(owner.name, file.path)
            or deny.is_denied(home, file.path)
            or deny.is_denied(home, file.live)
            or deny.escapes(home, file.live)
        ):
            state.session_skipped.append(
                SessionSkipped.create(session_id=unit.session_id, reason="unknown", path=file.path)
            )
            return None
        located = paths.resolve(file.path)
        if (
            isinstance(located, Unresolved)
            or located.path != file.live
            or _real_path_refusal(live, deny, home, file.path, located) is not None
        ):
            # The store's idea of where the file lives must be the resolver's, which is what apply writes through:
            # two answers for one record would collect one file and write another.
            state.session_skipped.append(
                SessionSkipped.create(session_id=unit.session_id, reason="unknown", path=file.path)
            )
            return None
        if live.is_symlink(_join(home, file.live)):
            # Collection copies regular files, never follows a link (``AGENTS.md`` §2.4).
            state.symlinks.append(file.path)
            state.session_skipped.append(
                SessionSkipped.create(session_id=unit.session_id, reason="unknown", path=file.path)
            )
            return None
        raw = live.read(_join(home, file.live))
        if raw is None:
            state.session_skipped.append(
                SessionSkipped.create(session_id=unit.session_id, reason="unknown", path=file.path)
            )
            return None
        if len(raw) > MAX_FILE_BYTES:
            # Listed sizes are a snapshot; the bytes read are what would be sealed.
            state.session_skipped.append(
                SessionSkipped.create(session_id=unit.session_id, reason="cap", path=file.path)
            )
            return None
        try:
            scanner.scan(raw, subject=file.path, format="text")
        except SecretScanError as exc:
            state.session_skipped.append(
                SessionSkipped.create(session_id=unit.session_id, reason="secret", path=file.path, detail=str(exc))
            )
            return None
        staged.append((LocalItem.create(path=file.path, preset=owner.name, scope="common", format="text"), raw))
    return staged


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


def _content_detail(
    addresses: tuple[ConflictAddress, ...], reason: str, why: str = "", when_tag: WhenTag | None = None
) -> str:
    """A pending-conflict detail that names the colliding key paths, never their values (§4).

    A binding is named by its `key` and `when` (its group): those are the structure a person picks by, while
    a binding's `command` and `args` are never named here.

    `path_label` renders each path the way a person reads the file, so a whole-document conflict —
    an array compared as one value — prints `(document)` rather than nothing, and two paths that differ
    (`"a.b"` as one key against `a` → `b`) do not print the same string. A conflict on one binding of
    an identified array names that binding too. The same label is what the `merge` command prints.
    """
    named = dict.fromkeys(path_label(path, element, when_tag=when_tag) for path, element in addresses)
    detail = reason if not named else f"{reason}: {', '.join(named)}"
    # A refusal the adapter gave (its fixed text) is said, so the person knows why the conflict stays one. An order
    # conflict is settled by choosing a whole side, not by a binding pick, and its reason says so.
    return f"{detail} ({why})" if why else detail


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


# --------------------------------------------------------------------------- apply


@dataclass(frozen=True)
class _Applied:
    """What step 14 did with each record, by outcome."""

    backup: str | None = None
    restore_points: tuple[RestorePoint, ...] = ()
    dropped: tuple[DroppedLocal, ...] = ()
    applied: tuple[str, ...] = ()
    deferred: tuple[Deferral, ...] = ()
    symlinks: tuple[str, ...] = ()
    aborted: tuple[str, ...] = ()
    refused: tuple[Refusal, ...] = ()
    unprojectable: tuple[Refusal, ...] = ()
    invalid: tuple[Refusal, ...] = ()
    session_skipped: tuple[SessionSkipped, ...] = ()
    notes: tuple[str, ...] = ()


def _missing_file_decision(installed: bool | None, *, parent_exists: bool) -> _Decision:
    """Whether `apply` may create a file that is absent and that this machine never wrote.

    This is the "no baseline" case: creating the file is the first apply here. App detection settles
    it when it can (``docs/design/01-sync-model.md`` step 14):

    * **the directory is already there** — `_WRITE`, whatever the detector said: a missing file in an
      existing settings directory means the app has been installed, and Spotlight can simply miss a
      bundle (indexing off, or the app under an excluded path), so a `False` must not defer forever
      a file the machine can clearly hold;
    * **the detector says installed** — `_WRITE`: create the parent directory and the file;
    * **otherwise** (no bundle ID for the path, or a detector that answered `False`/`None` and no
      directory) — `_DEFER`: report it with the app's download page rather than create a directory
      tree for an app that is not there.
    """
    if parent_exists or installed is True:
        return _WRITE
    return _DEFER


def _deferral(path: str, preset: Preset | None, installed: bool | None) -> Deferral:
    """The report row for a file `apply` did not create, carrying the app's download metadata.

    Only a definitive "not installed" gets the URL and the cask; a directory that is merely not
    there yet is reported without them, because no app is being suggested.
    """
    if installed is False and preset is not None:
        return Deferral(path, _APP_NOT_INSTALLED, download_url=preset.download_url, cask=preset.cask)
    return Deferral(path, _DIRECTORY_MISSING)


def _installed(apps: AppDetector, cache: dict[str, bool | None], bundle_id: str) -> bool | None:
    """`apps.installed`, memoized per bundle ID so one cycle queries each app once (§2.6)."""
    if bundle_id not in cache:
        cache[bundle_id] = apps.installed(bundle_id)
    return cache[bundle_id]


def _held_while_running(presets: _Presets, running: RunningApps, cache: dict[str, bool | None], path: str) -> bool:
    """True when `path`'s preset asks to wait for its app to quit and the app is open or cannot be told.

    The flag is the preset's data (`Preset.defer_while_running`), found by path from the local catalog and never from
    a record's preset name; this function knows no app. One query per bundle ID per cycle (§2.6).
    """
    owner = presets.catalog.preset_for(path)
    if owner is None or not owner.defer_while_running or owner.bundle_id is None:
        return False
    if owner.bundle_id not in cache:
        cache[owner.bundle_id] = running.running(owner.bundle_id)
    return cache[owner.bundle_id] is not False


def _apply(
    *,
    transport: PolicyView,
    cipher: Cipher,
    presets: _Presets,
    target_commit: str,
    target_manifest: Manifest,
    seen: Mapping[str, bytes],
    views: Mapping[str, bytes],
    ledger: Mapping[str, LedgerEntry],
    live: LiveFiles,
    deny: DenyList,
    apps: AppDetector,
    config: SyncConfig,
    home: str,
    adopt: AdoptChoice | None,
    backup: Backup,
    scanner: SecretScanner,
    unstored: frozenset[str] = frozenset(),
    paths: LivePaths | None = None,
    running: RunningApps = NO_RUNNING_INFO,
) -> _Applied:
    """Step 14: write the merged records into the live files, compare-and-swap checked.

    The bytes written come from `target_commit`, the published repo state — never from the
    collected local bytes, which after a merge are only one side of the result. `seen` is the
    other role those collected bytes play: the raw compare-and-swap reference for the file. When
    a preset owns the path the published bytes are first combined with the live file's own values
    at the preset's stripped keys, so an excluded credential the repo never held is left exactly
    as this machine has it (``AGENTS.md`` §3.4) — byte-identical, comments and layout included, when
    its text can be moved across as it stood; when a line beside it changed on the other machine, or a check
    fails, the value is the same but re-rendered, so a scalar may be re-serialized and a comment on its line
    dropped (``docs/design/02-base-and-merge.md``, "What a render is allowed to change").

    A record's `path` and `preset` arrive from the repo, so before writing anything this checks
    the target stays under `$HOME` (resolving the parent), is one this machine's local allowlist
    already tracks, and is one the local catalog lets that preset name
    (``docs/design/03-manifest.md``). A refusal is reported, never silent.

    An overwrite must replace only bytes some commit holds (`_restore_point`), or the write is refused. Bytes
    that no commit holds are scanned, sealed, and committed to the local-only restore ref first
    (`_save_restore_point`); a scan refusal leaves the file as it is. That covers a first-sight or suppressed
    record applied with `--adopt remote`, and `unstored`: the files whose live edit over a stale baseline this
    cycle merged into the published result (or settled by a pick), so the edit itself is in no commit.
    """
    paths = paths or LivePaths(catalog=presets.catalog)
    applied: list[str] = []
    restore_points: list[RestorePoint] = []
    dropped: list[DroppedLocal] = []
    deferred: list[Deferral] = []
    symlinks: list[str] = []
    aborted: list[str] = []
    refused: list[Refusal] = []
    unprojectable: list[Refusal] = []
    invalid: list[Refusal] = []
    session_skipped: list[SessionSkipped] = []
    notes: list[str] = []
    # One `mdfind` per app per cycle, however many files that app owns (§2.6).
    installed_cache: dict[str, bool | None] = {}
    running_cache: dict[str, bool | None] = {}
    # The same directory check the collect side makes: a record must not be *written* through a
    # symlinked glob directory either.
    unsafe = _unsafe_glob_directories(config, live, deny, home, paths)
    for file_id, record in target_manifest.records.items():
        if record.scope == "machine" and record.machine != config.machine_id:
            continue
        refusal = _record_refusal(presets, config, deny, home, record)
        if refusal is not None:
            refused.append(refusal)
            continue
        if any(glob.matches(record.path) for glob in unsafe):
            refused.append(Refusal(record.path, _GLOB_DIRECTORY_UNSAFE))
            continue
        located = paths.resolve(record.path)
        if isinstance(located, Unresolved):
            deferred.append(Deferral(record.path, located.reason))
            continue
        unsafe_profile = _real_path_refusal(live, deny, home, record.path, located)
        if unsafe_profile is not None:
            refused.append(unsafe_profile)
            continue
        if located.profile_directory is not None and not live.exists(_join(home, located.profile_directory)):
            # The profile is registered but its folder is not here yet. Creating it would hand the app a
            # profile it did not make, so the file waits (decision D5).
            deferred.append(Deferral(record.path, _PROFILE_FOLDER_MISSING))
            continue
        if located.config_directory is not None and not live.exists(_join(home, located.config_directory)):
            # The release folder is the app's own: its first run may import settings from an older release, and a
            # folder created here would make it skip that. Never created, so the file waits. Known limitation
            # (TOCTOU): `LiveFiles.write` later runs `mkdir(parents=True)`, so a folder removed between this check
            # and the write would be re-created; profiles share the pattern, and it is tracked as a follow-up.
            deferred.append(Deferral(record.path, _CONFIG_FOLDER_MISSING))
            continue
        if located.agent_root is not None and not live.exists(_join(home, located.agent_root)):
            # The project is mapped but not cloned here. Its agent folder is the agent's to make on a first run, but
            # data for a project that is not on this Mac would be an orphan under a name nothing will open.
            deferred.append(Deferral(record.path, _AGENT_ROOT_MISSING))
            continue
        policy = presets.policy_for(record.path)
        if policy is not None and policy.format != record.format:
            # The record names a format the local catalog does not use for this path. Apply must
            # refuse it here rather than write it, because settle projects the bytes it reads with
            # the *record's* format and would refuse them: two verdicts for one record leaves the
            # file never settled, rewritten on every cycle.
            unprojectable.append(_refusal(record.path, "format-mismatch"))
            continue
        if policy is not None and presets.ignore_failure(policy) is not None:
            # Refused whether or not the file is there: a new file would be written with no live copy of the keys
            # the list keeps local, and an existing one could not have them re-attached.
            unprojectable.append(_refusal(record.path, "ignore-list"))
            continue
        stripped_policy: PresetFile | None = policy if policy is not None and presets.projects(policy) else None
        live_path = _join(home, located.path)
        if live.is_symlink(live_path):
            symlinks.append(record.path)
            continue
        try:
            live_bytes = live.read(live_path)
        except DocumentRefused as exc:
            refused.append(_document_refusal(record.path, exc))
            continue
        live_view = live_bytes
        # The compare-and-swap reference is the raw bytes collect read from this file — never the
        # projection, and never the content this cycle will publish, which for a merged record is
        # not what the file holds.
        collect_baseline = seen.get(file_id)
        if policy is not None and policy.append_only:
            # Written only when missing or when the repo's copy strictly extends the local one, so the old bytes are
            # a prefix of the new and no restore point is needed (`core/append.py`, decision D10).
            data = _verified_object(transport, cipher, target_commit, file_id, record.sha256)
            if data is None:
                deferred.append(Deferral(record.path, _OBJECT_MISSING))
                continue
            if live_bytes is not None and collect_baseline is not None and live_bytes != collect_baseline:
                # The agent appended between collect and apply: a write now would cut that line off.
                aborted.append(record.path)
                continue
            if live_bytes == data or (live_bytes is None and ledger.get(file_id) is not None):
                # In sync, or a file this machine wrote and the person removed: never recreated.
                continue
            if live_bytes is not None and not data.startswith(live_bytes):
                # The local copy is longer or has forked: it is never shortened or replaced (collect reports a fork).
                continue
            session = presets.sessions.session_of(record.path)
            if session is None:
                # No store claims the path, so nothing can say the agent is done with it; "I could not tell" is not
                # evidence that it is closed (decision D10).
                session_skipped.append(SessionSkipped.create(session_id=record.path, reason="open", path=record.path))
                continue
            if presets.activity.is_open(session) is not False:
                session_skipped.append(SessionSkipped.create(session_id=session, reason="open", path=record.path))
                continue
            live.write(live_path, data)
            applied.append(record.path)
            if (note := presets.sessions.note_for(record.path, data)) is not None:
                notes.append(note)
            continue
        # Collect's projection stands in for this one only when the file is byte-for-byte the one it was made from.
        # A record whose format is not the catalog's never gets here (refused above), so the two agree on the format.
        # A file with carried keys is never reused: collect measured it against the ledger's object, while apply
        # measures it against the one it is about to write, and the two projections differ in the carried keys.
        reusable = (
            live_bytes is not None
            and live_bytes == collect_baseline
            and stripped_policy is not None
            and not presets.carried(stripped_policy)
        )
        cached = views.get(file_id) if reusable else None
        if cached is not None:
            live_view = cached
        elif live_bytes is not None and stripped_policy is not None:
            view = _project_file(
                presets,
                live_bytes,
                record.path,
                record.format,
                source=_object_reader(transport, cipher, target_commit, record),
            )
            if view.failure is not None:
                unprojectable.append(_refusal(record.path, view.failure))
                continue
            live_view = view.view
        changed = live_bytes is not None and collect_baseline is not None and live_bytes != collect_baseline
        entry = ledger.get(file_id)
        # With no collect baseline, `_decision` compares the view with the ledger digest. That digest is of the
        # ledger's object, so a file with carried keys is measured against that object here, not the one about
        # to be written: a remote change to a carried key would otherwise read as an edit and abort the write.
        # The restore-point check below needs the same view even when collect did read the file, because the
        # records a commit holds were projected against the ledger's object too.
        ledger_view = live_view
        if (
            entry is not None
            and live_bytes is not None
            and stripped_policy is not None
            and presets.carried(stripped_policy)
        ):
            measured = _project_file(
                presets,
                live_bytes,
                record.path,
                record.format,
                source=_entry_reader(transport, cipher, file_id, entry),
            )
            ledger_view = measured.view if measured.failure is None else live_view
        decision = _decision(
            record,
            live_bytes=live_bytes,
            live_view=live_view,
            ledger_view=ledger_view,
            changed=changed,
            entry=entry,
            baseline=collect_baseline,
            adopt=adopt,
        )
        if decision == _ABORT:
            aborted.append(record.path)
            continue
        if decision == _DEFER:
            deferred.append(Deferral(record.path, _DIRECTORY_MISSING))
            continue
        if decision == _SKIP:
            continue
        if live_bytes is None and ledger.get(file_id) is None:
            # First apply: the file is not here and this machine never wrote it. Whether to create
            # it depends on the app, so ask the local catalog (never the record's untrusted preset
            # name) and, when the path has a bundle ID, the detector.
            preset = presets.catalog.preset_for(record.path)
            bundle_id = None if preset is None else preset.bundle_id
            installed = None if bundle_id is None else _installed(apps, installed_cache, bundle_id)
            # A profile file's profile folder was checked above, so the app is there for it: its own
            # `snippets/` may be missing, and creating that inside a profile that exists is ordinary.
            parent_exists = located.owner_directory is not None or live.exists(str(Path(live_path).parent))
            if _missing_file_decision(installed, parent_exists=parent_exists) == _DEFER:
                deferred.append(_deferral(record.path, preset, installed))
                continue
        data = _verified_object(transport, cipher, target_commit, file_id, record.sha256)
        if data is None:
            deferred.append(Deferral(record.path, _OBJECT_MISSING))
            continue
        losses: tuple[DroppedItem, ...] = ()
        if stripped_policy is not None:
            reduced = _reduce_for_apply(presets, data, live_bytes, stripped_policy, record)
            if isinstance(reduced, str):
                unprojectable.append(Refusal(record.path, reduced))
                continue
            data, losses = reduced
        if not _syntax_ok(presets, data, record.path):
            invalid.append(Refusal(record.path, _SYNTAX_FAILED))
            continue
        if live_bytes != data and _held_while_running(presets, running, running_cache, record.path):
            # An IDE rewrites its settings when it quits, so a file written under it is lost (or fought over). Only
            # a write that would change something waits; collect already ran, so local edits keep flowing out.
            deferred.append(Deferral(record.path, _APP_RUNNING))
            continue
        if live_bytes is not None and live_bytes != data and backup.commit is None:
            refused.append(Refusal(record.path, _NO_RESTORE_POINT))
            continue
        if live_bytes is not None and live_bytes != data:
            if entry is None or entry.suppressed or file_id in unstored:
                # Its bytes are in no commit (an `--adopt remote` overwrite, or a stale-baseline edit): save them first.
                held_in = _save_restore_point(
                    transport=transport,
                    cipher=cipher,
                    scanner=scanner,
                    backup=backup,
                    key_generation=target_manifest.key_generation,
                    record=record,
                    view=ledger_view,
                )
            else:
                held_in = _restore_point(backup, file_id, ledger_view, entry.commit)
                if held_in is None:
                    held_in = Refusal(record.path, _NOT_IN_RESTORE_POINT)
            if isinstance(held_in, Refusal):
                refused.append(held_in)
                continue
            restore_points.append(RestorePoint(record.path, held_in))
        try:
            live.write(live_path, data)
        except DocumentRefused as exc:
            if not isinstance(exc, DocumentWrittenUnverified):
                # Nothing was written, so the restore point saved above is not one anything was overwritten for.
                # A write that was committed but not confirmed did change the database: its restore point stays.
                restore_points[:] = [point for point in restore_points if point.path != record.path]
            refused.append(_document_refusal(record.path, exc))
            continue
        applied.append(record.path)
        # Only a file that was written lost anything: a refusal, a deferral and a syntax failure left it as it was.
        dropped.extend(DroppedLocal(record.path, path_label(loss.item), loss.keys) for loss in losses)
    return _Applied(
        backup=backup.commit,
        restore_points=tuple(restore_points),
        dropped=tuple(dropped),
        applied=tuple(applied),
        deferred=tuple(deferred),
        symlinks=tuple(symlinks),
        aborted=tuple(aborted),
        refused=tuple(refused),
        unprojectable=tuple(unprojectable),
        invalid=tuple(invalid),
        session_skipped=tuple(session_skipped),
        notes=tuple(dict.fromkeys(notes)),
    )


def _save_restore_point(
    *,
    transport: GitTransport,
    cipher: Cipher,
    scanner: SecretScanner,
    backup: Backup,
    key_generation: int,
    record: ManifestRecord,
    view: bytes | None,
) -> str | Refusal:
    """Seal a file's current projected bytes into the local-only restore ref; the commit, or why not.

    The bytes are scanned first, exactly as a collect would scan them: the restore ref is encrypted and never
    pushed, but a secret the scan refuses stays out of every store this tool writes (``AGENTS.md`` §3.4). The
    seal uses the object domain and the record's file ID, the same AAD an object in the repo carries, so a
    restore commit's blob opens with `Cipher.open(domain="object", file_id=...)`. The projection is what is
    saved because it is what the overwrite replaces. An excluded value is in no commit, so it is not here: it stays
    in the live file unless the synced settings do not have the item it belongs to, and then it is gone and
    `SyncReport.dropped` is the only account of it.
    """
    if view is None:
        return Refusal(record.path, _NOT_IN_RESTORE_POINT)
    try:
        scanner.scan(view, subject=record.path, format=record.format)
    except SecretScanError:
        return Refusal(record.path, _RESTORE_SCAN_REFUSED)
    sealed = cipher.seal(view, domain="object", file_id=record.file_id, key_id=key_generation)
    return transport.commit_restore(backup.commit, {object_path(record.file_id): sealed}, _RESTORE_MESSAGE)


def _restore_point(backup: Backup, file_id: str, view: bytes | None, ledger_commit: str | None) -> str | None:
    """The commit that holds this file's current projected content byte for byte, or `None` when none does.

    The cycle's own commit is asked first: it holds what collect saw. A file an earlier cycle published but never
    applied is not in it (the repo moved on), so the ledger's commit — the last one this machine accounted for —
    is asked next; `key prune` refuses a generation that reachable history still uses, so it still decrypts.
    The digest is of the projection, the form a record holds; a file that cannot be projected is in neither.
    """
    if backup.records_at is None or view is None:
        return None
    digest = _sha256(view)
    for commit in (backup.commit, ledger_commit):
        if commit is None:
            continue
        records = backup.records_at(commit)
        record = None if records is None else records.get(file_id)
        if record is not None and record.sha256 == digest:
            return commit
    return None


def _decision(
    record: ManifestRecord,
    *,
    live_bytes: bytes | None,
    live_view: bytes | None,
    ledger_view: bytes | None,
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
        return _WRITE if _sha256(live_bytes if ledger_view is None else ledger_view) == entry.sha256 else _ABORT
    return _WRITE if live_bytes == baseline else _ABORT


def _release_of(located: Located) -> str:
    """The release folder name a located path lives in (`DataGrip2026.2`), or `""` for a path without one."""
    return "" if located.config_directory is None else located.config_directory.rsplit("/", 1)[-1]


def _drop_foreign_releases(
    ledger: Mapping[str, LedgerEntry], manifest: Manifest | None, paths: LivePaths
) -> dict[str, LedgerEntry]:
    """The ledger without baselines that were taken in another release folder than this machine uses now.

    After an IDE upgrade the new folder holds defaults or nothing, and a baseline from the old folder would read
    those as a local edit (defaults overwrite the other Macs) or as a deletion (suppressed, never applied again).
    A dropped entry is a file with no baseline here — the same path as a reinstalled app — and is never marked
    suppressed because of the version change. An entry with no recorded release (a legacy ledger) is kept.
    """
    records = {} if manifest is None else manifest.records
    kept: dict[str, LedgerEntry] = {}
    for file_id, entry in ledger.items():
        record = records.get(file_id)
        located = None if record is None or entry.release == "" else paths.resolve(record.path)
        current = "" if located is None or isinstance(located, Unresolved) else _release_of(located)
        if current != "" and current != entry.release:
            continue
        kept[file_id] = entry
    return kept


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
    paths: LivePaths | None = None,
    target: Callable[[ManifestRecord], _Source] = lambda record: _NO_SOURCE,
) -> dict[str, LedgerEntry]:
    """Rewrite the ledger so the next cycle sees in-agreement files as a normal baseline.

    Gated exactly like apply: this reads live paths, so a record that is denied, that escapes
    `$HOME`, that is not in the local allowlist, that is under a glob whose directory may not be used,
    or that belongs to another machine is never opened — the deny-list refuses before anything is read
    (``AGENTS.md`` §3.4). The digest is the **projection's**, matching what collect and the record
    compare, so a file whose only difference is a stripped key stays settled instead of being
    re-collected on every cycle.
    """
    paths = paths or LivePaths(catalog=presets.catalog)
    settled: dict[str, LedgerEntry] = {}
    unsafe = _unsafe_glob_directories(config, live, deny, home, paths)
    for file_id, record in target_manifest.records.items():
        if record.scope == "machine" and record.machine != config.machine_id:
            continue
        if _record_refusal(presets, config, deny, home, record) is not None:
            continue
        if any(glob.matches(record.path) for glob in unsafe):
            # Settling would read the file through the symlinked directory — the same path apply
            # refused to write. Nothing is sealed or pushed, but a refused path is not read either.
            continue
        located = paths.resolve(record.path)
        if isinstance(located, Unresolved) or _real_path_refusal(live, deny, home, record.path, located) is not None:
            # No real path to read (the profile is not here, the index is unreadable, or its directory is
            # unsafe). The baseline is kept as it was: a profile this machine cannot see is not one it
            # deleted, so nothing is marked removed (``docs/design/03-manifest.md``).
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = old
            continue
        live_path = _join(home, located.path)
        # Known limitation (TOCTOU): the folder is checked here and `LiveFiles.write` later runs `mkdir(parents=True)`,
        # so a folder removed in between would be re-created. Profiles share this pattern; tracked as a follow-up.
        if (
            live.is_symlink(live_path)
            or (located.config_directory is not None and not live.exists(_join(home, located.config_directory)))
            or (located.agent_root is not None and not live.exists(_join(home, located.agent_root)))
        ):
            # A symlink is never read, and a release folder that is not there is not a file the user removed: the
            # baseline stays, so the record is neither suppressed nor re-created when the app comes back.
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = old
            continue
        try:
            live_bytes = live.read(live_path)
        except DocumentRefused:
            # Not "removed": a document the store would not open keeps its baseline, so nothing is suppressed.
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = old
            continue
        if live_bytes is None:
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = replace(old, suppressed=True)
            continue
        digest = _ledger_digest(presets, live_bytes, record.path, record.format, target(record))
        if digest is not None and digest == record.sha256:
            settled[file_id] = LedgerEntry(
                sha256=digest, commit=target_commit, suppressed=False, release=_release_of(located)
            )
        else:
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = old
    return settled


def _ledger_digest(presets: _Presets, live_bytes: bytes, path: str, fmt: FileFormat, source: _Source) -> str | None:
    """The digest the ledger should hold for a live file, or `None` when it cannot be projected.

    `source` reads the object the cycle ended on, because that is the object the next collect measures the file
    against (``docs/design/02-base-and-merge.md``).
    """
    projected = _project_file(presets, live_bytes, path, fmt, source=source)
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
    wrapper: GenerationWrap,
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
    apps: AppDetector = NO_APP_INFO,
    running: RunningApps = NO_RUNNING_INFO,
    adopt: AdoptChoice | None = None,
    resolving: bool = False,
    decide: Decide | None = None,
    when_tag: WhenTag | None = None,
    profiles: ProfileIndex | None = None,
    config_dirs: AppConfigDir | None = None,
    agent_projects: AgentProjectIndex | None = None,
    rotation_marker: RotationMarkerStore = NO_ROTATION_MARKER,
    sessions: SessionStore = NO_SESSION_STORE,
    activity: SessionActivity = NO_SESSION_ACTIVITY,
    clock: Callable[[], float] = time.time,
) -> SyncReport:
    """Run one sync cycle. A conflict returns a report; a hard failure raises.

    `resolving` runs the cycle that `resolve` settles a pending state with; `decide` answers the
    conflicts that cycle finds (see `Resolution`). A plain sync passes neither.
    """
    presets = _Presets(catalog=catalog, adapters=adapters, validators=validators, when_tag=when_tag)
    lock.acquire()
    try:
        if resolving:
            if pending_store.load() is None:
                raise PendingConflict("there is no pending conflict to resolve")
        elif pending_store.is_pending():
            raise PendingConflict(
                "a previous sync stopped with unresolved conflicts; resolve them before syncing again"
            )
        # Read once per run: every live path of the cycle is resolved against this one view of the profiles, and
        # every file that obeys the user's ignore list against this one reading of it.
        presets = replace(
            presets,
            ignores=_read_ignores(catalog, adapters, live, deny, home),
            sessions=sessions,
            activity=activity,
            now=clock(),
        )
        paths = open_live_paths(config, catalog, profiles, config_dirs, agent_projects)
        view = PolicyView(
            transport,
            cipher=cipher,
            catalog=catalog,
            project=static_projector(presets),
            scanner=scanner,
        )
        return _cycle(
            transport=view,
            files=files,
            cipher=cipher,
            scanner=scanner,
            store=store,
            codec=codec,
            wrapper=wrapper,
            driver=driver,
            presets=presets,
            live=live,
            ledger_store=ledger_store,
            pending_store=pending_store,
            rotation_marker=rotation_marker,
            deny=deny,
            apps=apps,
            running=running,
            source=source,
            config=config,
            home=home,
            branch=branch,
            adopt=adopt,
            resolution=Resolution(decide=decide, resolving=resolving),
            paths=paths,
        )
    finally:
        lock.release()


def _one_per_session(skipped: tuple[SessionSkipped, ...]) -> tuple[SessionSkipped, ...]:
    """The first report for each session and reason, in the order found."""
    first: dict[tuple[str, str], SessionSkipped] = {}
    for each in skipped:
        first.setdefault((each.session_id, each.reason), each)
    return tuple(first.values())


def _answer(views: tuple[ConflictView, ...], resolution: Resolution) -> Picks | None:
    """The picks the next pass should use, after asking for decisions on the conflicts this pass found.

    `None` when there is no one to ask, or when the answer adds nothing the run has not already
    decided: then the conflicts cannot move, and the run stops with them pending.
    """
    if resolution.decide is None or not views or resolution.rounds >= MAX_RESOLVE_ROUNDS:
        return None
    fresh = resolution.decide(views, resolution.picks)
    if not fresh or not any(key not in resolution.picks for key in fresh):
        return None
    return MappingProxyType({**resolution.picks, **fresh})


def _next(resolution: Resolution, picks: Picks) -> Resolution:
    """The resolution for the rerun that follows an answer: the same run, with its picks and one round more."""
    return Resolution(
        picks=picks,
        decide=resolution.decide,
        resolving=resolution.resolving,
        rounds=resolution.rounds + 1,
    )


def _cycle(
    *,
    transport: PolicyView,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    store: KeyStore,
    codec: RingCodec,
    wrapper: GenerationWrap,
    driver: MergeDriver,
    presets: _Presets,
    live: LiveFiles,
    ledger_store: LedgerStore,
    pending_store: PendingStore,
    rotation_marker: RotationMarkerStore,
    deny: DenyList,
    apps: AppDetector,
    running: RunningApps,
    source: FileIdSource,
    config: SyncConfig,
    home: str,
    branch: str,
    adopt: AdoptChoice | None,
    resolution: Resolution,
    paths: LivePaths,
    _restarted: bool = False,
    _carried_notes: tuple[str, ...] = (),
) -> SyncReport:
    def again(next_resolution: Resolution, restarted: bool, carried: tuple[str, ...] = _carried_notes) -> SyncReport:
        return _cycle(
            transport=transport,
            files=files,
            cipher=cipher,
            scanner=scanner,
            store=store,
            codec=codec,
            wrapper=wrapper,
            driver=driver,
            presets=presets,
            live=live,
            ledger_store=ledger_store,
            pending_store=pending_store,
            rotation_marker=rotation_marker,
            deny=deny,
            apps=apps,
            running=running,
            source=source,
            config=config,
            home=home,
            branch=branch,
            adopt=adopt,
            resolution=next_resolution,
            paths=paths,
            _restarted=restarted,
            _carried_notes=carried,
        )

    ledger = dict(ledger_store.load())
    head_commit = transport.head()
    if head_commit is not None:
        # Discard anything a previous, failed cycle left in the working tree before `git add -A`
        # can stage it — a partially written object or a stale manifest. The commits, not the
        # working tree, are the source of truth.
        transport.materialize(head_commit)
    head_manifest = _read_manifest(transport, cipher, head_commit)
    ledger = _drop_foreign_releases(ledger, head_manifest, paths)
    # The write is planned against what is stored: against the view, a migrated object would look unchanged.
    stored_head = _read_manifest(transport.raw, cipher, head_commit)
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
        paths=paths,
        baseline=_baseline_reader(transport, cipher, ledger, head_commit),
        scanner=scanner,
        head_object=lambda record: _verified_object(transport, cipher, head_commit, record.file_id, record.sha256),
    )
    if collected.first_sight:
        # Present both sides before anything is written; the user picks with --adopt.
        return SyncReport(
            first_sight=tuple(collected.first_sight),
            symlinks=tuple(collected.symlinks),
            skipped=tuple(collected.skipped),
            refused=tuple(collected.refused),
            unprojectable=tuple(collected.unprojectable),
            notes=(*_carried_notes, *presets.notes(), *transport.notes()),
        )
    if collected.stale:
        found = _merge_stale(
            config=config,
            transport=transport,
            cipher=cipher,
            driver=driver,
            presets=presets,
            head_commit=head_commit,
            collected=collected,
            resolution=resolution,
        )
        if found:
            views = tuple(view for item in found for view in item.views)
            answer = _answer(views, resolution)
            if answer is not None:
                return again(_next(resolution, answer), _restarted)
            stale_conflicts = tuple(item.record for item in found)
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
    new_manifest = Manifest.create(
        schema=MANIFEST_SCHEMA,
        key_generation=generation,
        records=collected.records,
        policy=stamp_policies(collected.records.values(), presets.catalog),
    )
    plan = plan_writes(stored_head, new_manifest)
    # A live file that merely matches HEAD again (a crash between apply and the ledger save) must
    # not produce an empty commit; the write plan, not the collect list, decides (step 4–5).
    changed = plan.write_manifest or bool(plan.objects_to_write) or bool(plan.objects_to_delete)
    if changed:
        write_repo(
            files=files,
            cipher=cipher,
            scanner=scanner,
            head=stored_head,
            new=new_manifest,
            contents=collected.contents,
            # An object the migration re-projected and this collect did not touch keeps the gate's sealed blob.
            carried=transport.migrated_blobs(head_commit),
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
        wrapper=wrapper,
        store=store,
        rotation_marker=rotation_marker,
        config=config,
        branch=branch,
        local_commit=local_commit,
        local_manifest=new_manifest,
        resolution=resolution,
    )
    if outcome.restart:
        # `_publish` adopted a remote that had rotated ahead of our collect commit; re-collect on
        # the new generation. One restart settles it (the adopted tip is the remote's generation),
        # so a second request means the remote rotated again mid-cycle.
        if _restarted:
            raise GitError("the remote rotated again while this machine adopted it; re-run `syncintosh sync`")
        return again(resolution, True, (*_carried_notes, *(() if outcome.note is None else (outcome.note,))))
    if outcome.conflicts:
        answer = _answer(outcome.views, resolution)
        if answer is not None:
            return again(_next(resolution, answer), _restarted)
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
    if resolution.resolving:
        # The push landed, so the conflicts this run settled are no longer pending.
        pending_store.clear()

    target_commit = outcome.published or local_commit or ""
    target_manifest = _read_manifest(transport, cipher, target_commit) or new_manifest
    # The restore point is the commit this cycle made (or the HEAD it kept), never the publish's merge; a file
    # published earlier but not yet applied is restored from the ledger's commit instead (`_restore_point`).
    # One manifest decrypt per commit per cycle, however many files ask.
    records_cache: dict[str, Mapping[str, ManifestRecord] | None] = {}

    def records_at(commit: str) -> Mapping[str, ManifestRecord] | None:
        if commit not in records_cache:
            manifest = _read_manifest(transport, cipher, commit)
            records_cache[commit] = None if manifest is None else manifest.records
        return records_cache[commit]

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
        apps=apps,
        running=running,
        config=config,
        home=home,
        adopt=adopt,
        backup=Backup.create(local_commit, records_at=records_at),
        scanner=scanner,
        unstored=frozenset(change.record.file_id for change in collected.stale),
        paths=paths,
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
            paths=paths,
            target=lambda record: _object_reader(transport, cipher, target_commit, record),
        )
    )
    return SyncReport(
        published_commit=outcome.published,
        backup_commit=applied.backup,
        restore_points=applied.restore_points,
        dropped=applied.dropped,
        created=tuple(collected.created),
        collected=tuple(collected.collected),
        applied=applied.applied,
        # The same deferral can be found twice (collect lists a profile's borrowed file, and apply meets its
        # record); one row is enough, in the order it first appeared.
        deferred=tuple(dict.fromkeys((*collected.deferred, *applied.deferred))),
        suppressed=tuple(collected.suppressed),
        symlinks=tuple(dict.fromkeys((*collected.symlinks, *applied.symlinks))),
        aborted=applied.aborted,
        refused=tuple(dict.fromkeys((*collected.refused, *applied.refused))),
        unprojectable=tuple(dict.fromkeys((*collected.unprojectable, *applied.unprojectable))),
        invalid=applied.invalid,
        skipped=tuple(collected.skipped),
        unchanged=not changed and outcome.published in (None, local_commit),
        notes=(*_carried_notes, *presets.notes(), *transport.notes(), *applied.notes),
        # One row per session and reason: collect names the session, apply the file, and they are the same finding.
        session_skipped=_one_per_session(
            (*collected.session_skipped, *outcome.session_skipped, *applied.session_skipped)
        ),
    )


__all__ = [
    "MAX_PUSH_ATTEMPTS",
    "NO_APP_INFO",
    "NO_CATALOG",
    "NO_RESOLUTION",
    "NO_RUNNING_INFO",
    "NO_SESSION_ACTIVITY",
    "NO_SESSION_STORE",
    "NO_VALIDATORS",
    "Backup",
    "Deferral",
    "DroppedLocal",
    "RecordReader",
    "Resolution",
    "RestorePoint",
    "SyncReport",
    "sync",
]
