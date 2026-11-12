# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deciding that a VS Code profile was renamed, and which records follow it (issue #67).

A profile record is keyed by the profile's **name** (`core/profiles.py`), so renaming the profile in the app leaves the
old name's records behind and publishes new ones. The directory id is stable on one Mac and differs per Mac, so the
apply ledger remembers an **anchor** per profile record: `(directory id, profile_anchor(name))` as it stood when the
baseline was settled. Comparing the anchor with the records and with this Mac's profile list yields exactly one of:

* **move** — this Mac renamed the profile: the records still carry the anchored name, the anchored directory is now
  called something else, and nothing here has the old name. The records keep their `file_id` and change only their
  path, so the objects are neither re-sealed nor renamed (the AAD binds the `file_id`, not the path).
* **renamed elsewhere** — another Mac renamed it: the records carry a name this Mac's directory lacks. Moving
  them back would undo the rename, so the profile is left for the person to rename here (`storage.json` is never
  written, issue #22 D5) and the caller defers collecting it.
* **blocked** — the evidence is ambiguous or the move would not be allowed; the old records stay as they are.

Nothing here guesses: a record without an anchor is never matched (a ledger from before the anchor existed), an
unreadable profile list moves nothing, and a rename that only changes case is not a rename (`collision_key`).

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal

from syncintosh.core.manifest import ManifestRecord
from syncintosh.core.models import LedgerEntry
from syncintosh.core.profiles import ProfileMap, ProfilePath, ProfileSpec, profile_name_key

BlockReason = Literal[
    "unreadable",
    "not-allowed",
    "target-exists",
    "anchors-disagree",
    "concurrent-rename",
    "old-name-present",
]

UNREADABLE: Final[BlockReason] = "unreadable"
NOT_ALLOWED: Final[BlockReason] = "not-allowed"
TARGET_EXISTS: Final[BlockReason] = "target-exists"
ANCHORS_DISAGREE: Final[BlockReason] = "anchors-disagree"
CONCURRENT_RENAME: Final[BlockReason] = "concurrent-rename"
OLD_NAME_PRESENT: Final[BlockReason] = "old-name-present"


def profile_anchor(name: str) -> str:
    """The digest the ledger stores for a profile name: `sha256` of its `collision_key`, hex.

    The name itself never reaches the ledger (``AGENTS.md`` §3.3). This keeps names out of the state file; it is not
    a secret, since the same Mac's `storage.json` holds the name in the clear. Folding first makes a case- or
    normalization-only change the same anchor, which is what keeps it from reading as a rename.
    """
    return hashlib.sha256(profile_name_key(name).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProfileMove:
    """This Mac renamed profile `old` to `new`: every record of the profile takes the new path, `file_id` unchanged."""

    old: str
    new: str
    paths: tuple[tuple[str, str], ...]  # (file_id, new logical path), by file_id


@dataclass(frozen=True)
class RenamedElsewhere:
    """Another Mac renamed profile `old` (what `directory` is still called here) to `new`."""

    old: str
    new: str
    directory: str


@dataclass(frozen=True)
class RenameBlocked:
    """The records of profile `name` look renamed but are left where they are, for `reason`."""

    name: str
    reason: BlockReason


@dataclass(frozen=True)
class ProfileRenamePlan:
    """What the profile records and this Mac's profile list say about renames; every part is a sorted tuple."""

    moves: tuple[ProfileMove, ...] = ()
    elsewhere: tuple[RenamedElsewhere, ...] = ()
    blocked: tuple[RenameBlocked, ...] = ()


@dataclass(frozen=True)
class _Group:
    name: str
    members: tuple[tuple[ManifestRecord, ProfilePath], ...]


def plan_profile_renames(
    records: Mapping[str, ManifestRecord],
    ledger: Mapping[str, LedgerEntry],
    profiles: ProfileMap,
    spec: ProfileSpec,
    allows: Callable[[ManifestRecord], bool],
) -> ProfileRenamePlan:
    """Classify every profile of `spec` that the ledger has an anchor for.

    `allows` is the local allowlist over a record (`core.config.allows`); it is asked about the records as they
    would be after the move, so a name this machine's config does not map is never moved to (``AGENTS.md`` §3.6,
    issue #22 D3).
    """
    groups = _groups(records.values(), spec)
    moves: list[ProfileMove] = []
    elsewhere: list[RenamedElsewhere] = []
    blocked: list[RenameBlocked] = []
    anchored = {
        key: {
            (entry.profile_dir, entry.profile_key)
            for record, _ in group.members
            if (entry := ledger.get(record.file_id)) is not None and entry.profile_dir and entry.profile_key
        }
        for key, group in groups.items()
    }
    # One directory answers for one profile: two groups anchored to it cannot both be what it was called.
    claims = [directory for anchors in anchored.values() for directory in {each[0] for each in anchors}]
    for key in sorted(groups):
        group = groups[key]
        anchors = anchored[key]
        if not anchors:
            continue
        if not profiles.readable:
            blocked.append(RenameBlocked(group.name, UNREADABLE))
            continue
        if len(anchors) > 1 or claims.count(next(iter(anchors))[0]) > 1:
            blocked.append(RenameBlocked(group.name, ANCHORS_DISAGREE))
            continue
        ((directory, ledger_key),) = anchors
        local = next((entry for entry in profiles.entries if entry.directory == directory), None)
        # The directory is gone: a profile this Mac cannot see is not one it renamed, and nothing is a deletion.
        if local is None or profile_name_key(local.name) == key:
            continue
        if ledger_key == profile_anchor(group.name):
            outcome = _move_or_block(group, local.name, groups, profiles, spec, allows)
            if isinstance(outcome, ProfileMove):
                moves.append(outcome)
            else:
                blocked.append(outcome)
        elif ledger_key == profile_anchor(local.name):
            elsewhere.append(RenamedElsewhere(old=local.name, new=group.name, directory=directory))
        else:
            blocked.append(RenameBlocked(group.name, CONCURRENT_RENAME))
    kept, clashing = _split_clashing(moves)
    return ProfileRenamePlan(
        moves=tuple(kept),
        elsewhere=tuple(elsewhere),
        blocked=tuple(blocked) + tuple(RenameBlocked(move.old, ANCHORS_DISAGREE) for move in clashing),
    )


def moved_records(records: Mapping[str, ManifestRecord], plan: ProfileRenamePlan) -> dict[str, ManifestRecord]:
    """`records` with every planned move applied: the path changes, every other field and the `file_id` do not."""
    new_paths = {file_id: path for move in plan.moves for file_id, path in move.paths}
    return {
        file_id: replace(record, path=new_paths[file_id]) if file_id in new_paths else record
        for file_id, record in records.items()
    }


def _groups(records: Iterable[ManifestRecord], spec: ProfileSpec) -> dict[str, _Group]:
    """The records of each profile, by `profile_name_key`; a record that is not a logical profile path is no member."""
    members: dict[str, list[tuple[ManifestRecord, ProfilePath]]] = {}
    for record in sorted(records, key=lambda each: each.file_id):
        parsed = spec.parse(record.path)
        if parsed is not None:
            members.setdefault(profile_name_key(parsed.name), []).append((record, parsed))
    return {key: _Group(name=found[0][1].name, members=tuple(found)) for key, found in members.items()}


def _move_or_block(
    group: _Group,
    new_name: str,
    groups: Mapping[str, _Group],
    profiles: ProfileMap,
    spec: ProfileSpec,
    allows: Callable[[ManifestRecord], bool],
) -> ProfileMove | RenameBlocked:
    # Some other directory is called by the old name now (a swap, or a new profile): the records may belong to it.
    if profiles.lookup(group.name) is not None:
        return RenameBlocked(group.name, OLD_NAME_PRESENT)
    paths = tuple((record.file_id, spec.logical_path(new_name, parsed.relative)) for record, parsed in group.members)
    if not all(allows(replace(record, path=path)) for (record, _), (_, path) in zip(group.members, paths, strict=True)):
        return RenameBlocked(group.name, NOT_ALLOWED)
    # Records already sit under the new name: moving would put two records on one path, so none of the profile moves.
    if profile_name_key(new_name) in groups:
        return RenameBlocked(group.name, TARGET_EXISTS)
    return ProfileMove(old=group.name, new=new_name, paths=paths)


def _split_clashing(moves: list[ProfileMove]) -> tuple[list[ProfileMove], list[ProfileMove]]:
    """Moves whose target name no other move claims, and the ones that clash on one."""
    claims = [profile_name_key(move.new) for move in moves]
    kept = [move for move, claim in zip(moves, claims, strict=True) if claims.count(claim) == 1]
    return kept, [move for move in moves if move not in kept]


__all__ = [
    "ANCHORS_DISAGREE",
    "CONCURRENT_RENAME",
    "NOT_ALLOWED",
    "OLD_NAME_PRESENT",
    "TARGET_EXISTS",
    "UNREADABLE",
    "BlockReason",
    "ProfileMove",
    "ProfileRenamePlan",
    "RenameBlocked",
    "RenamedElsewhere",
    "moved_records",
    "plan_profile_renames",
    "profile_anchor",
]
