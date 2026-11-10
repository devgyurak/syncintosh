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
