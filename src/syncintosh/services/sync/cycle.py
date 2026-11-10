# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sync cycle itself: `sync`, `_cycle`, and the answers that drive the resolve rounds.

`services.sync` is split by stage; see its package docstring for the cycle."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from syncintosh.core.errors import GitError, PendingConflict
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.models import PendingState
from syncintosh.core.policy import stamp_policies
from syncintosh.core.repo import plan_writes
from syncintosh.core.resolution import ConflictView, Decide, Picks
from syncintosh.core.sessions import SessionSkipped
from syncintosh.services.policy_gate import PolicyView
from syncintosh.services.profile_paths import LivePaths, open_live_paths
from syncintosh.services.repo_writer import write_repo
from syncintosh.services.sync._shared import (
    Backup,
    SyncPorts,
    SyncReport,
    _baseline_reader,
    _commit,
    _current_generation,
    _object_reader,
    _Presets,
    _read_ignores,
    _read_manifest,
    _verified_object,
    static_projector,
)
from syncintosh.services.sync.apply import _apply
from syncintosh.services.sync.collect import _collect
from syncintosh.services.sync.ledger import _drop_foreign_releases, _settle_ledger
from syncintosh.services.sync.merge import MAX_RESOLVE_ROUNDS, Resolution, _merge_stale
from syncintosh.services.sync.publish import _publish

# --------------------------------------------------------------------------- entry point


def sync(
    ports: SyncPorts,
    *,
    adopt: AdoptChoice | None = None,
    resolving: bool = False,
    decide: Decide | None = None,
    clock: Callable[[], float] = time.time,
) -> SyncReport:
    """Run one sync cycle. A conflict returns a report; a hard failure raises.

    `resolving` runs the cycle that `resolve` settles a pending state with; `decide` answers the
    conflicts that cycle finds (see `Resolution`). A plain sync passes neither.
    """
    presets = _Presets(
        catalog=ports.catalog, adapters=ports.adapters, validators=ports.validators, when_tag=ports.when_tag
    )
    ports.lock.acquire()
    try:
        if resolving:
            if ports.pending_store.load() is None:
                raise PendingConflict("there is no pending conflict to resolve")
        elif ports.pending_store.is_pending():
            raise PendingConflict(
                "a previous sync stopped with unresolved conflicts; resolve them before syncing again"
            )
        # Read once per run: every live path of the cycle is resolved against this one view of the profiles, and
        # every file that obeys the user's ignore list against this one reading of it.
        presets = replace(
            presets,
            ignores=_read_ignores(ports.catalog, ports.adapters, ports.live, ports.deny, ports.home),
            sessions=ports.sessions,
            activity=ports.activity,
            now=clock(),
        )
        paths = open_live_paths(
            ports.config, ports.catalog, ports.profiles, ports.config_dirs, ports.agent_projects, ports.documents
        )
        view = PolicyView(
            ports.transport,
            cipher=ports.cipher,
            catalog=ports.catalog,
            project=static_projector(presets),
            scanner=ports.scanner,
        )
        return _cycle(
            ports,
            _Run(
                view=view,
                presets=presets,
                paths=paths,
                adopt=adopt,
                resolution=Resolution(decide=decide, resolving=resolving),
            ),
        )
    finally:
        ports.lock.release()


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


@dataclass(frozen=True)
class _Run:
    """What one `sync` call fixed before the cycle: the policy view, the presets, the live paths, and the answers.

    Split from `SyncPorts` because these are built per run (the view wraps the transport, the paths are resolved
    against this run's profiles), and `resolution` changes from one round of the cycle to the next.
    """

    view: PolicyView
    presets: _Presets
    paths: LivePaths
    adopt: AdoptChoice | None
    resolution: Resolution


def _cycle(
    ports: SyncPorts,
    run: _Run,
    *,
    _restarted: bool = False,
    _carried_notes: tuple[str, ...] = (),
) -> SyncReport:
    view, presets, paths, adopt, resolution = run.view, run.presets, run.paths, run.adopt, run.resolution

    def again(next_resolution: Resolution, restarted: bool, carried: tuple[str, ...] = _carried_notes) -> SyncReport:
        return _cycle(ports, replace(run, resolution=next_resolution), _restarted=restarted, _carried_notes=carried)

    ledger = dict(ports.ledger_store.load())
    head_commit = view.head()
    if head_commit is not None:
        # Discard anything a previous, failed cycle left in the working tree before `git add -A`
        # can stage it — a partially written object or a stale manifest. The commits, not the
        # working tree, are the source of truth.
        view.materialize(head_commit)
    head_manifest = _read_manifest(view, ports.cipher, head_commit)
    ledger = _drop_foreign_releases(ledger, head_manifest, paths)
    # The write is planned against what is stored: against the view, a migrated object would look unchanged.
    stored_head = _read_manifest(view.raw, ports.cipher, head_commit)
    collected = _collect(
        config=ports.config,
        head=head_manifest,
        ledger=ledger,
        live=ports.live,
        deny=ports.deny,
        home=ports.home,
        adopt=adopt,
        source=ports.source,
        presets=presets,
        paths=paths,
        baseline=_baseline_reader(view, ports.cipher, ledger, head_commit),
        scanner=ports.scanner,
        head_object=lambda record: _verified_object(view, ports.cipher, head_commit, record.file_id, record.sha256),
    )
    if collected.first_sight:
        # Present both sides before anything is written; the user picks with --adopt.
        return SyncReport(
            first_sight=tuple(collected.first_sight),
            symlinks=tuple(collected.symlinks),
            skipped=tuple(collected.skipped),
            refused=tuple(collected.refused),
            unprojectable=tuple(collected.unprojectable),
            notes=(*_carried_notes, *presets.notes(), *view.notes()),
        )
    if collected.stale:
        found = _merge_stale(
            config=ports.config,
            transport=view,
            cipher=ports.cipher,
            driver=ports.driver,
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
            ports.pending_store.record(
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

    generation = _current_generation(ports.store, head_manifest)
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
            files=ports.files,
            cipher=ports.cipher,
            scanner=ports.scanner,
            head=stored_head,
            new=new_manifest,
            contents=collected.contents,
            # An object the migration re-projected and this collect did not touch keeps the gate's sealed blob.
            carried=view.migrated_blobs(head_commit),
        )
        local_commit = _commit(view, ports.branch, () if head_commit is None else (head_commit,), "syncintosh: collect")
    else:
        local_commit = head_commit

    outcome = _publish(
        transport=view,
        files=ports.files,
        cipher=ports.cipher,
        scanner=ports.scanner,
        driver=ports.driver,
        presets=presets,
        codec=ports.codec,
        wrapper=ports.wrapper,
        store=ports.store,
        rotation_marker=ports.rotation_marker,
        config=ports.config,
        branch=ports.branch,
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
        ports.pending_store.record(
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
        ports.pending_store.clear()

    target_commit = outcome.published or local_commit or ""
    target_manifest = _read_manifest(view, ports.cipher, target_commit) or new_manifest
    # The restore point is the commit this cycle made (or the HEAD it kept), never the publish's merge; a file
    # published earlier but not yet applied is restored from the ledger's commit instead (`_restore_point`).
    # One manifest decrypt per commit per cycle, however many files ask.
    records_cache: dict[str, Mapping[str, ManifestRecord] | None] = {}

    def records_at(commit: str) -> Mapping[str, ManifestRecord] | None:
        if commit not in records_cache:
            manifest = _read_manifest(view, ports.cipher, commit)
            records_cache[commit] = None if manifest is None else manifest.records
        return records_cache[commit]

    applied = _apply(
        transport=view,
        cipher=ports.cipher,
        presets=presets,
        target_commit=target_commit,
        target_manifest=target_manifest,
        seen=collected.seen,
        views=collected.views,
        ledger=ledger,
        live=ports.live,
        deny=ports.deny,
        apps=ports.apps,
        running=ports.running,
        config=ports.config,
        home=ports.home,
        adopt=adopt,
        backup=Backup.create(local_commit, records_at=records_at),
        scanner=ports.scanner,
        unstored=frozenset(change.record.file_id for change in collected.stale),
        paths=paths,
    )
    ports.ledger_store.save(
        _settle_ledger(
            target_manifest=target_manifest,
            target_commit=target_commit,
            ledger=ledger,
            live=ports.live,
            deny=ports.deny,
            presets=presets,
            config=ports.config,
            home=ports.home,
            paths=paths,
            target=lambda record: _object_reader(view, ports.cipher, target_commit, record),
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
        notes=(*_carried_notes, *presets.notes(), *view.notes(), *applied.notes),
        # One row per session and reason: collect names the session, apply the file, and they are the same finding.
        session_skipped=_one_per_session(
            (*collected.session_skipped, *outcome.session_skipped, *applied.session_skipped)
        ),
    )
