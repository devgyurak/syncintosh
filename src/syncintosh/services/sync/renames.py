# SPDX-License-Identifier: AGPL-3.0-or-later
"""Profile renames in the sync cycle: carrying a renamed VS Code profile's records to its new name (issue #67).

`services.sync` is split by stage; see its package docstring for the cycle. The decision is pure
(`core/profile_rename.py`); this stage feeds it the manifest records, the apply ledger and this Mac's profile list, and
turns what it says into the cycle's state: records with a new path, a deferral for a profile another Mac renamed,
and a row for a rename that was left alone. `storage.json` is never written (issue #22 D5)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from syncintosh.core.config import SyncConfig, allows
from syncintosh.core.manifest import ManifestRecord
from syncintosh.core.models import LedgerEntry
from syncintosh.core.profile_rename import (
    ANCHORS_DISAGREE,
    CONCURRENT_RENAME,
    NOT_ALLOWED,
    OLD_NAME_PRESENT,
    TARGET_EXISTS,
    UNREADABLE,
    BlockReason,
    moved_records,
    plan_profile_renames,
)
from syncintosh.core.profiles import encode_profile_name
from syncintosh.services.profile_paths import LivePaths, profile_directory
from syncintosh.services.sync._shared import Deferral, ProfileRenamed, Refusal

# Fixed phrases: a rename that was seen and left alone, so the old records stay as they are.
_BLOCKED: dict[BlockReason, str] = {
    NOT_ALLOWED: "the profile was renamed here, but this machine's config does not name the new name",
    TARGET_EXISTS: "the profile was renamed here, but records already exist under the new name",
    ANCHORS_DISAGREE: "the profile's earlier sync state points at more than one profile, so it was not moved",
    CONCURRENT_RENAME: "the profile was renamed to different names on two Macs, so it was not moved",
    OLD_NAME_PRESENT: "the profile was renamed here, but another profile now has the old name",
}


@dataclass(frozen=True)
class _Renames:
    """What the profile records, the ledger and this Mac's profile lists say about renames, for one cycle."""

    records: Mapping[str, ManifestRecord]
    moved: tuple[ProfileRenamed, ...] = ()
    deferrals: tuple[Deferral, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    # The real directory of each local profile another Mac renamed: none of its files is collected until the person
    # renames it here too, because collecting it under its old name would bring the old records back.
    held: frozenset[str] = field(default_factory=frozenset)


def _plan_renames(
    config: SyncConfig, records: Mapping[str, ManifestRecord], ledger: Mapping[str, LedgerEntry], paths: LivePaths
) -> _Renames:
    """`records` with this Mac's profile renames applied, and the reports and holds the rest of the cycle needs.

    Read-only: it changes no file and no ledger entry, so `diff` shares it. A layout this machine does not track, or
    whose list could not be read, moves nothing (the index problem is already reported, and a profile this Mac cannot
    see is not one it renamed).
    """
    current = dict(records)
    moved: list[ProfileRenamed] = []
    deferrals: list[Deferral] = []
    refusals: list[Refusal] = []
    held: set[str] = set()
    for view in paths.views:
        if not any(entry.spec == view.spec for entry in config.profiles):
            continue
        spec = view.spec
        plan = plan_profile_renames(current, ledger, view.profiles, spec, lambda record: allows(config, record))
        current = moved_records(current, plan)
        moved.extend(ProfileRenamed(old=move.old, new=move.new, records=len(move.paths)) for move in plan.moves)
        for found in plan.elsewhere:
            held.add(profile_directory(spec, found.directory))
            deferrals.append(
                Deferral(
                    f"{spec.home}/{encode_profile_name(found.old)}",
                    f'the profile "{found.old}" was renamed to "{found.new}" on another Mac; '
                    "rename it in the app here too",
                )
            )
        # An unreadable list is already a row of its own (`ProfileMap.problems`), so it is not said twice.
        refusals.extend(
            Refusal(f"{spec.home}/{encode_profile_name(each.name)}", _BLOCKED[each.reason], blocking=False)
            for each in plan.blocked
            if each.reason != UNREADABLE
        )
    return _Renames(
        records=current,
        moved=tuple(moved),
        deferrals=tuple(deferrals),
        refusals=tuple(refusals),
        held=frozenset(held),
    )
