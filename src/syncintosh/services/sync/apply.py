# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply stage of the sync cycle: writing the merged content to the live files, with its restore point.

`services.sync` is split by stage; see its package docstring for the cycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from syncintosh.core.config import SyncConfig
from syncintosh.core.errors import DocumentRefused, DocumentWrittenUnverified, SecretScanError
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.manifest import Manifest, ManifestRecord
from syncintosh.core.models import LedgerEntry
from syncintosh.core.ports import AppDetector, Cipher, DenyList, GitTransport, LiveFiles, RunningApps, SecretScanner
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.projection import DroppedItem
from syncintosh.core.repo import object_path
from syncintosh.core.sessions import SessionSkipped
from syncintosh.core.tree import path_label
from syncintosh.services.policy_gate import PolicyView
from syncintosh.services.profile_paths import LivePaths, Unresolved
from syncintosh.services.sync._shared import (
    _ABORT,
    _AGENT_ROOT_MISSING,
    _APP_NOT_INSTALLED,
    _APP_RUNNING,
    _CONFIG_FOLDER_MISSING,
    _DEFER,
    _DIRECTORY_MISSING,
    _GLOB_DIRECTORY_UNSAFE,
    _NO_RESTORE_POINT,
    _NOT_IN_RESTORE_POINT,
    _OBJECT_MISSING,
    _PROFILE_FOLDER_MISSING,
    _RESTORE_MESSAGE,
    _RESTORE_SCAN_REFUSED,
    _SKIP,
    _SYNTAX_FAILED,
    _WRITE,
    NO_RUNNING_INFO,
    Backup,
    Deferral,
    DroppedLocal,
    Refusal,
    RestorePoint,
    _Decision,
    _decision,
    _document_refusal,
    _entry_reader,
    _join,
    _object_reader,
    _Presets,
    _project_file,
    _record_refusal,
    _reduce_for_apply,
    _refusal,
    _sha256,
    _syntax_ok,
    _verified_object,
)
from syncintosh.services.sync.collect import _real_path_refusal, _unsafe_glob_directories

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
    # The session files this cycle wrote, noted once the loop is done so an ancestor written later is there to find.
    written: list[tuple[str, bytes, bool]] = []
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
            if live_bytes is not None and _sha256(live_bytes) == record.sha256:
                # Already what the repository holds: nothing to decrypt or write.
                continue
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
            if presets.sessions.retired(record.path):
                session_skipped.append(SessionSkipped.create(session_id=session, reason="archived", path=record.path))
                continue
            # A conversation, not a setting: a file created here is owner-only, as a person's own transcripts should be.
            live.write(live_path, data, private=True)
            applied.append(record.path)
            written.append((record.path, data, live_bytes is None))
            continue
        # Collect's projection stands in for this one only when the file is byte-for-byte the one it was made from.
        # A record whose format is not the catalog's never gets here (refused above), so the two agree on the format.
        # A file with carried keys is never reused: collect measured it against the ledger's object, while apply
        # measures it against the one it is about to write, and the two projections differ in the carried keys.
        reusable = (
            live_bytes is not None
            and live_bytes == collect_baseline
            and stripped_policy is not None
            and not stripped_policy.ignore_list
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
                entry=ledger.get(file_id),
            )
            if view.failure is not None:
                unprojectable.append(_refusal(record.path, view.failure))
                continue
            live_view = view.view
        changed = live_bytes is not None and collect_baseline is not None and live_bytes != collect_baseline
        entry = ledger.get(file_id)
        # Keys this Mac stopped carrying take the repo's value, which only a write can do: the live projection
        # already agrees with the record, so without this the file would read as in sync.
        adopted = (
            frozenset()
            if stripped_policy is None or entry is None
            else presets.adopted_in(
                stripped_policy, entry, live_bytes, _entry_reader(transport, cipher, file_id, entry)
            )
        )
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
            and (stripped_policy.ignore_list or presets.carried(stripped_policy))
        ):
            measured = _project_file(
                presets,
                live_bytes,
                record.path,
                record.format,
                source=_entry_reader(transport, cipher, file_id, entry),
                entry=entry,
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
            adopting=bool(adopted),
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
        if adopted and live_bytes == data:
            # The live file already holds the repo's value for each adopted key, so there is nothing to write.
            continue
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
            if entry is None or entry.suppressed or file_id in unstored or adopted:
                # Its bytes are in no commit (an `--adopt remote` overwrite, or a stale-baseline edit): save them first.
                # So are an adopted key's: every commit holds the repo's value there, and this Mac's own is what the
                # write replaces, so the view is the live projection with no adoption.
                held_in = _save_restore_point(
                    transport=transport,
                    cipher=cipher,
                    scanner=scanner,
                    backup=backup,
                    key_generation=target_manifest.key_generation,
                    record=record,
                    view=_unadopted_view(presets, live_bytes, record, entry, ledger_view, transport, cipher, file_id)
                    if adopted
                    else ledger_view,
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
        notes=tuple(
            dict.fromkeys(
                note
                for path, data, created in written
                if (note := presets.sessions.note_for(path, data, created=created)) is not None
            )
        ),
    )


def _unadopted_view(
    presets: _Presets,
    live_bytes: bytes | None,
    record: ManifestRecord,
    entry: LedgerEntry | None,
    fallback: bytes | None,
    transport: PolicyView,
    cipher: Cipher,
    file_id: str,
) -> bytes | None:
    """The live file's projection with this Mac's own value for each adopted key, which is what an apply overwrites.

    The ledger's source still decides the keys that are carried. `fallback` stands in when it cannot be projected.
    """
    if live_bytes is None or entry is None:
        return fallback
    measured = _project_file(
        presets, live_bytes, record.path, record.format, source=_entry_reader(transport, cipher, file_id, entry)
    )
    return fallback if measured.failure is not None else measured.view


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
