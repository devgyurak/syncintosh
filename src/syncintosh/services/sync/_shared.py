# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pieces of the sync cycle: reports, null objects, constants, readers, and the projection helpers.

`services.sync` is split by stage; see its package docstring for the cycle."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from syncintosh.core.config import LocalItem, SyncConfig, allows, allows_path
from syncintosh.core.errors import (
    ContentDigestMismatch,
    ContentParseError,
    DocumentRefused,
    DocumentStale,
    DocumentWrittenUnverified,
    GitError,
    IgnoreListMalformed,
    ProjectionError,
    SyntaxInvalid,
)
from syncintosh.core.ignore import NO_IGNORES, IgnoreSpec
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.manifest import FileFormat, Manifest, ManifestRecord, tracking_key
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.models import LedgerEntry, Scope
from syncintosh.core.models import PendingConflict as PendingConflictRecord
from syncintosh.core.ports import (
    AgentProjectIndex,
    AppConfigDir,
    AppDetector,
    Cipher,
    ContentAdapter,
    DenyList,
    DocumentSkips,
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
)
from syncintosh.core.presets import PresetFile, ValidatorName
from syncintosh.core.projection import Carry, DroppedItem, project_text, restore_with_drops
from syncintosh.core.repo import MANIFEST_PATH, TRACKED_PATHS, object_path
from syncintosh.core.sessions import SessionListing, SessionSkipped
from syncintosh.core.tree import KeyPath
from syncintosh.services.policy_gate import PolicyView

# A push race is expected to settle in one or two tries; the cap stops a pathological fight from
# looping forever (``docs/design/01-sync-model.md`` step 13).
MAX_PUSH_ATTEMPTS = 4


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

    def sessions(self, published: frozenset[str] = frozenset()) -> SessionListing:
        return SessionListing()

    def session_of(self, path: str) -> str | None:
        return None

    def note_for(self, path: str, data: bytes, *, created: bool) -> str | None:
        return None

    def retired(self, path: str) -> bool:
        return False


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
class ProfileRenamed:
    """A profile this Mac renamed in the app: `records` records moved from `old` to `new` (`core/profile_rename.py`)."""

    old: str
    new: str
    records: int


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
    # Profiles whose records this cycle moved to a new name; reported once the collect commit is made.
    renamed: tuple[ProfileRenamed, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        """True when the cycle stopped with unresolved conflicts."""
        return bool(self.conflicts)

    @property
    def needs_adopt(self) -> bool:
        """True when a first-sight record needs `--adopt local|remote`."""
        return bool(self.first_sight)

    @property
    def failed(self) -> bool:
        """True when a script or launchd job must read this cycle as a failure; `sync` and `resolve` share this verdict.

        Conflicts, a first-sight record that needs `--adopt`, and any blocking refusal, unprojectable path, or file
        that would not parse. A path this machine simply does not track is *not* blocking: every machine carries every
        record, so it is reported and the cycle still did its job.
        """
        settled = (*self.refused, *self.unprojectable, *self.invalid)
        return self.has_conflicts or self.needs_adopt or any(refusal.blocking for refusal in settled)

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
    renamed: list[ProfileRenamed] = field(default_factory=list)


def _collect_report(
    collected: _Collect,
    *,
    committed: bool,
    notes: tuple[str, ...],
    deferred: tuple[Deferral, ...] | None = None,
    symlinks: tuple[str, ...] | None = None,
    refused: tuple[Refusal, ...] | None = None,
    unprojectable: tuple[Refusal, ...] | None = None,
    session_skipped: tuple[SessionSkipped, ...] | None = None,
    first_sight: tuple[str, ...] = (),
    conflicts: tuple[PendingConflictRecord, ...] = (),
    published_commit: str | None = None,
    backup_commit: str | None = None,
    restore_points: tuple[RestorePoint, ...] = (),
    dropped: tuple[DroppedLocal, ...] = (),
    applied: tuple[str, ...] = (),
    aborted: tuple[str, ...] = (),
    invalid: tuple[Refusal, ...] = (),
    unchanged: bool = False,
) -> SyncReport:
    """The report a collect pass fills, under one field policy for every way a cycle can end.

    What collect *observed* is reported on every return: symlinks, skipped paths, refused and unprojectable paths,
    deferrals, skipped sessions, and the notes. What a cycle *did* is reported only where it made the collect commit:
    `created`, `collected`, `suppressed`. A return before that commit (first sight, a stale-baseline conflict) would
    otherwise claim files as collected that were never committed. The observed lists default to what collect found; a
    later stage passes the merged list where it adds to them. Each field is a named parameter so a misspelt one fails
    the type check instead of reaching `SyncReport` as a stray keyword.
    """
    return SyncReport(
        symlinks=tuple(collected.symlinks) if symlinks is None else symlinks,
        skipped=tuple(collected.skipped),
        refused=tuple(collected.refused) if refused is None else refused,
        unprojectable=tuple(collected.unprojectable) if unprojectable is None else unprojectable,
        deferred=tuple(collected.deferred) if deferred is None else deferred,
        session_skipped=tuple(collected.session_skipped) if session_skipped is None else session_skipped,
        notes=notes,
        created=tuple(collected.created) if committed else (),
        collected=tuple(collected.collected) if committed else (),
        suppressed=tuple(collected.suppressed) if committed else (),
        renamed=tuple(collected.renamed) if committed else (),
        first_sight=first_sight,
        conflicts=conflicts,
        published_commit=published_commit,
        backup_commit=backup_commit,
        restore_points=restore_points,
        dropped=dropped,
        applied=applied,
        aborted=aborted,
        invalid=invalid,
        unchanged=unchanged,
    )


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


@dataclass(frozen=True)
class SyncPorts:
    """Every port one sync cycle runs against, built by `create` and handed to `sync` whole.

    One object instead of thirty keyword arguments, so `sync`, `watch` and `resolve` take the same
    wiring and a caller cannot drop a port by forgetting a keyword (``AGENTS.md`` §2.7). What differs
    from run to run (`adopt`, `resolving`, `decide`) stays an argument of `sync`.
    """

    transport: GitTransport
    files: RepoFiles
    cipher: Cipher
    wrapper: GenerationWrap
    scanner: SecretScanner
    store: KeyStore
    codec: RingCodec
    driver: MergeDriver
    adapters: Mapping[FileFormat, ContentAdapter]
    catalog: PresetCatalog
    validators: Mapping[ValidatorName, SyntaxValidator]
    live: LiveFiles
    ledger_store: LedgerStore
    pending_store: PendingStore
    rotation_marker: RotationMarkerStore
    lock: SyncLock
    deny: DenyList
    apps: AppDetector
    running: RunningApps
    config_dirs: AppConfigDir | None
    agent_projects: AgentProjectIndex | None
    documents: DocumentSkips | None
    sessions: SessionStore
    activity: SessionActivity
    profiles: ProfileIndex | None
    source: FileIdSource
    config: SyncConfig
    home: str
    branch: str
    when_tag: WhenTag | None

    @classmethod
    def create(
        cls,
        *,
        transport: GitTransport,
        files: RepoFiles,
        cipher: Cipher,
        wrapper: GenerationWrap,
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
        adapters: Mapping[FileFormat, ContentAdapter],
        catalog: PresetCatalog,
        validators: Mapping[ValidatorName, SyntaxValidator],
        rotation_marker: RotationMarkerStore,
        apps: AppDetector,
        running: RunningApps,
        config_dirs: AppConfigDir | None,
        agent_projects: AgentProjectIndex | None,
        documents: DocumentSkips | None,
        sessions: SessionStore,
        activity: SessionActivity,
        profiles: ProfileIndex | None,
        when_tag: WhenTag | None,
    ) -> SyncPorts:
        """Build the ports. None has a default, so a port a caller forgot is an error, not a silent null object."""
        return cls(
            transport=transport,
            files=files,
            cipher=cipher,
            wrapper=wrapper,
            scanner=scanner,
            store=store,
            codec=codec,
            driver=driver,
            adapters=adapters,
            catalog=catalog,
            validators=validators,
            live=live,
            ledger_store=ledger_store,
            pending_store=pending_store,
            rotation_marker=rotation_marker,
            lock=lock,
            deny=deny,
            apps=apps,
            running=running,
            config_dirs=config_dirs,
            agent_projects=agent_projects,
            documents=documents,
            sessions=sessions,
            activity=activity,
            profiles=profiles,
            source=source,
            config=config,
            home=home,
            branch=branch,
            when_tag=when_tag,
        )
