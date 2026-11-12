# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collect stage of the sync cycle: reading the tracked files into projected, staged content.

`services.sync` is split by stage; see its package docstring for the cycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from syncintosh.core.append import append_merge
from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.errors import DocumentRefused, SecretScanError
from syncintosh.core.ledger import AdoptChoice, chosen_digest, classify
from syncintosh.core.manifest import Manifest, ManifestRecord, mint_file_id
from syncintosh.core.models import ConflictAddress, LedgerEntry
from syncintosh.core.paths import collision_key
from syncintosh.core.ports import DenyList, FileIdSource, LiveFiles, SecretScanner, WhenTag
from syncintosh.core.presets import MAX_GLOB_EXPANSION, PresetGlob
from syncintosh.core.sessions import MAX_FILE_BYTES, SessionSkipped, SessionUnit, eligibility, within_project_cap
from syncintosh.core.tree import path_label
from syncintosh.services.profile_paths import LivePaths, Located, Unresolved, profile_directory
from syncintosh.services.sync._shared import (
    _DENIED,
    _ESCAPES,
    _FILE_NOT_HERE,
    _GLOB_DIRECTORY_UNSAFE,
    _GLOB_FULL,
    _NO_SOURCE,
    _PROFILE_DIRECTORY_UNSAFE,
    UNOWNED_PATH,
    Deferral,
    Refusal,
    _Collect,
    _document_refusal,
    _find_item_record,
    _join,
    _Presets,
    _project_file,
    _Projected,
    _refusal,
    _replace_sha,
    _sha256,
    _Source,
    _StaleChange,
)
from syncintosh.services.sync.renames import _plan_renames

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
    config: SyncConfig,
    head: Manifest | None,
    live: LiveFiles,
    deny: DenyList,
    home: str,
    paths: LivePaths,
    held_directories: frozenset[str] = frozenset(),
) -> _Expanded:
    """The logical items of every allowed profile this machine really has.

    One item per profile per file (decision D4), with the logical path a record carries; the live path
    is `paths.resolve`'s; the directory a glob lists is joined by `profile_directory`, the one place that
    does. A file whose resource the profile borrows from the default profile is **not** collected
    (decision D6) and is reported. A snippet glob lists the profile's own
    `snippets` directory, after that directory passes the same symlink/deny/escape checks a glob
    directory gets. Problems the profile index reported (a collision, a malformed entry, an unreadable
    list) are surfaced as non-blocking rows, so a person can see why a profile is missing.

    A profile in `held_directories` (real directories) is skipped whole: another Mac renamed it, and collecting it
    under the name it still has here would bring the old records back (issue #67). `_collect` has already said why.
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
            if not entry.allows(profile.name) or profile_directory(spec, profile.directory) in held_directories:
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
    # A profile renamed in the app moves its records before anything is matched against them, so the collect below
    # finds them under the new name instead of creating them again (issue #67).
    renames = _plan_renames(config, {} if head is None else head.records, ledger, paths)
    state = _Collect(records=dict(renames.records), renamed=list(renames.moved))
    state.deferred.extend(renames.deferrals)
    state.refused.extend(renames.refusals)
    named = None if head is None or not renames.moved else replace(head, records=renames.records)
    expanded = _expand_globs(config, head, live, deny, home, paths)
    profiled = _expand_profiles(config, head if named is None else named, live, deny, home, paths, renames.held)
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
            entry=None if record is None else ledger.get(record.file_id),
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
    listing = presets.sessions.sessions(
        frozenset(() if head is None else (record.path for record in head.records.values()))
    )
    state.session_skipped.extend(listing.skipped)
    published = frozenset(
        found
        for record in (() if head is None else head.records.values())
        if (found := presets.sessions.session_of(record.path)) is not None
    )
    # The project cap is judged over every session the store lists, idle or not, so a published session that happens to
    # be open this cycle still counts and the decision for a new session does not flip with what is open right now.
    listed, over = within_project_cap(listing.units, published=published)
    state.session_skipped.extend(SessionSkipped.create(session_id=unit.session_id, reason="cap") for unit in over)
    idle: list[SessionUnit] = []
    for unit in listed:
        why = eligibility(unit, now=presets.now, is_open=presets.activity.is_open(unit.session_id))
        if why is None:
            idle.append(unit)
        else:
            state.session_skipped.append(SessionSkipped.create(session_id=unit.session_id, reason=why))
    sealed = {record.path: record.sha256 for record in (() if head is None else head.records.values())}
    for unit in idle:
        staged = _stage_session(
            state, unit, live=live, deny=deny, home=home, presets=presets, scanner=scanner, paths=paths, sealed=sealed
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
    sealed: Mapping[str, str],
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
            if _sha256(raw) != sealed.get(file.path):
                # Bytes the repository already holds were scanned when sealed; only new bytes are scanned again.
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
