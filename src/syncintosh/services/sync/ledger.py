# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ledger stage of the sync cycle: settling the apply ledger after an apply.

`services.sync` is split by stage; see its package docstring for the cycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from syncintosh.core.config import SyncConfig
from syncintosh.core.errors import DocumentRefused
from syncintosh.core.manifest import FileFormat, Manifest, ManifestRecord
from syncintosh.core.models import LedgerEntry
from syncintosh.core.ports import DenyList, LiveFiles
from syncintosh.core.profile_rename import profile_anchor
from syncintosh.core.profiles import ProfileMap
from syncintosh.services.profile_paths import LivePaths, Located, Unresolved
from syncintosh.services.sync._shared import (
    _NO_SOURCE,
    _join,
    _Presets,
    _project_file,
    _record_refusal,
    _sha256,
    _Source,
)
from syncintosh.services.sync.collect import _real_path_refusal, _unsafe_glob_directories


def _release_of(located: Located) -> str:
    """The release folder name a located path lives in (`DataGrip2026.2`), or `""` for a path without one."""
    return "" if located.config_directory is None else located.config_directory.rsplit("/", 1)[-1]


def _profile_of(paths: LivePaths, path: str) -> tuple[ProfileMap, str] | None:
    """This machine's profile list and the profile name a logical path names, or `None` for a path that names none."""
    found = paths.logical(path)
    if found is None:
        return None
    spec, parsed = found
    view = next((candidate for candidate in paths.views if candidate.spec == spec), None)
    return None if view is None else (view.profiles, parsed.name)


def _profile_anchor_of(paths: LivePaths, path: str) -> tuple[str, str] | None:
    """`(directory id, anchor)` of the profile a logical path names on this machine, or `None` when it names none here.

    `None` covers a path that is not a profile's, a profile this Mac lacks, and an unreadable list: an entry whose
    profile cannot be seen keeps the anchor it has, because that is the evidence of what the profile was called when
    this Mac last had it (`core/profile_rename.py`).
    """
    found = _profile_of(paths, path)
    profile = None if found is None else found[0].lookup(found[1])
    return None if found is None or profile is None else (profile.directory, profile_anchor(found[1]))


def _fill_anchors(
    ledger: Mapping[str, LedgerEntry], manifest: Manifest | None, paths: LivePaths
) -> dict[str, LedgerEntry]:
    """`ledger` with an anchor on each profile entry that lacks one, taken from the records this cycle started from.

    A ledger from before the anchor existed has none, and the merge below may replace the records with a rename this
    Mac has not seen: the anchor has to be what the profile was called *before* that, or the old directory would be
    collected under its old name again (decision D6 covers only a rename made before the anchor existed).
    """
    records = {} if manifest is None else manifest.records
    filled: dict[str, LedgerEntry] = {}
    for file_id, entry in ledger.items():
        record = records.get(file_id)
        anchor = (
            None
            if (entry.profile_dir and entry.profile_key) or record is None
            else _profile_anchor_of(paths, record.path)
        )
        filled[file_id] = entry if anchor is None else replace(entry, profile_dir=anchor[0], profile_key=anchor[1])
    return filled


def _with_anchors(
    settled: Mapping[str, LedgerEntry],
    read: frozenset[str],
    previous: Mapping[str, LedgerEntry],
    manifest: Manifest,
    paths: LivePaths,
) -> dict[str, LedgerEntry]:
    """`settled` with each profile entry anchored to the directory its profile has on this machine.

    One whose live file this cycle read takes the directory and name the file was read from. One that was not read
    keeps its anchor, because a different profile that merely carries the new name must not replace the evidence of
    where the baseline came from (`core/profile_rename.py`). Two cases follow the profile instead, or the profile's
    records would be anchored two ways and block each other: the record's name is found in the *same directory* the
    anchor names (this Mac renamed it, so the baseline came from there), or the anchored *name* is unchanged and now
    belongs to another directory (a swap, or a profile created again).
    """
    anchored: dict[str, LedgerEntry] = {}
    for file_id, entry in settled.items():
        record = manifest.records.get(file_id)
        current = None if record is None else _profile_anchor_of(paths, record.path)
        old = previous.get(file_id)
        follows = (
            current is not None and old is not None and (old.profile_dir == current[0] or old.profile_key == current[1])
        )
        if current is not None and (file_id in read or follows):
            anchored[file_id] = replace(entry, profile_dir=current[0], profile_key=current[1])
        else:
            anchored[file_id] = (
                entry if old is None else replace(entry, profile_dir=old.profile_dir, profile_key=old.profile_key)
            )
    return anchored


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
    read: set[str] = set()
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
        # No adoption here: the digest says whether the live file *already* agrees with the record. An adopted key
        # whose write did not happen (aborted, refused, deferred) holds this Mac's own value, so it must not match,
        # and the entry keeps its old carried set for the next cycle to adopt again.
        digest = _ledger_digest(presets, live_bytes, record.path, record.format, target(record))
        if digest is not None and digest == record.sha256:
            policy = presets.policy_for(record.path)
            settled[file_id] = LedgerEntry(
                sha256=digest,
                commit=target_commit,
                suppressed=False,
                release=_release_of(located),
                # What this cycle carried, so the next one can tell a key that stopped being carried (and so
                # starts to sync) from one that never was.
                carried=None if policy is None else presets.carried_digests(policy),
            )
            read.add(file_id)
        else:
            old = ledger.get(file_id)
            if old is not None:
                settled[file_id] = old
    return _with_anchors(settled, frozenset(read), ledger, target_manifest, paths)


def _ledger_digest(
    presets: _Presets, live_bytes: bytes, path: str, fmt: FileFormat, source: _Source, entry: LedgerEntry | None = None
) -> str | None:
    """The digest the ledger should hold for a live file, or `None` when it cannot be projected.

    `source` reads the object the cycle ended on, because that is the object the next collect measures the file
    against (``docs/design/02-base-and-merge.md``).
    """
    projected = _project_file(presets, live_bytes, path, fmt, source=source, entry=entry)
    if projected.failure is not None or projected.view is None:
        return None
    return _sha256(projected.view)
