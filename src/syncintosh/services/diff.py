# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: show how this machine's tracked files differ from the repo, without syncing.

Read-only and lock-free: it decrypts the repo's manifest and objects **in memory** and never
writes a live file, so two `diff`s (or a `diff` beside a sync) cannot interfere
(``docs/design/01-sync-model.md``). By default it reports only *which* key paths differ — never
their values, which are the user's settings and may hold a secret (``AGENTS.md`` §4); `--values`
is the explicit opt-in that adds a unified diff.

It reuses the cycle's projection so a path whose only difference is a key the preset strips reads
as in-sync, exactly as `sync` would treat it (one implementation, ``AGENTS.md`` §7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from syncintosh.core.config import SyncConfig
from syncintosh.core.diff import changed_addresses
from syncintosh.core.errors import ContentParseError
from syncintosh.core.manifest import FileFormat, ManifestRecord
from syncintosh.core.models import ConflictAddress
from syncintosh.core.ports import (
    Cipher,
    ContentAdapter,
    DenyList,
    GitTransport,
    LiveFiles,
    PresetCatalog,
    ProfileIndex,
    SyntaxValidator,
    remote_ref,
)
from syncintosh.core.presets import ValidatorName
from syncintosh.services.profile_paths import LivePaths, Unresolved, open_live_paths
from syncintosh.services.sync import (
    _decode,
    _Presets,
    _project_file,
    _read_manifest,
    _real_path_refusal,
    _record_refusal,
    _sha256,
    _verified_object,
)

# How a tracked path stands relative to the repo side.
DiffStatus = Literal["in-sync", "changed", "missing-here", "deferred"]

# How the repo side is chosen: the remote tracking ref when it exists, else this machine's HEAD.
_REPO_SIDE = "the repo side is this machine's HEAD (nothing fetched yet)"


@dataclass(frozen=True)
class FileDiff:
    """One path: its status, and the differing key paths when that can be computed.

    `here`/`repo` are the plaintexts, kept so `--values` can render a unified diff; a caller that
    only shows key paths never reads them.
    """

    path: str
    status: DiffStatus
    addresses: tuple[ConflictAddress, ...] = field(default=(), repr=False)
    here: str | None = field(default=None, repr=False)
    repo: str | None = field(default=None, repr=False)
    # Why a path cannot be compared here (a profile this machine lacks): a fixed phrase, never file content.
    reason: str | None = None


@dataclass(frozen=True)
class DiffReport:
    """Every tracked path's diff, in the repo manifest's order."""

    entries: tuple[FileDiff, ...]
    source: str = _REPO_SIDE

    @property
    def changed(self) -> tuple[FileDiff, ...]:
        """The entries that are not in sync."""
        return tuple(entry for entry in self.entries if entry.status != "in-sync")


def diff_repo(
    *,
    transport: GitTransport,
    cipher: Cipher,
    live: LiveFiles,
    config: SyncConfig,
    home: str,
    deny: DenyList,
    branch: str,
    catalog: PresetCatalog,
    adapters: Mapping[FileFormat, ContentAdapter],
    validators: Mapping[ValidatorName, SyntaxValidator],
    profiles: ProfileIndex | None = None,
) -> DiffReport:
    """Compare every tracked path's live file with the repo side. Never writes anything."""
    presets = _Presets(catalog=catalog, adapters=adapters, validators=validators)
    paths = open_live_paths(config, catalog, profiles)
    head = transport.head()
    remote = transport.resolve(remote_ref(branch))
    repo_commit = remote or head
    source = _REPO_SIDE if remote is None else f"the repo side is {branch} at {remote[:12]}"
    manifest = _read_manifest(transport, cipher, repo_commit)
    if manifest is None:
        return DiffReport(entries=(), source=source)

    entries: list[FileDiff] = []
    for file_id, record in manifest.records.items():
        if record.scope == "machine" and record.machine != config.machine_id:
            continue
        if _record_refusal(presets, config, deny, home, record) is not None:
            # A path this machine does not track or may not write is not part of *this* machine's
            # diff; `sync` reports it, `diff` stays quiet about it.
            continue
        entries.append(_diff_record(transport, cipher, live, presets, home, repo_commit, file_id, record, paths, deny))
    return DiffReport(entries=tuple(entries), source=source)


def _diff_record(
    transport: GitTransport,
    cipher: Cipher,
    live: LiveFiles,
    presets: _Presets,
    home: str,
    repo_commit: str | None,
    file_id: str,
    record: ManifestRecord,
    paths: LivePaths,
    deny: DenyList,
) -> FileDiff:
    located = paths.resolve(record.path)
    if isinstance(located, Unresolved):
        return FileDiff(record.path, "deferred", reason=located.reason)
    unsafe = _real_path_refusal(live, deny, home, record.path, located)
    if unsafe is not None:
        # The real directory may have become a symlink or a protected path since the list was read; a
        # diff must not read through it (and `--values` would print what it found).
        return FileDiff(record.path, "deferred", reason=unsafe.reason)
    if live.is_symlink(str(Path(home) / located.path)):
        # Collect and apply never follow a link, so a diff must not either: `--values` would print whatever
        # the link points at.
        return FileDiff(record.path, "deferred", reason="the file is a symbolic link")
    repo_bytes = _verified_object(transport, cipher, repo_commit, file_id, record.sha256)
    repo_text = None if repo_bytes is None else _decode(repo_bytes)
    raw = live.read(str(Path(home) / located.path))
    if raw is None:
        return FileDiff(record.path, "missing-here", repo=repo_text)
    projected = _project_file(presets, raw, record.path, record.format)
    view = projected.view
    if view is not None and _sha256(view) == record.sha256:
        return FileDiff(record.path, "in-sync")
    return FileDiff(
        record.path,
        "changed",
        addresses=_changed_addresses(presets, view, repo_bytes, record.format, record.path),
        here=None if view is None else _decode(view),
        repo=repo_text,
    )


def _changed_addresses(
    presets: _Presets, view: bytes | None, repo_bytes: bytes | None, fmt: FileFormat, path: str
) -> tuple[ConflictAddress, ...]:
    """The differing key paths between the live projection and the repo object, or `()`.

    An identified array reports the bindings that changed, by the preset's rule for it. Only a structural
    format with an adapter can be compared key by key; a `text`/`binary` record, or one that no longer
    parses, reports the whole file as changed with no key paths.
    """
    adapter = presets.adapter_for(fmt)
    left = None if view is None else _decode(view)
    right = None if repo_bytes is None else _decode(repo_bytes)
    if adapter is None or left is None or right is None:
        return ()
    policy = presets.policy_for(path)
    arrays = policy.array_rules if policy is not None else {}
    try:
        return changed_addresses(adapter.parse(left), adapter.parse(right), arrays)
    except ContentParseError:
        return ()


__all__ = ["DiffReport", "DiffStatus", "FileDiff", "diff_repo"]
